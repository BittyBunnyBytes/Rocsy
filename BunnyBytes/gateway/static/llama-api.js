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
