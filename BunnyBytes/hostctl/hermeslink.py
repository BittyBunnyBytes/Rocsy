"""Connect Hermes to the local llama-swap API without exposing its bearer key."""
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path

from paths import BUNNY_ENV, HERMES_HOME, ROOT as ROCSY_ROOT

import yaml

HERMES_CONFIG = HERMES_HOME / 'config.yaml'
HERMES_ENV = HERMES_HOME / '.env'
HERMES_GUARDS = HERMES_HOME / 'rocsy-guards.json'   # read by the Hermes "rocsy" plugin
LLAMA_URL = 'http://127.0.0.1:8400'
ALIAS = 'Rocsy'
LEGACY_ALIASES = ('llama-swap-local',)


class LinkError(Exception):
    pass


def env_value(path: Path, name: str) -> str:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ''
    for line in lines:
        if re.fullmatch(rf'\s*(?:export\s+)?{re.escape(name)}\s*=.*', line):
            return line.split('=', 1)[1].strip().strip('"\'')
    return ''


def models(key: str) -> list[str]:
    if not key:
        raise LinkError('The llama-swap API key is missing from BunnyBytes settings')
    request = urllib.request.Request(LLAMA_URL + '/v1/models', headers={'Authorization': 'Bearer ' + key})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.load(response)
    except Exception as exc:
        raise LinkError('llama-swap did not answer its models API') from exc
    found = [item.get('id') for item in data.get('data', []) if isinstance(item, dict)]
    return [name for name in found if isinstance(name, str) and name]


def alias_config(config: dict) -> dict:
    if not isinstance(config, dict):
        return {}
    aliases = config.get('model_aliases') or {}
    value = aliases.get(ALIAS) if isinstance(aliases, dict) else None
    return value if isinstance(value, dict) else {}


def remove_managed_aliases(original: str) -> str:
    lines = original.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == 'model_aliases:'), None)
    if start is None:
        return original
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((' ', '#'))), len(lines))
    for managed in (ALIAS, *LEGACY_ALIASES):
        old = next((i for i in range(start + 1, end) if lines[i].startswith('  ' + managed + ':')), None)
        if old is not None:
            old_end = next((i for i in range(old + 1, end) if re.match(r'^  [^ #][^:]*:', lines[i])), end)
            del lines[old:old_end]
            end -= old_end - old
    return ''.join(lines)


def config_text(model: str, original: str) -> str:
    lines = remove_managed_aliases(original).splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == 'model_aliases:'), None)
    if start is None:
        lines.extend(['\n', 'model_aliases:\n'])
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((' ', '#'))), len(lines))
    block = [f'  {ALIAS}:\n', f'    model: {json.dumps(model)}\n', '    provider: custom\n',
             f'    base_url: {LLAMA_URL}/v1\n', '    key_env: LLAMA_API_KEY\n']
    lines[end:end] = block
    result = ''.join(lines)
    parsed = yaml.safe_load(result)
    if not isinstance(parsed, dict) or alias_config(parsed).get('model') != model or any(
            old in (parsed.get('model_aliases') or {}) for old in LEGACY_ALIASES):
        raise LinkError('Hermes configuration could not be validated')
    return result


def env_text(original: str, key: str) -> str:
    lines = original.splitlines(keepends=True)
    replacement = 'LLAMA_API_KEY=' + key + '\n'
    for i, line in enumerate(lines):
        if re.match(r'^\s*(?:export\s+)?LLAMA_API_KEY\s*=', line):
            lines[i] = replacement
            break
    else:
        if lines and not lines[-1].endswith('\n'):
            lines.append('\n')
        lines.append(replacement)
    return ''.join(lines)


def atomic_write(path: Path, contents: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix='.hermes-link-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def status() -> dict:
    key = env_value(BUNNY_ENV, 'LLAMA_API_KEY')
    try:
        available = models(key)
        error = ''
    except LinkError as exc:
        available, error = [], str(exc)
    try:
        config = yaml.safe_load(HERMES_CONFIG.read_text()) or {}
        alias = alias_config(config)
    except (OSError, yaml.YAMLError):
        alias = {}
        error = error or 'Hermes configuration is unavailable'
    chosen = alias.get('model', '')
    configured = (alias.get('provider') == 'custom' and alias.get('base_url') == LLAMA_URL + '/v1'
                  and alias.get('key_env') == 'LLAMA_API_KEY' and bool(chosen)
                  and bool(key) and env_value(HERMES_ENV, 'LLAMA_API_KEY') == key)
    return {'connected': configured and chosen in available, 'configured': configured,
            'model': chosen if configured else '', 'models': available, 'error': error,
            'alias': ALIAS}


def connect(model: str) -> dict:
    key = env_value(BUNNY_ENV, 'LLAMA_API_KEY')
    available = models(key)
    if model not in available:
        raise LinkError('Choose a model currently listed by llama-swap')
    try:
        original_config = HERMES_CONFIG.read_text()
        original_env = HERMES_ENV.read_text() if HERMES_ENV.exists() else ''
    except OSError as exc:
        raise LinkError('Hermes settings are unavailable') from exc
    new_config = config_text(model, original_config)
    new_env = env_text(original_env, key)
    try:
        if new_env != original_env:
            atomic_write(HERMES_ENV, new_env)
        if new_config != original_config:
            atomic_write(HERMES_CONFIG, new_config)
    except OSError as exc:
        if new_env != original_env:
            atomic_write(HERMES_ENV, original_env)
        raise LinkError('Could not save Hermes connection') from exc
    return status()


def disconnect() -> dict:
    try:
        original = HERMES_CONFIG.read_text()
    except OSError as exc:
        raise LinkError('Hermes settings are unavailable') from exc
    updated = remove_managed_aliases(original)
    try:
        parsed = yaml.safe_load(updated)
    except yaml.YAMLError as exc:
        raise LinkError('Hermes configuration could not be validated') from exc
    if not isinstance(parsed, dict) or any(name in (parsed.get('model_aliases') or {}) for name in (ALIAS, *LEGACY_ALIASES)):
        raise LinkError('Hermes configuration could not be validated')
    if updated != original:
        atomic_write(HERMES_CONFIG, updated)
    return status()


# ---- context window sync --------------------------------------------------------------------
# Hermes can't read llama-swap's real context size (its /v1/models has none), so it guessed
# 196K for a model served at 64K: compression fired far too late, prompts overflowed and replies
# were cut off mid tool call ("output length limit"). Keep each llama-swap model's per-slot
# context in Hermes' custom_providers entries (models.<id>.context_length).
LLAMA_CONFIG = ROCSY_ROOT / 'Llama/config.yaml'
LLAMA_OVERRIDES = ROCSY_ROOT / 'Llama/overrides'
LLAMA_ROUTES = ('/Llama/v1', '127.0.0.1:8400/v1', 'localhost:8400/v1')


def _flag(tokens: list[str], *names: str) -> str:
    value = ''
    for i, token in enumerate(tokens[:-1]):
        if token in names:
            value = tokens[i + 1]                 # llama.cpp keeps the last value given
    return value


def llama_contexts() -> dict[str, int]:
    """Per-slot context of every llama-swap model with an explicit --ctx-size (as llama-launch
    resolves it: config.yaml, then overrides/<id>.ctx, then --parallel from overrides/<id>.args)."""
    try:
        models_cfg = (yaml.safe_load(LLAMA_CONFIG.read_text()) or {}).get('models') or {}
    except (OSError, yaml.YAMLError):
        return {}
    found: dict[str, int] = {}
    for model_id, spec in models_cfg.items():
        if not isinstance(spec, dict) or not isinstance(spec.get('cmd'), str):
            continue
        tokens = spec['cmd'].split()
        ctx, parallel = _flag(tokens, '--ctx-size', '-c'), _flag(tokens, '--parallel', '-np')
        try:
            override = (LLAMA_OVERRIDES / f'{model_id}.ctx').read_text().strip()
            if override.isdigit() and 4096 <= int(override) <= 262144:
                ctx = override
        except OSError:
            pass
        try:
            parallel = _flag((LLAMA_OVERRIDES / f'{model_id}.args').read_text().split(), '--parallel') or parallel
        except OSError:
            pass
        if ctx.isdigit() and int(ctx) > 0:
            slots = int(parallel) if parallel.isdigit() and int(parallel) > 0 else 1
            found[str(model_id)] = int(ctx) // slots
    return found


def context_config_text(original: str, contexts: dict[str, int]) -> str:
    """Set models.<id>.context_length in the llama-swap custom_providers entries, editing lines so
    the rest of the file (comments, order) is untouched. Only one-line `id: {...}` entries change."""
    lines = original.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == 'custom_providers:'), None)
    if start is None or not contexts:
        return original
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((' ', '#', '-'))), len(lines))
    entries = [i for i in range(start + 1, end) if re.match(r'^\s*- ', lines[i])] + [end]
    for a, b in zip(entries, entries[1:]):
        try:
            entry = yaml.safe_load(''.join(lines[a:b]))
        except yaml.YAMLError:
            continue
        entry = entry[0] if isinstance(entry, list) and entry else {}
        if not isinstance(entry, dict) or not any(str(entry.get('base_url', '')).rstrip('/').endswith(r) for r in LLAMA_ROUTES):
            continue
        head = next((i for i in range(a, b) if re.match(r'^\s+models:\s*$', lines[i])), None)
        if head is None:
            continue
        indent = len(lines[head]) - len(lines[head].lstrip()) + 2
        seen: set[str] = set()
        last = head
        for i in range(head + 1, b):
            line = lines[i]
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) < indent:
                break
            last = i
            match = re.match(r'^(\s+)("?)([^":]+)\2:\s*(\{.*\})\s*$', line)
            if not match or len(match.group(1)) != indent:
                continue
            model_id = match.group(3).strip()
            seen.add(model_id)
            if model_id not in contexts:
                continue
            try:
                value = yaml.safe_load(match.group(4)) or {}
            except yaml.YAMLError:
                continue
            if not isinstance(value, dict) or value.get('context_length') == contexts[model_id]:
                continue
            value['context_length'] = contexts[model_id]
            lines[i] = f"{match.group(1)}{match.group(2)}{match.group(3)}{match.group(2)}: {yaml.safe_dump(value, default_flow_style=True).strip()}\n"
        missing = [m for m in contexts if m not in seen]
        if missing:
            lines[last + 1:last + 1] = [f"{' ' * indent}{json.dumps(m)}: {{context_length: {contexts[m]}}}\n" for m in sorted(missing)]
            return context_config_text(''.join(lines), contexts)   # indices moved: rescan
    result = ''.join(lines)
    parsed = yaml.safe_load(result)
    if not isinstance(parsed, dict) or not isinstance(parsed.get('custom_providers'), list):
        raise LinkError('Hermes configuration could not be validated')
    return result


def sync_context() -> dict:
    """Write llama-swap's context sizes into Hermes; returns what changed (nothing when in sync)."""
    contexts = llama_contexts()
    try:
        original = HERMES_CONFIG.read_text()
    except OSError as exc:
        raise LinkError('Hermes settings are unavailable') from exc
    updated = context_config_text(original, contexts)
    if updated != original:
        atomic_write(HERMES_CONFIG, updated)
    return {'changed': updated != original, 'contexts': contexts}


AGENT_CONFIG = ROCSY_ROOT / 'BunnyBytes/agent/data/config.json'


# Hermes runs on this machine (host network), so llama-swap is reachable directly. Going out through
# the public address and back in through the Cloudflare tunnel adds latency and
# Cloudflare's 120 s response limit (HTTP 524), which long prompts on slow models (Strata) exceed.
TUNNEL_ROUTE = re.compile(r'^(\s*base_url:\s*)["\']?https?://(?!127\.0\.0\.1|localhost)[^\s#"\']+/Llama/v1/?["\']?\s*$', re.M)


def local_route_text(text: str) -> str:
    return TUNNEL_ROUTE.sub(lambda m: f'{m.group(1)}{LLAMA_URL}/v1  # Rocsy: local route, not through the Cloudflare tunnel', text)


def sync_local_route() -> dict:
    """Point every Hermes model route at the public /Llama/v1 back at the local llama-swap."""
    try:
        original = HERMES_CONFIG.read_text()
    except OSError as exc:
        raise LinkError('Hermes settings are unavailable') from exc
    updated = local_route_text(original)
    if updated != original:
        if not isinstance(yaml.safe_load(updated), dict):
            raise LinkError('Hermes configuration could not be validated')
        atomic_write(HERMES_CONFIG, updated)
    return {'changed': updated != original, 'routes': len(TUNNEL_ROUTE.findall(original))}
# Settings -> Agentic "Loop guard" -> Hermes tool_loop_guardrails switches
LOOP_GUARD_FLAGS = {
    'off': {'warnings_enabled': False, 'hard_stop_enabled': False, 'non_interactive_hard_stop_enabled': False},
    'warn': {'warnings_enabled': True, 'hard_stop_enabled': False, 'non_interactive_hard_stop_enabled': False},
    'stop': {'warnings_enabled': True, 'hard_stop_enabled': True, 'non_interactive_hard_stop_enabled': True},
}


def guardrail_config_text(text: str, flags: dict[str, bool]) -> str:
    """Set the top-level tool_loop_guardrails switches, adding the section or keys when missing."""
    lines = text.splitlines(keepends=True)
    head = next((i for i, line in enumerate(lines) if re.match(r'^tool_loop_guardrails:\s*$', line)), None)
    if head is None:
        return text.rstrip('\n') + '\ntool_loop_guardrails:\n' + ''.join(f'  {k}: {str(v).lower()}\n' for k, v in flags.items())
    end = next((i for i in range(head + 1, len(lines)) if lines[i].strip() and not lines[i].startswith(' ')), len(lines))
    todo = dict(flags)
    for i in range(head + 1, end):
        match = re.match(r'^  ([a-z_]+):\s*\S+\s*$', lines[i])
        if match and match.group(1) in todo:
            lines[i] = f'  {match.group(1)}: {str(todo.pop(match.group(1))).lower()}\n'
    lines[head + 1:head + 1] = [f'  {k}: {str(v).lower()}\n' for k, v in todo.items()]
    return ''.join(lines)


def sync_guardrails() -> dict:
    """Write the Agent's loop_guard setting (default off) into Hermes; read per run, no restart needed."""
    try:
        mode = json.loads(AGENT_CONFIG.read_text()).get('loop_guard')
    except (OSError, ValueError):
        mode = None
    mode = mode if mode in LOOP_GUARD_FLAGS else 'off'
    try:
        original = HERMES_CONFIG.read_text()
    except OSError as exc:
        raise LinkError('Hermes settings are unavailable') from exc
    updated = guardrail_config_text(original, LOOP_GUARD_FLAGS[mode])
    if updated != original:
        parsed = yaml.safe_load(updated)
        section = parsed.get('tool_loop_guardrails') if isinstance(parsed, dict) else None
        if not isinstance(section, dict) or any(section.get(k) is not v for k, v in LOOP_GUARD_FLAGS[mode].items()):
            raise LinkError('Hermes loop guard settings could not be validated')
        atomic_write(HERMES_CONFIG, updated)
    # Hermes' own repetition cut-off ("the model fell into a repetition loop") has no config key;
    # the rocsy plugin switches it with this file. Only "stop" may cut a reply off.
    guards = json.dumps({'repetition_guard': mode == 'stop'}) + '\n'
    try:
        guards_changed = HERMES_GUARDS.read_text() != guards
    except OSError:
        guards_changed = True
    if guards_changed:
        atomic_write(HERMES_GUARDS, guards)
    return {'changed': updated != original or guards_changed, 'mode': mode}


# ---- compression trigger ----------------------------------------------------------------------
# Hermes floors its compression trigger at 64,000 tokens (capped to 85% on small windows), so a
# 64K llama-swap model only compressed at ~55.7K of its rough chars/4 estimate - real prompts were
# already ~35% bigger and overflowed before the summary ran. The trigger is min(max(threshold x
# window, 64K), threshold_tokens), so both are written from Settings -> Compression ("at" % of the
# Agent model's real window). A cloud/unknown Agent model gets the ratio only (no token cap).
# The other managed keys come from the same settings; the Settings tab owns them, so their lines
# are rewritten (marked) whatever was there before. A missing Agent setting = the Balanced preset.
COMPRESS_MARK = '# Rocsy: Settings -> Compression'
COMPRESS_DEFAULTS = {'at': 50, 'keep_messages': 20, 'keep_user': 3, 'summary': 20,
                     'trim_at': 30, 'trim_min_chars': 4000, 'tool_rounds': 6}
COMPRESS_REF_WINDOW = 128000   # trimming % for a cloud/unknown window
HERMES_COMPRESSION = HERMES_HOME / 'rocsy-compression.json'   # summary ratio + tail tool rounds, for the rocsy plugin


def compression_values(settings: dict, ctx: int | None) -> dict:
    """Hermes compression.<key> values from the Agent's compression settings (None = remove the key)."""
    c = {**COMPRESS_DEFAULTS, **{k: v for k, v in settings.items() if k in COMPRESS_DEFAULTS and isinstance(v, int)}}
    trim = int((ctx or COMPRESS_REF_WINDOW) * c['trim_at'] / 100)
    return {
        'threshold': round(c['at'] / 100, 2),
        'threshold_tokens': int(ctx * c['at'] / 100) if ctx else None,
        'protect_last_n': c['keep_messages'],
        'min_tail_user_messages': c['keep_user'],
        'proactive_prune_tokens': trim,
        'proactive_prune_min_result_chars': c['trim_min_chars'],
    }


def compression_config_text(text: str, values: dict) -> str:
    """Set (or with None remove) the managed keys of the top-level compression: section."""
    lines = text.splitlines(keepends=True)
    want = {k: f'  {k}: {v}  {COMPRESS_MARK}\n' for k, v in values.items() if v is not None}
    head = next((i for i, line in enumerate(lines) if re.match(r'^compression:\s*$', line)), None)
    if head is None:
        return text.rstrip('\n') + '\ncompression:\n' + ''.join(want.values()) if want else text
    end = next((i for i in range(head + 1, len(lines)) if lines[i].strip() and not lines[i].startswith(' ')), len(lines))
    out, done = [], set()
    for line in lines[head + 1:end]:
        key = re.match(r'^  ([a-z_]+):', line)
        key = key.group(1) if key else None
        if key in values:
            if key in want and key not in done:
                out.append(want[key])
            done.add(key)
            continue
        out.append(line)
    lines[head + 1:end] = [want[k] for k in want if k not in done] + out
    return ''.join(lines)


BACKUP_MARK = '# Rocsy: Settings -> Agentic backup model'
AUX_ROUTE_LINES = ('model', 'base_url', 'api_key', 'key_env', 'api_key_env')


def aux_compression_text(text: str, backup: dict | None) -> str:
    """auxiliary.compression = the session's own model (provider auto: Hermes passes the live run's
    model, route and key, so nothing else loads into VRAM), plus the Agentic backup model as a
    one-line fallback_chain. A fallback_chain the user wrote by hand (unmarked) is left alone."""
    lines = text.splitlines(keepends=True)
    aux = next((i for i, line in enumerate(lines) if re.match(r'^auxiliary:\s*$', line)), None)
    if aux is None:
        lines += ['auxiliary:\n']
        aux = len(lines) - 1
    aux_end = next((i for i in range(aux + 1, len(lines)) if lines[i].strip() and not lines[i].startswith(' ')), len(lines))
    head = next((i for i in range(aux + 1, aux_end) if re.match(r'^  compression:\s*$', lines[i])), None)
    if head is None:
        lines[aux + 1:aux + 1] = ['  compression:\n']
        head, aux_end = aux + 1, aux_end + 1
    end = next((i for i in range(head + 1, aux_end) if lines[i].strip() and not lines[i].startswith('    ')), aux_end)
    out, has_provider, chain_done = [], False, False
    want_chain = (f'    fallback_chain: [{json.dumps({"provider": backup["provider"], "model": backup["model"]})}]  {BACKUP_MARK}\n'
                  if backup else None)
    for i in range(head + 1, end):
        line = lines[i]
        key = re.match(r'^    ([a-z_]+):', line)
        key = key.group(1) if key else None
        if key == 'provider':
            out.append('    provider: auto\n'); has_provider = True; continue
        if key in AUX_ROUTE_LINES:
            continue                                       # a pinned route would bypass the session's model
        if key == 'fallback_chain':
            if BACKUP_MARK not in line:
                return text                                # hand-written chain: not ours to replace
            chain_done = True
            if want_chain:
                out.append(want_chain)
            continue
        out.append(line)
    if not has_provider:
        out.insert(0, '    provider: auto\n')
    if want_chain and not chain_done:
        out.append(want_chain)
    lines[head + 1:end] = out
    return ''.join(lines)


def sync_compression() -> dict:
    """Settings -> Compression into Hermes: trigger at "at" % of the Agent model's llama-swap
    window, tail/trim keys, plugin constants, and the summarizer on the session's own model."""
    try:
        agent = json.loads(AGENT_CONFIG.read_text())
    except (OSError, ValueError):
        agent = {}
    model = str(agent.get('model') or '')
    ctx = llama_contexts().get(model)
    settings = agent.get('compression') if isinstance(agent.get('compression'), dict) else {}
    values = compression_values(settings, ctx)
    cap = values['threshold_tokens']
    # The Agentic "Backup model (online)" picker is a placeholder for now: no fallback_chain is
    # written (a marked one left from before is removed). aux_compression_text still supports one.
    backup = None
    try:
        original = HERMES_CONFIG.read_text()
    except OSError as exc:
        raise LinkError('Hermes settings are unavailable') from exc
    updated = aux_compression_text(compression_config_text(original, values), backup)
    if updated != original:
        parsed = yaml.safe_load(updated) or {}
        section, aux = parsed.get('compression'), ((parsed.get('auxiliary') or {}).get('compression') or {})
        chain = aux.get('fallback_chain')
        if (not isinstance(section, dict) or any(section.get(k) != v for k, v in values.items())
                or aux.get('provider') != 'auto' or any(aux.get(k) for k in AUX_ROUTE_LINES)
                or (backup and BACKUP_MARK in updated and chain != [backup])):
            raise LinkError('Hermes compression settings could not be validated')
        atomic_write(HERMES_CONFIG, updated)
    c = {**COMPRESS_DEFAULTS, **{k: v for k, v in settings.items() if k in COMPRESS_DEFAULTS and isinstance(v, int)}}
    plugin = json.dumps({'summary_ratio': c['summary'] / 100, 'keep_tool_rounds': c['tool_rounds']}) + '\n'
    try:
        plugin_changed = HERMES_COMPRESSION.read_text() != plugin
    except OSError:
        plugin_changed = True
    if plugin_changed:
        atomic_write(HERMES_COMPRESSION, plugin)
    return {'changed': updated != original or plugin_changed, 'model': model, 'cap': cap, 'backup': backup,
            'preset': settings.get('preset') or 'balanced'}
