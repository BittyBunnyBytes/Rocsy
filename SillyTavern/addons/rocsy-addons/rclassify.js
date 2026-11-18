// Expression classifier picker (Rocsy Addons). SillyTavern's Expressions extension has fixed APIs: Local (one ONNX
// model from config.yaml), LLM (the chat connection), WebLLM, Extras, None. This adds Rocsy's own:
//   - onnx:     any ONNX emotion model (server/rclassify.mjs runs it in its own transformers pipeline, CPU),
//   - llm:      any llama-swap model, held to the label list by a grammar,
//   - decision: a decision model (Laya, Jev-Style, OpenJev, Lev, Kev…) through llama.cpp's /v1/systemone,
//   - pseudo:   a "Pseudo Emotion Model": your own labels (with descriptions) run on one of the three above; on ONNX
//               each label is made from the model's labels (e.g. smug = pride + amusement).
// SillyTavern stays on "Local" and its /api/extra/classify(/labels) calls go to /api/plugins/rocsy/classify.
// Exposed through RocsyImage (rimage.js): classifierPicker, classifierLabels.

const API = '/api/plugins/rocsy';
const MODULE = 'rocsy-addons';
const ctx = () => globalThis.SillyTavern.getContext();
const EXPRESSION_API = {local: 0, extras: 1, llm: 2, webllm: 3, none: 99};
// SillyTavern's default labels (GoEmotions): the LLM / decision classifiers' list, plus any custom expressions.
const GO = ['admiration', 'amusement', 'anger', 'annoyance', 'approval', 'caring', 'confusion', 'curiosity', 'desire',
    'disappointment', 'disapproval', 'disgust', 'embarrassment', 'excitement', 'fear', 'gratitude', 'grief', 'joy', 'love', 'nervousness',
    'optimism', 'pride', 'realization', 'relief', 'remorse', 'sadness', 'surprise', 'neutral'];

export const MODES = [
    ['off', 'Off (no expressions)'],
    ['st', 'SillyTavern local (config.yaml model)'],
    ['onnx', 'Rocsy: ONNX model (CPU)'],
    ['llm', 'Rocsy: llama-swap model'],
    ['decision', 'Rocsy: decision model (Laya, Jev…)'],
    ['pseudo', 'Pseudo emotion model (your own labels)'],
    ['chat', 'Chat connection (SillyTavern LLM mode)'],
    ['webllm', 'WebLLM (in this browser)'],
];
const ROCSY_MODES = ['onnx', 'llm', 'decision', 'pseudo'];
const ENGINES = [['llm', 'llama-swap model'], ['decision', 'Decision model (Laya, Jev…)'], ['onnx', 'ONNX model (CPU)']];

// Starting points for a pseudo model. `from` = the GoEmotions labels each one is made of (used on ONNX models).
const PRESETS = {
    simple: {name: 'Simple moods', labels: [
        ['happy', 'cheerful, pleased, laughing, affectionate', 'joy amusement excitement optimism gratitude love relief pride admiration approval caring'],
        ['sad', 'hurt, disappointed, grieving, regretful', 'sadness grief disappointment remorse'],
        ['angry', 'mad, annoyed, disgusted, disapproving', 'anger annoyance disapproval disgust'],
        ['scared', 'afraid, anxious, nervous', 'fear nervousness'],
        ['surprised', 'shocked, realizing something, confused', 'surprise realization confusion curiosity'],
        ['flustered', 'embarrassed, shy, blushing, longing', 'embarrassment desire'],
        ['neutral', 'calm, matter-of-fact, no strong feeling', 'neutral'],
    ]},
    ekman: {name: 'Ekman 7', labels: [
        ['anger', 'angry, irritated, hostile', 'anger annoyance disapproval'], ['disgust', 'repulsed, grossed out', 'disgust'],
        ['fear', 'afraid, anxious', 'fear nervousness'], ['joy', 'happy, delighted, amused', 'joy amusement excitement optimism gratitude love relief pride admiration approval caring'],
        ['neutral', 'no strong feeling', 'neutral'], ['sadness', 'sad, hurt, grieving', 'sadness grief disappointment remorse embarrassment'],
        ['surprise', 'surprised, realizing, confused', 'surprise realization confusion curiosity'],
    ]},
    roleplay: {name: 'Roleplay moods', labels: [
        ['smug', 'self-satisfied, teasing, cocky grin', 'pride amusement'],
        ['playful', 'joking, flirty, mischievous', 'amusement excitement'],
        ['affectionate', 'warm, loving, tender', 'love caring gratitude admiration'],
        ['happy', 'cheerful, glad', 'joy optimism relief approval'],
        ['flustered', 'embarrassed, blushing, caught off guard', 'embarrassment nervousness desire'],
        ['pouting', 'sulking, mildly annoyed, disappointed', 'annoyance disappointment disapproval'],
        ['angry', 'furious, shouting', 'anger disgust'],
        ['sad', 'hurt, crying, grieving', 'sadness grief remorse'],
        ['scared', 'frightened, panicking', 'fear'],
        ['shocked', 'stunned, surprised', 'surprise realization'],
        ['thinking', 'curious, puzzled, considering', 'curiosity confusion'],
        ['neutral', 'calm, plain', 'neutral'],
    ]},
    goemotions: {name: 'GoEmotions (28)', labels: GO.map(l => [l, '', l])},
    blank: {name: 'New pseudo model', labels: [['happy', '', 'joy'], ['neutral', '', 'neutral']]},
};
const fromPreset = key => ({name: PRESETS[key].name, labels: PRESETS[key].labels.map(([label, description, from]) => ({label, description, from: from.split(' ')}))});

export const classifier = () => {
    const all = ctx().extensionSettings[MODULE] ||= {};
    const c = all.classifier ||= {};
    if (!c.mode) {   // first look: follow what SillyTavern is set to
        const api = ctx().extensionSettings.expressions?.api;
        c.mode = api === EXPRESSION_API.local ? 'st' : api === EXPRESSION_API.llm ? 'chat' : api === EXPRESSION_API.webllm ? 'webllm' : 'off';
    }
    c.onnx ||= 'Cohee/roberta-base-go_emotions-onnx';
    c.llm ??= '';
    c.decision ??= '';
    if (!Array.isArray(c.pseudo) || !c.pseudo.length) c.pseudo = [{id: 'p1', engine: 'llm', model: '', instructions: '', ...fromPreset('roleplay')}];
    if (!c.pseudo.some(p => p.id === c.pseudoId)) c.pseudoId = c.pseudo[0].id;
    return c;
};
const currentPseudo = () => { const c = classifier(); return c.pseudo.find(p => p.id === c.pseudoId) || c.pseudo[0]; };
const rerouted = () => ROCSY_MODES.includes(classifier().mode) && ctx().extensionSettings.expressions?.api === EXPRESSION_API.local;
const chatModel = () => globalThis.RocsyAddons?.chatModel?.() || ctx().chatCompletionSettings?.custom_model || '';
const customLabels = () => (ctx().extensionSettings.expressions?.custom || []).map(String);

function requestBody(extra = {}) {
    const c = classifier();
    if (c.mode === 'pseudo') {
        const p = currentPseudo();
        return JSON.stringify({backend: 'pseudo', pseudo: {engine: p.engine, model: p.model || (p.engine === 'llm' ? chatModel() : p.engine === 'onnx' ? c.onnx : c.decision),
            instructions: p.instructions, labels: p.labels}, ...extra});
    }
    const model = c.mode === 'llm' ? (c.llm || chatModel()) : c.mode === 'decision' ? c.decision : c.onnx;
    return JSON.stringify({backend: c.mode, model, labels: [...GO, ...customLabels()], ...extra});
}

// Reroute SillyTavern's local classifier calls while a Rocsy classifier is picked.
(() => {
    if (window.__rocsyClassifyWrapped) return;
    window.__rocsyClassifyWrapped = true;
    const original = window.fetch;
    window.fetch = function (input, init) {
        try {
            const url = typeof input === 'string' ? input : input?.url || '';
            const method = (init?.method || input?.method || 'GET').toUpperCase();
            const match = method === 'POST' && rerouted() && /\/api\/extra\/classify(\/labels)?(\?|$)/.exec(new URL(url, location.href).pathname);
            if (match) {
                const headers = {...ctx().getRequestHeaders(), 'Content-Type': 'application/json'};
                if (match[1]) return original.call(this, `${API}/classify/labels`, {method: 'POST', headers, body: requestBody()});
                let text = '';
                try { text = JSON.parse(init?.body || '{}').text || ''; } catch { /* not JSON */ }
                return original.call(this, `${API}/classify`, {method: 'POST', headers, body: requestBody({text})});
            }
        } catch { /* fall through to SillyTavern's own */ }
        return original.apply(this, arguments);
    };
})();

/** Tell SillyTavern's Expressions which API to use, and drop its cached label list. */
export function applyMode() {
    const c = classifier();
    const ext = ctx().extensionSettings;
    ext.expressions ||= {};
    const api = c.mode === 'off' ? EXPRESSION_API.none : c.mode === 'chat' ? EXPRESSION_API.llm : c.mode === 'webllm' ? EXPRESSION_API.webllm : EXPRESSION_API.local;
    ext.expressions.api = api;
    const select = $('#expression_api');
    if (select.length) select.val(String(api)).trigger('change');   // its handler resets the label cache and sprites
    ctx().saveSettingsDebounced();
}
let applyTimer = 0;
const applySoon = () => { clearTimeout(applyTimer); applyTimer = setTimeout(applyMode, 800); };

/** The labels the current classifier answers with, and what each means (for drawing the sprites). */
export async function classifierInfo() {
    const c = classifier();
    try {
        if (ROCSY_MODES.includes(c.mode)) {
            const response = await fetch(`${API}/classify/labels`, {method: 'POST', headers: {...ctx().getRequestHeaders(), 'Content-Type': 'application/json'}, body: requestBody()});
            if (response.ok) {
                const data = await response.json();
                return {labels: [...new Set([...data.labels, ...(c.mode === 'pseudo' ? [] : customLabels())])], descriptions: data.descriptions || {}};
            }
        }
    } catch { /* fall back */ }
    return {labels: [...new Set([...GO, ...customLabels()])], descriptions: {}};
}
export const classifierLabels = async () => (await classifierInfo()).labels;

export function classifierName() {
    const c = classifier();
    return c.mode === 'onnx' ? c.onnx.split('/').pop() : c.mode === 'llm' ? `llama-swap ${c.llm || 'chat model'}` : c.mode === 'decision' ? `decision model ${c.decision || '(none picked)'}`
        : c.mode === 'pseudo' ? `pseudo model “${currentPseudo().name}”` : {off: 'off', st: 'SillyTavern local', chat: 'chat connection', webllm: 'WebLLM'}[c.mode] || c.mode;
}

/** The picker (Rocsy Addons → Image, and the Expressions settings page). el/icon = rimage.js's helpers. */
export function classifierPicker(el, icon) {
    const box = el('div', {class: 'rim rim-classifier'});
    let models = null;
    const muted = text => el('small', {class: 'rvo-muted rad-muted', text});
    const row = (label, ...control) => el('label', {class: 'rim-row'}, el('span', {class: 'rim-label', text: label}), ...control);
    const select = (options, value, onchange, extra = {}) => {
        const node = el('select', {class: 'text_pole rvo-einput', onchange: e => onchange(e.target.value), ...extra}, ...options.map(([v, t]) => el('option', {value: v, text: t})));
        if (value && !options.some(([v]) => v === value)) node.prepend(el('option', {value, text: value}));
        node.value = value;
        return node;
    };
    // A decision model: a llama-swap model, or the URL of another systemone server (Jev-Style's `jev-style serve`).
    const decisionControl = (value, onchange) => {
        const isUrl = /^https?:\/\//i.test(value || '');
        const url = el('input', {class: 'text_pole rvo-einput', type: 'url', value: isUrl ? value : '', placeholder: 'http://127.0.0.1:8765 (a systemone server)', hidden: !isUrl,
            onchange: e => { const v = e.target.value.trim(); if (/^https?:\/\/\S+$/i.test(v)) onchange(v); else toastr.warning('Use a http(s):// URL'); }});
        const pick = select([['', 'Pick a decision model…'], ...llamaOptions(true), ['__url', 'Another systemone server (URL)…']], isUrl ? '__url' : value, v => {
            if (v === '__url') { url.hidden = false; url.focus(); return; }
            onchange(v);
        });
        if (isUrl) pick.value = '__url';
        return [pick, url];
    };
    const onnxOptions = () => (models?.onnx || []).map(m => [m.id, `${m.name} · ${m.labels} labels`]);
    const llamaOptions = (decisionFirst = false) => [...(models?.llama || [])].sort((a, b) => decisionFirst ? (b.decision - a.decision) : 0)
        .map(m => [m.id, `${m.name}${m.decision ? ' · decision model' : ''}`]);

    const tryBox = () => {
        const out = el('span', {class: 'rvo-muted rad-muted'});
        const input = el('input', {class: 'text_pole rvo-einput', type: 'text', value: 'Hmph. Fine, I’ll help you… but only because you asked so nicely.'});
        const run = el('button', {class: 'menu_button rvo-cbtn', type: 'button', onclick: async () => {
            out.textContent = 'Classifying…';
            try {
                const response = await fetch(`${API}/classify`, {method: 'POST', headers: {...ctx().getRequestHeaders(), 'Content-Type': 'application/json'}, body: requestBody({text: input.value})});
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.error || `failed (${response.status})`);
                out.textContent = data.classification.slice(0, 3).map(x => `${x.label} ${Math.round(x.score * 100)}%`).join(' · ');
            } catch (error) { out.textContent = error.message; }
        }}, icon('fa-vial'), el('span', {text: 'Try'}));
        return [el('div', {class: 'rim-row'}, input, run), out];
    };

    const pseudoEditor = () => {
        const c = classifier();
        const p = currentPseudo();
        const changed = (redraw = false) => { ctx().saveSettingsDebounced(); applySoon(); if (redraw) draw(); };
        const addModel = preset => {
            const id = `p${Date.now().toString(36)}`;
            c.pseudo.push({id, engine: p.engine, model: p.model, instructions: '', ...fromPreset(preset)});
            c.pseudoId = id;
            changed(true);
        };
        const picker = select(c.pseudo.map(x => [x.id, x.name || 'Untitled']), c.pseudoId, v => { c.pseudoId = v; changed(true); });
        const presetPick = select([['', 'New from…'], ...Object.entries(PRESETS).map(([k, v]) => [k, v.name])], '', v => { if (v) addModel(v); });
        const dup = el('button', {class: 'menu_button rvo-cbtn', type: 'button', title: 'Duplicate', onclick: () => {
            const id = `p${Date.now().toString(36)}`;
            c.pseudo.push({...JSON.parse(JSON.stringify(p)), id, name: `${p.name} (copy)`});
            c.pseudoId = id; changed(true);
        }}, icon('fa-clone'));
        const del = el('button', {class: 'menu_button rvo-cbtn', type: 'button', title: 'Delete this pseudo model', onclick: async () => {
            if (c.pseudo.length < 2) { toastr.info('Keep at least one pseudo model'); return; }
            if (!await ctx().callGenericPopup(`Delete the pseudo model “${p.name}”?`, ctx().POPUP_TYPE?.CONFIRM ?? 2)) return;
            c.pseudo = c.pseudo.filter(x => x.id !== p.id); c.pseudoId = c.pseudo[0].id; changed(true);
        }}, icon('fa-trash-can'));
        const name = el('input', {class: 'text_pole rvo-einput', type: 'text', value: p.name || '', placeholder: 'Name',
            oninput: e => { p.name = e.target.value; ctx().saveSettingsDebounced(); }, onchange: () => draw()});
        const engine = select(ENGINES, p.engine, v => { p.engine = v; p.model = ''; changed(true); });
        const model = p.engine === 'onnx' ? [select(onnxOptions(), p.model || c.onnx, v => { p.model = v; changed(); })]
            : p.engine === 'decision' ? decisionControl(p.model, v => { p.model = v; changed(); })
                : [select([['', `Same as the chat (${chatModel() || 'none yet'})`], ...llamaOptions()], p.model, v => { p.model = v; changed(); })];
        const question = el('input', {class: 'text_pole rvo-einput', type: 'text', value: p.instructions || '', placeholder: 'Which emotion does the speaker of this roleplay message show most strongly?',
            oninput: e => { p.instructions = e.target.value; changed(); }});
        const list = el('div', {class: 'rim-pseudo-labels'});
        const drawLabels = () => {
            list.replaceChildren(el('div', {class: 'rim-pseudo-head'}, el('span', {text: 'Label'}), el('span', {text: 'What it means'}),
                p.engine === 'onnx' ? el('span', {text: 'Made from (model labels)'}) : null, el('span')),
            ...p.labels.map((l, i) => el('div', {class: `rim-pseudo-row ${p.engine === 'onnx' ? 'with-from' : ''}`},
                el('input', {class: 'text_pole rvo-einput', type: 'text', value: l.label, placeholder: 'label', 'aria-label': 'Label',
                    oninput: e => { l.label = e.target.value.trim().toLowerCase().replace(/[^\p{L}\p{N}_ -]/gu, '').slice(0, 40); changed(); }}),
                el('input', {class: 'text_pole rvo-einput', type: 'text', value: l.description || '', placeholder: 'what this looks or sounds like', 'aria-label': 'Description',
                    oninput: e => { l.description = e.target.value.slice(0, 300); changed(); }}),
                p.engine === 'onnx' ? el('input', {class: 'text_pole rvo-einput', type: 'text', value: (l.from || []).join(', '), placeholder: 'joy, amusement', 'aria-label': 'Made from',
                    oninput: e => { l.from = e.target.value.split(/[,\s]+/).map(x => x.trim().toLowerCase()).filter(Boolean); changed(); }}) : null,
                el('button', {class: 'menu_button rvo-cbtn rim-x', type: 'button', title: 'Remove', onclick: () => { p.labels.splice(i, 1); changed(); drawLabels(); }}, icon('fa-xmark')))));
        };
        drawLabels();
        const add = el('button', {class: 'menu_button rvo-cbtn', type: 'button', onclick: () => { p.labels.push({label: '', description: '', from: []}); drawLabels(); list.querySelector('.rim-pseudo-row:last-child input')?.focus(); }},
            icon('fa-plus'), el('span', {text: 'Add label'}));
        const engineNote = p.engine === 'decision' ? 'Decision models (Laya, Jev-Style…) read each label’s meaning and give a probability for each in one pass. Laya runs in llama-swap; Jev-Style through its own server’s URL.'
            : p.engine === 'onnx' ? 'The ONNX model scores its own labels; each of yours adds up the ones it is made from. Descriptions are only used to draw the sprites.'
                : 'The model reads the labels and their meanings and answers with one (a grammar keeps it to the list).';
        return [
            el('div', {class: 'rim-row'}, picker, presetPick, dup, del),
            row('Name', name), row('Runs on', engine), row('Model', model[0]), ...model.slice(1), row('Question', question), muted(engineNote),
            list, el('div', {class: 'rim-row'}, add, muted(`${p.labels.filter(l => l.label).length} labels · the character editor makes a sprite per label`)),
        ];
    };

    const draw = () => {
        const c = classifier();
        const rows = [row('Classifier', select(MODES, c.mode, v => { c.mode = v; applyMode(); draw(); }))];
        if (c.mode === 'onnx') {
            const info = (models?.onnx || []).find(m => m.id === c.onnx);
            const custom = el('input', {class: 'text_pole rvo-einput', type: 'text', placeholder: 'owner/model (transformers.js ONNX, text-classification)', hidden: true,
                onchange: e => { const v = e.target.value.trim(); if (/^[\w.-]+\/[\w.-]+$/.test(v)) { c.onnx = v; applyMode(); draw(); } else toastr.warning('Use owner/model'); }});
            const pick = select([...onnxOptions(), ['__custom', 'Other Hugging Face model…']], c.onnx, v => {
                if (v === '__custom') { custom.hidden = false; custom.focus(); return; }
                c.onnx = v; applyMode(); draw();
            });
            rows.push(row('Model', pick), custom, muted(`${info ? `${info.size}. ` : ''}Runs on the CPU inside SillyTavern; downloaded from Hugging Face the first time it’s used.`));
        } else if (c.mode === 'llm') {
            rows.push(row('Model', select([['', `Same as the chat (${chatModel() || 'none yet'})`], ...llamaOptions()], c.llm, v => { c.llm = v; applyMode(); draw(); })),
                muted('One short request per reply, held to the expression labels. Another model than the chat’s makes llama-swap swap models unless both fit together.'));
        } else if (c.mode === 'decision') {
            const [pick, url] = decisionControl(c.decision, v => { c.decision = v; applyMode(); draw(); });
            rows.push(row('Model', pick), url,
                muted('A decision model gives a probability for every expression in one pass. Laya runs in llama-swap (CPU, next to the chat model). Jev-Style needs its own server (`jev-style serve`): pick “Another systemone server” and give its URL. For your own labels, use a pseudo emotion model.'));
        } else if (c.mode === 'pseudo') {
            rows.push(...pseudoEditor());
        } else if (c.mode === 'chat') {
            rows.push(muted('SillyTavern asks the chat connection itself after each reply.'));
        } else if (c.mode === 'st') {
            rows.push(muted('SillyTavern’s built-in local classifier (DistilBERT GoEmotions unless config.yaml names another).'));
        } else if (c.mode === 'webllm') {
            rows.push(muted('Needs SillyTavern’s WebLLM extension and a WebGPU browser.'));
        }
        if (ROCSY_MODES.includes(c.mode)) rows.push(...tryBox());
        box.replaceChildren(...rows);
    };
    draw();
    fetch(`${API}/classify/models`, {headers: ctx().getRequestHeaders()}).then(r => r.ok ? r.json() : null).then(data => {
        if (data && box.isConnected !== false) { models = data; draw(); }
    }).catch(() => {});
    return box;
}
