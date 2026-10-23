/* Advanced workflow card (Image, Video, Music): per-workflow model-file overrides.
   Backend: shared/workflow_models.py, served by each studio at <prefix>/api/workflow-models.
   Mount: an element with data-workflow-models="<api url>" inside the studio's own card
   markup (so it keeps that studio's look); an optional [data-workflow-models-badge]
   shows how many files the shown workflow overrides. The shown workflow follows the
   studio's model control (#model select or input[name=model] radios). */
(() => {
  const mount = document.querySelector('[data-workflow-models]');
  if (!mount) return;
  const API = mount.dataset.workflowModels;
  const badge = document.querySelector('[data-workflow-models-badge]');
  let state = null;
  let shown = '';
  let lastModel = '';
  let loading = null;

  function h(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, val] of Object.entries(props)) {
      if (val === undefined || val === null || val === false) continue;
      if (key === 'class') node.className = val;
      else if (key === 'text') node.textContent = val;
      else if (key === 'value') node.value = val;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), val);
      else node.setAttribute(key, val === true ? '' : val);
    }
    node.append(...children.flat().filter(child => child !== null && child !== undefined && child !== false));
    return node;
  }

  function studioModel() {
    const radio = document.querySelector('input[name="model"]:checked');
    return radio ? radio.value : (document.getElementById('model')?.value || '');
  }

  function workflowFor(model) {
    const key = state?.aliases?.[model] || model;
    return state?.workflows.some(item => item.key === key) ? key : (state?.workflows[0]?.key || '');
  }

  async function load() {
    loading ??= fetch(`${API}?model=${encodeURIComponent(studioModel())}`, { headers: { Accept: 'application/json' } })
      .then(async response => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        state = await response.json();
      })
      .finally(() => { loading = null; });
    await loading;
  }

  async function save(body) {
    const response = await fetch(API, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || (typeof data.detail === 'string' ? data.detail : '') || data.error?.message || `HTTP ${response.status}`);
    state = data;
  }

  function updateBadge(workflow) {
    if (!badge) return;
    badge.textContent = workflow?.overridden ? `${workflow.overridden} changed` : '';
  }

  function slotRow(workflow, slot) {
    const status = h('span', { class: 'wm-status', role: 'status' });
    const select = h('select', { id: `wm-${workflow.key}-${slot.id}`.replace(/[^A-Za-z0-9_-]/g, '_') });
    select.append(h('option', { value: '', text: `Default · ${slot.default}${slot.default_missing ? ' (missing in ComfyUI)' : ''}` }));
    const choices = slot.choices || [];
    if (slot.override && !choices.includes(slot.override)) {
      select.append(h('option', { value: slot.override, text: `${slot.override} (missing in ComfyUI)` }));
    }
    for (const file of choices) {
      if (file !== slot.default) select.append(h('option', { value: file, text: file }));
    }
    select.value = slot.override || '';
    select.disabled = !slot.choices && !slot.override;
    select.addEventListener('change', async () => {
      select.disabled = true;
      status.textContent = 'Saving…';
      try {
        await save({ workflow: workflow.key, slot: slot.id, file: select.value || null });
        render();
      } catch (error) {
        status.textContent = error.message;
        select.value = slot.override || '';
        select.disabled = false;
      }
    });
    const row = h('div', { class: `field-row wm-row${slot.override ? ' is-overridden' : ''}` },
      h('label', { for: select.id }, slot.label, h('span', { class: 'wm-class', text: ` ${slot.class_type}` })),
      select, status);
    return row;
  }

  function render() {
    if (!state) return;
    const workflow = state.workflows.find(item => item.key === shown) || state.workflows[0];
    if (!workflow) {
      mount.replaceChildren(h('p', { class: 'wm-note', text: 'No workflows to configure.' }));
      return;
    }
    shown = workflow.key;
    const picker = h('select', { id: 'wm-workflow' },
      state.workflows.map(item => h('option', { value: item.key, text: item.overridden ? `${item.label} (${item.overridden} changed)` : item.label })));
    picker.value = shown;
    picker.addEventListener('change', () => { shown = picker.value; render(); });
    const reset = h('button', { type: 'button', class: 'secondary-button wm-reset', text: 'Reset to defaults', disabled: !workflow.overridden });
    reset.addEventListener('click', async () => {
      reset.disabled = true;
      try { await save({ workflow: workflow.key, reset: true }); render(); } catch (error) { alert(error.message); reset.disabled = false; }
    });
    mount.replaceChildren(...[
      h('p', { class: 'wm-note' },
        'Swap the model files this workflow loads. Saved on the server and used for every render of the workflow, Agentic included. ',
        'Only the workflow\'s default file is replaced, so checkpoint menus and other per-render pickers still win.'),
      state.online ? null : h('p', { class: 'wm-note wm-warn', text: 'ComfyUI is offline, so the file lists are empty. Saved changes still apply.' }),
      h('div', { class: 'field-row wm-workflow-row' }, h('label', { for: 'wm-workflow', text: 'Workflow' }), picker),
      workflow.note ? h('p', { class: 'wm-note', text: workflow.note }) : null,
      h('div', { class: 'wm-slots' }, workflow.slots.map(slot => slotRow(workflow, slot))),
      h('div', { class: 'wm-actions' }, reset),
    ].filter(Boolean));
    updateBadge(workflow);
  }

  function followModel() {
    const model = studioModel();
    if (model === lastModel || !state) return;
    lastModel = model;
    shown = workflowFor(model);
    render();
  }

  // Model controls: radios/select fire change; Music's engine buttons set the hidden select on click.
  document.addEventListener('change', event => { if (event.target?.name === 'model' || event.target?.id === 'model') followModel(); });
  document.addEventListener('click', () => setTimeout(followModel, 0));
  // Refresh file lists when the card is opened (new downloads show up without a reload).
  mount.closest('details')?.addEventListener('toggle', event => {
    if (event.target.open) load().then(render).catch(() => {});
  });

  load().then(() => {
    lastModel = studioModel();
    shown = workflowFor(lastModel);
    render();
  }).catch(error => {
    mount.replaceChildren(h('p', { class: 'wm-note wm-warn', text: `Couldn't load workflow settings: ${error.message}` }));
  });
})();
