// Llama API page (/llama-api): connection details plus copy-ready snippets.
let config;
const el = id => document.getElementById(id);
const yamlQuote = value => JSON.stringify(value);

const SNIPPETS = {
  hermes: {
    note: "Paste under model_aliases: in your Hermes config, with LLAMA_API_KEY set in Hermes' environment.",
    text: (model, alias) => `${alias}:\n  model: ${yamlQuote(model)}\n  provider: custom\n  base_url: ${yamlQuote(config.base_url)}\n  key_env: LLAMA_API_KEY`,
  },
  env: {
    note: 'For apps that read the standard OpenAI environment variables.',
    text: model => `OPENAI_BASE_URL=${config.base_url}\nOPENAI_API_KEY=${config.api_key}\nOPENAI_MODEL=${model}`,
  },
  curl: {
    note: 'A quick test from any terminal. The first request loads the model, so it can take a moment.',
    text: model => `curl ${config.base_url}/chat/completions \\\n  -H "Authorization: Bearer ${config.api_key}" \\\n  -H "Content-Type: application/json" \\\n  -d '${JSON.stringify({model, messages: [{role: 'user', content: 'Hello!'}]})}'`,
  },
  python: {
    note: 'Uses the official openai package (pip install openai).',
    text: model => `from openai import OpenAI\n\nclient = OpenAI(\n    base_url=${yamlQuote(config.base_url)},\n    api_key=${yamlQuote(config.api_key)},\n)\nreply = client.chat.completions.create(\n    model=${yamlQuote(model)},\n    messages=[{"role": "user", "content": "Hello!"}],\n)\nprint(reply.choices[0].message.content)`,
  },
};
let snippet = 'hermes';

function status(message, kind = '') {
  el('status').textContent = message;
  el('status').dataset.kind = kind;
}

function refresh() {
  const model = el('model').value;
  const alias = `llama-${model.replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '').toLowerCase()}`;
  el('snippet').textContent = SNIPPETS[snippet].text(model, alias);
  el('snippet-note').textContent = SNIPPETS[snippet].note;
  document.querySelectorAll('[data-snippet]').forEach(tab => {
    const selected = tab.dataset.snippet === snippet;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
}

async function copyFrom(id, button) {
  try {
    await navigator.clipboard.writeText(el(id).textContent);
  } catch {
    return status('Copy failed: select the text and copy it manually.', 'error');
  }
  const old = button.textContent;
  button.textContent = 'Copied';
  button.classList.add('is-done');
  setTimeout(() => { button.textContent = old; button.classList.remove('is-done'); }, 1200);
}

async function init() {
  const response = await fetch('/llama-api/config');
  if (!response.ok) throw new Error('Could not load API settings.');
  config = await response.json();
  el('base-url').textContent = config.base_url;
  el('api-key').textContent = config.api_key;
  const select = el('model');
  select.replaceChildren(...config.models.map(model => new Option(model, model)));
  select.disabled = config.models.length === 0;
  const remembered = (() => { try { return localStorage.getItem('llama-api-model'); } catch { return null; } })();
  if (config.models.includes(remembered)) select.value = remembered;
  el('count').hidden = false;
  el('count').textContent = `${config.models.length} model${config.models.length === 1 ? '' : 's'}`;

  select.addEventListener('change', () => {
    try { localStorage.setItem('llama-api-model', select.value); } catch {}
    refresh();
  });
  el('reveal').addEventListener('click', () => {
    const hidden = el('api-key').classList.toggle('is-secret');
    el('reveal').textContent = hidden ? 'Reveal' : 'Hide';
    el('reveal').setAttribute('aria-pressed', String(!hidden));
  });
  document.querySelectorAll('[data-copy]').forEach(button =>
    button.addEventListener('click', () => copyFrom(button.dataset.copy, button)));
  const tabs = [...document.querySelectorAll('[data-snippet]')];
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => { snippet = tab.dataset.snippet; refresh(); });
    tab.addEventListener('keydown', event => {
      const step = {ArrowRight: 1, ArrowLeft: -1}[event.key];
      if (!step) return;
      const next = tabs[(index + step + tabs.length) % tabs.length];
      snippet = next.dataset.snippet;
      refresh();
      next.focus();
    });
  });
  refresh();
}

init().catch(error => status(error.message, 'error'));

// ---- Keys for other apps (/llama-api/keys) ----
const fmtTime = t => t ? new Date(t * 1000).toLocaleString() : 'never';
const limitText = (n, unit) => n ? `${n}/${unit}` : `no ${unit} limit`;

async function keyAction(url, method = 'POST', body) {
  const response = await fetch(url, {
    method,
    headers: {'X-BB-Action': '1', ...(body ? {'Content-Type': 'application/json'} : {})},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function renderKeys(keys) {
  const list = el('key-list');
  if (!keys.length) {
    const empty = document.createElement('li');
    empty.className = 'bb-key-empty';
    empty.textContent = 'No extra keys yet.';
    return list.replaceChildren(empty);
  }
  list.replaceChildren(...keys.map(key => {
    const item = document.createElement('li');
    item.className = 'bb-key-item' + (key.active ? '' : ' is-revoked');
    const top = document.createElement('div');
    top.className = 'bb-key-top';
    const title = document.createElement('div');
    const name = document.createElement('span');
    name.className = 'bb-key-name';
    name.textContent = key.name;
    const hint = document.createElement('span');
    hint.className = 'bb-key-hint';
    hint.textContent = key.hint;
    title.append(name, hint);
    if (!key.active) {
      const badge = document.createElement('span');
      badge.className = 'bb-key-badge';
      badge.textContent = 'revoked';
      title.append(' ', badge);
    }
    const buttons = document.createElement('div');
    buttons.className = 'bb-key-buttons';
    if (key.active) {
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'bb-api-button';
      edit.textContent = 'Edit';
      edit.addEventListener('click', () => openKeyForm(key));
      const rotate = document.createElement('button');
      rotate.type = 'button';
      rotate.className = 'bb-api-button';
      rotate.textContent = 'New secret';
      rotate.title = 'Same settings, new key; the old one stops working at once';
      rotate.addEventListener('click', async () => {
        if (!confirm(`Give "${key.name}" a new secret? The current one stops working at once, so update the app using it.`)) return;
        try {
          const data = await keyAction(`/llama-api/keys/${key.id}/rotate`);
          showToken(data.token);
          loadKeys();
        } catch (error) { status(error.message, 'error'); }
      });
      buttons.append(edit, rotate);
      const revoke = document.createElement('button');
      revoke.type = 'button';
      revoke.className = 'bb-api-button';
      revoke.textContent = 'Revoke';
      revoke.addEventListener('click', async () => {
        if (!confirm(`Revoke "${key.name}"? Anything using it stops working at once.`)) return;
        try { await keyAction(`/llama-api/keys/${key.id}/revoke`); loadKeys(); }
        catch (error) { status(error.message, 'error'); }
      });
      buttons.append(revoke);
    }
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'bb-api-button';
    del.textContent = 'Delete';
    del.addEventListener('click', async () => {
      if (!confirm(`Delete "${key.name}" for good?`)) return;
      try { await keyAction(`/llama-api/keys/${key.id}`, 'DELETE'); loadKeys(); }
      catch (error) { status(error.message, 'error'); }
    });
    buttons.append(del);
    top.append(title, buttons);
    const meta = document.createElement('p');
    meta.className = 'bb-key-meta';
    meta.textContent = [
      `Models: ${key.models.length ? key.models.join(', ') : 'all'}`,
      `context ${key.max_context} tokens, replies up to ${key.max_output}`,
      `${limitText(key.per_minute, 'minute')}, ${limitText(key.per_day, 'day')}`,
      key.allow_images ? `vision (up to ${key.max_images} picture${key.max_images === 1 ? '' : 's'})` : 'no vision',
      key.allow_audio ? 'audio' : 'no audio',
      key.require_loaded ? 'only when already loaded' : 'may load the model',
      ...(key.character ? [`character ${key.character} (live card)`] : []),
      ...(key.thinking === false ? ['thinking off'] : []),
      ...(key.system_extra ? ['+ system prompt additions'] : []),
    ].join(' · ') + `\nUsed ${key.uses_day} today, ${key.uses_total} in total · last used ${fmtTime(key.last_used)}` +
      ` · created ${fmtTime(key.created)}` + (key.active ? '' : ` · revoked ${fmtTime(key.revoked)}`);
    meta.style.whiteSpace = 'pre-line';
    item.append(top, meta);
    return item;
  }));
}

async function loadKeys() {
  const response = await fetch('/llama-api/keys');
  if (!response.ok) return status('Could not load keys.', 'error');
  const data = await response.json();
  stCharacters = data.characters || [];
  renderKeys(data.keys);
}

let editingId = null;   // null = the form creates a new key
let stCharacters = [];  // SillyTavern card names (GET /llama-api/keys)

function showToken(token) {
  el('key-token').textContent = token;
  el('key-created').hidden = false;
  el('key-created').scrollIntoView({block: 'nearest'});
}

function openKeyForm(key = null) {
  editingId = key ? key.id : null;
  const box = el('kf-models');
  // Models the key already allows stay listed even if llama-swap no longer has them.
  const models = [...new Set([...(config?.models || []), ...(key?.models || [])])];
  box.replaceChildren(...models.map(model => {
    const label = document.createElement('label');
    const tick = document.createElement('input');
    tick.type = 'checkbox';
    tick.value = model;
    tick.checked = !!key && key.models.includes(model);
    label.append(tick, ' ', model);
    return label;
  }));
  el('kf-name').value = key ? key.name : '';
  el('kf-ctx').value = key ? key.max_context : 4096;
  el('kf-out').value = key ? key.max_output : 512;
  el('kf-rpm').value = key ? key.per_minute : 6;
  el('kf-rpd').value = key ? key.per_day : 300;
  el('kf-images').checked = !!key?.allow_images;
  el('kf-maximg').value = key?.max_images ?? 4;
  el('kf-audio').checked = !!key?.allow_audio;
  el('kf-loaded').checked = !!key?.require_loaded;
  const pick = el('kf-character');
  const names = [...new Set([...stCharacters, ...(key?.character ? [key.character] : [])])];
  pick.replaceChildren(pick.options[0], ...names.map(name => new Option(name, name)));
  pick.value = key?.character || '';
  el('kf-thinking').checked = key ? key.thinking !== false : true;
  el('kf-extra').value = key?.system_extra || '';
  el('kf-submit').textContent = key ? 'Save changes' : 'Create key';
  el('key-created').hidden = true;
  el('key-form').hidden = false;
  el('key-form').scrollIntoView({block: 'nearest'});
  el('kf-name').focus();
}

el('key-new').addEventListener('click', () => openKeyForm());
el('kf-cancel').addEventListener('click', () => { el('key-form').hidden = true; });
el('key-form').addEventListener('submit', async event => {
  event.preventDefault();
  const spec = {
    name: el('kf-name').value,
    models: [...el('kf-models').querySelectorAll('input:checked')].map(box => box.value),
    max_context: Number(el('kf-ctx').value),
    max_output: Number(el('kf-out').value),
    per_minute: Number(el('kf-rpm').value),
    per_day: Number(el('kf-rpd').value),
    allow_images: el('kf-images').checked,
    max_images: Number(el('kf-maximg').value),
    allow_audio: el('kf-audio').checked,
    require_loaded: el('kf-loaded').checked,
    character: el('kf-character').value,
    thinking: el('kf-thinking').checked,
    system_extra: el('kf-extra').value,
  };
  try {
    if (editingId) {
      await keyAction(`/llama-api/keys/${editingId}`, 'PATCH', spec);
      status('Key saved. The new limits apply to its next request.', 'ok');
    } else {
      showToken((await keyAction('/llama-api/keys', 'POST', spec)).token);
    }
    el('key-form').hidden = true;
    editingId = null;
    loadKeys();
  } catch (error) {
    status(error.message, 'error');
  }
});
loadKeys();
