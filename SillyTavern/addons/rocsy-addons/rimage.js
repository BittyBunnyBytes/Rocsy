// Rocsy Image + Rocsy Voice tools for one character (used by the Rocsy layout's character editor), and the
// hookup of SillyTavern's own Image Generation to Rocsy Image (the server plugin's A1111 bridge).
// Server side: server/rimage.mjs and server/rvoice.mjs. Exposed as globalThis.RocsyImage.

import {classifier, classifierPicker, classifierLabels, classifierInfo, classifierName, applyMode} from './rclassify.js';

const API = '/api/plugins/rocsy';
const BRIDGE = 'http://127.0.0.1:8146';
const ctx = () => globalThis.SillyTavern.getContext();
const MODULE = 'rocsy-addons';

function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
        else if (value !== false && value !== undefined && value !== null) node.setAttribute(key, value === true ? '' : value);
    }
    node.append(...children.filter(child => child !== null && child !== undefined && child !== false));
    return node;
}
const icon = name => el('i', {class: `fa-solid ${name} fa-fw`, 'aria-hidden': 'true'});
const button = (label, iconName, onclick, extra = {}) => el('button', {class: `rvo-cbtn ${extra.primary ? 'primary' : ''}`, type: 'button', title: extra.title || label, onclick},
    icon(iconName), el('span', {text: label}));
/** replaceChildren without the null/false placeholders (they'd show as text). */
const fill = (node, ...children) => node.replaceChildren(...children.filter(c => c !== null && c !== undefined && c !== false));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function api(path, options = {}) {
    const headers = {...ctx().getRequestHeaders(), ...(options.headers || {})};
    if (options.raw) delete headers['Content-Type'];
    const response = await fetch(API + path, {...options, headers, cache: 'no-cache'});
    if (response.status === 404 && !/\/task\//.test(path)) throw new Error('The Rocsy Addons server part is out of date: reinstall Rocsy Addons (Core Backend → SillyTavern), which restarts SillyTavern.');
    if (!response.ok) {
        let message = `Request failed (${response.status})`;
        try { message = (await response.json()).error || message; } catch { /* not JSON */ }
        throw new Error(message);
    }
    return response.headers.get('content-type')?.includes('json') ? response.json() : response.blob();
}
const post = (path, body) => api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});

// Stylesheet next to this file (the manifest has one CSS file).
(() => {
    if (document.getElementById('rim-style')) return;
    document.head.append(el('link', {id: 'rim-style', rel: 'stylesheet', href: new URL('./rimage.css', import.meta.url).href}));
})();

const store = () => {
    const all = ctx().extensionSettings[MODULE] ||= {};
    return all.rimage ||= {avatarModel: 'qwen_image_21', count: 2, transparent: true, sdSetup: false};
};
const saveStore = () => ctx().saveSettingsDebounced();
const chatModel = () => globalThis.RocsyAddons?.chatModel?.() || ctx().chatCompletionSettings?.custom_model || '';

const cardOf = ch => ({
    name: ch.name, description: ch.description || ch.data?.description, personality: ch.personality || ch.data?.personality,
    scenario: ch.scenario || ch.data?.scenario, creator_notes: ch.data?.creator_notes || ch.creatorcomment, first_mes: ch.first_mes,
    mes_example: ch.mes_example, tags: (ctx().tagMap?.[ch.avatar] || []).map(id => ctx().tags?.find(t => t.id === id)?.name).filter(Boolean),
});

let info = null;
async function imageInfo(force = false) {
    if (!info || force) info = await api('/image');
    return info;
}

// ------------------------------------------------------------------------- picture --

async function useAsPicture(ch, blob) {
    const form = new FormData();
    form.append('avatar', new File([blob], 'rocsy.png', {type: 'image/png'}));
    form.append('avatar_url', ch.avatar);
    const headers = ctx().getRequestHeaders();
    delete headers['Content-Type'];
    const response = await fetch('/api/characters/edit-avatar', {method: 'POST', body: form, headers});
    if (!response.ok) throw new Error(`SillyTavern answered ${response.status}`);
    await fetch(`/characters/${encodeURIComponent(ch.avatar)}`, {cache: 'reload'}).catch(() => {});
    await fetch(ctx().getThumbnailUrl('avatar', ch.avatar), {cache: 'reload'}).catch(() => {});
}

async function waitTask(id, onUpdate, isAlive) {
    for (;;) {
        if (!isAlive()) return null;
        let task;
        try { task = await api(`/image/task/${id}`); } catch (error) { onUpdate({state: 'failed', error: error.message, stage: 'Lost', items: []}); return null; }
        onUpdate(task);
        if (task.state !== 'running') return task;
        await sleep(2500);
    }
}

/** The editor's "Picture & expressions" card. onPicture() runs after the card got a new picture. */
function pictureTools(ch, {onPicture} = {}) {
    const box = el('div', {class: 'rim'});
    const alive = () => box.isConnected;
    const s = store();
    let status = null;

    const pictureBox = el('div', {class: 'rim-block'});
    const exprBox = el('div', {class: 'rim-block'});
    box.append(pictureBox, exprBox);

    // --- picture
    const drawPicture = (stock) => {
        const model = el('select', {class: 'rvo-einput', onchange: e => { s.avatarModel = e.target.value; saveStore(); }},
            ...(info?.models || []).filter(m => m.ready).map(m => el('option', {value: m.id, text: m.title.replace(/^Rocsy · /, '')})));
        model.value = s.avatarModel;
        if (!model.value && model.options.length) model.value = model.options[0].value;
        const count = el('select', {class: 'rvo-einput rim-count', title: 'How many to choose from', onchange: e => { s.count = Number(e.target.value); saveStore(); }},
            ...[1, 2, 3, 4].map(n => el('option', {value: String(n), text: `${n} ${n === 1 ? 'picture' : 'pictures'}`})));
        count.value = String(s.count || 2);
        const prompt = el('textarea', {class: 'rvo-einput rim-prompt', rows: '3', placeholder: 'Leave empty: your chat model writes the prompt from the card'});
        const progress = el('div', {class: 'rim-progress'});
        const results = el('div', {class: 'rim-results'});
        const go = button(stock ? 'Generate picture' : 'New picture', 'fa-wand-magic-sparkles', async () => {
            go.disabled = true;
            results.replaceChildren();
            try {
                const task = await post('/image/avatar', {avatar: ch.avatar, model: model.value, card: {...cardOf(ch), model: chatModel()}, prompt: prompt.value.trim(), count: Number(count.value)});
                const stop = button('Stop', 'fa-stop', () => post(`/image/task/${task.id}/stop`).catch(() => {}));
                const line = el('span', {text: task.stage});
                progress.replaceChildren(icon('fa-spinner fa-spin'), line, stop);
                const done = await waitTask(task.id, t => { line.textContent = t.stage + (t.error ? `: ${t.error}` : ''); if (t.prompt && !prompt.value.trim()) prompt.value = t.prompt; }, alive);
                if (!done) return;
                progress.replaceChildren(el('span', {class: done.state === 'done' ? 'rim-ok' : 'rim-bad', text: done.state === 'done' ? 'Pick one to use it:' : `${done.stage}${done.error ? `: ${done.error}` : ''}`}));
                for (let i = 0; i < done.pictures; i++) {
                    const blob = await api(`/image/task/${done.id}/picture/${i}`);
                    const url = URL.createObjectURL(blob);
                    const use = el('button', {class: 'rim-pick', type: 'button', title: 'Use this picture', onclick: async () => {
                        try {
                            use.disabled = true;
                            await useAsPicture(ch, blob);
                            toastr.success(`New picture for ${ch.name}`, 'Rocsy Image');
                            onPicture?.();
                            refresh();
                        } catch (error) { toastr.error(error.message, 'Picture'); use.disabled = false; }
                    }}, el('img', {src: url, alt: ''}), el('span', {class: 'rim-pick-label'}, icon('fa-check'), el('span', {text: 'Use this'})));
                    results.append(use);
                }
            } catch (error) {
                progress.replaceChildren(el('span', {class: 'rim-bad', text: error.message}));
            } finally {
                go.disabled = false;
            }
        }, {primary: stock});
        fill(pictureBox,
            el('div', {class: 'rim-head'}, icon('fa-image'), el('strong', {text: 'Picture'}),
                stock ? el('span', {class: 'rim-badge', text: 'not set'}) : null),
            stock ? el('small', {class: 'rvo-muted', text: `${ch.name} still has SillyTavern's stock picture. Rocsy Image can paint one from the card.`}) : null,
            el('div', {class: 'rim-row'}, model, count), prompt, el('div', {class: 'rim-row'}, go), progress, results);
    };

    // --- expressions
    const drawExpressions = (state) => {
        const labels = state.labels?.length ? state.labels : info?.expressions || [];
        const have = new Set(state.sprites || []);
        const missing = labels.filter(l => !have.has(l));
        const transparent = el('input', {type: 'checkbox', onchange: e => { s.transparent = e.target.checked; saveStore(); }});
        transparent.checked = s.transparent !== false;
        const grid = el('div', {class: 'rim-sprites'}, ...labels.map(label => el('div', {class: `rim-sprite ${have.has(label) ? 'has' : ''}`, 'data-label': label, title: label},
            have.has(label) ? el('img', {src: `/characters/${encodeURIComponent(ch.name)}/${encodeURIComponent(label)}.png?t=${Date.now()}`, alt: '', loading: 'lazy'}) : el('span', {class: 'rim-sprite-empty'}, icon('fa-face-meh-blank')),
            el('small', {text: label}))));
        // Click a finished expression: the overhaul's gallery of all the character's sprites (cells finished later too).
        grid.addEventListener('click', event => {
            const cell = event.target.closest('.rim-sprite.has');
            if (cell && globalThis.RocsyVisualOverhaul?.showExpressionGallery) globalThis.RocsyVisualOverhaul.showExpressionGallery(ch, cell.dataset.label);
        });
        // Master reference: one neutral picture every expression is edited from (made first, then reused).
        // From the card picture (default), or painted from the card text when that's off or there's no picture yet.
        const masterFrom = () => state.stock || s.masterFromPicture === false ? 'text' : 'picture';
        const fromPicture = el('input', {type: 'checkbox', disabled: state.stock || undefined, onchange: e => { s.masterFromPicture = e.target.checked; saveStore(); drawMaster(); }});
        fromPicture.checked = masterFrom() === 'picture';
        const masterBox = el('div', {class: 'rim-master'});
        let masterUrl = '';
        const drawMaster = () => {
            const m = state.master;
            const source = masterFrom();
            const reuse = m && (m.source === 'upload' || (m.source === source && m.fresh));
            const sourceText = source === 'picture' ? 'the character’s picture' : 'the card’s description (the chat model writes the prompt)';
            const note = !m ? `The first step makes a master reference from ${sourceText}.`
                : reuse ? (m.source === 'upload' ? 'Expressions are edited from your picture. “New master” removes it.' : 'Expressions are edited from this master. “Redo all” or “New master” make a fresh one.')
                : `A new master will be made from ${sourceText} on the next run${m.source === 'picture' && source === 'picture' ? ' (the picture changed)' : ''}.`;
            const thumb = m ? el('button', {class: 'rim-master-pic', type: 'button', title: 'Open the master reference', onclick: () => masterUrl && window.open(masterUrl, '_blank')},
                el('img', {alt: 'Master reference'})) : null;
            if (thumb) api(`/image/master?avatar=${encodeURIComponent(ch.avatar)}`).then(blob => {
                if (masterUrl) URL.revokeObjectURL(masterUrl);
                masterUrl = URL.createObjectURL(blob); thumb.firstChild.src = masterUrl;
            }).catch(() => thumb.remove());
            masterBox.replaceChildren(thumb || '',
                el('div', {class: 'rim-master-text'},
                    el('strong', {text: m ? `Master reference · ${m.source === 'upload' ? 'uploaded' : m.source === 'text' ? 'from description' : 'from picture'}` : 'Master reference'}),
                    el('small', {class: 'rvo-muted', text: m ? note : `${note} Or make it now to check it first, or upload your own.`}),
                    m ? null : el('div', {class: 'rim-row'},
                        button('Create master', 'fa-wand-magic-sparkles', async () => {
                            try { follow(await post('/image/master/create', {avatar: ch.avatar, name: ch.name, masterFrom: masterFrom(), card: {...cardOf(ch), model: chatModel()}, model: exprModel()})); }
                            catch (error) { toastr.error(error.message, 'Master reference'); }
                        }, {title: `Make only the master reference now, from ${sourceText}`}),
                        button('Upload…', 'fa-upload', () => upload.click(), {title: 'Use your own picture as the master reference'})),
                    m ? button('New master', 'fa-rotate', async () => {
                        try { await api(`/image/master?avatar=${encodeURIComponent(ch.avatar)}`, {method: 'DELETE'}); state.master = null; drawMaster(); }
                        catch (error) { toastr.error(error.message, 'Master reference'); }
                    }, {title: 'Forget this master: the next run makes a new one'}) : null));
        };
        const upload = el('input', {type: 'file', accept: 'image/*', hidden: true, onchange: async e => {
            const file = e.target.files?.[0];
            e.target.value = '';
            if (!file) return;
            try {
                const image = await new Promise((resolve, reject) => { const r = new FileReader(); r.onload = () => resolve(r.result); r.onerror = () => reject(new Error('Couldn’t read that file')); r.readAsDataURL(file); });
                await post('/image/master', {avatar: ch.avatar, image});
                toastr.success(`Master reference for ${ch.name} uploaded`, 'Rocsy Image');
                refresh();
            } catch (error) { toastr.error(error.message, 'Master reference'); }
        }});
        // Which edit model makes the master + expressions, and the guidance prompt each expression is edited with.
        const allModels = info?.models || [];
        const modelName = id => (allModels.find(m => m.id === id)?.title || id).replace(/^Rocsy · /, '');
        const editModels = allModels.filter(m => m.edit && m.ready);
        if (editModels.length && !editModels.some(m => m.id === s.exprModel)) s.exprModel = editModels[0].id;
        const exprModel = () => s.exprModel || 'qwen_image_21';
        const defaultPrompt = info?.expressionPrompt || '';
        const intro = el('small', {class: 'rvo-muted'});
        const summaryNote = el('small', {class: 'rvo-muted'});
        const transparentText = el('span');
        const describe = () => {
            intro.textContent = `${modelName(exprModel())} first makes one master reference of the character, then redraws it with each feeling (queued one at a time). They’re saved as SillyTavern expression sprites.`;
            summaryNote.textContent = ` · ${modelName(exprModel())}${s.exprPrompt ? ' · custom prompt' : ''}`;
            transparentText.textContent = exprModel() === 'qwen_image_21' ? 'Transparent background' : 'Transparent background (only Qwen Image 2.1 can; this model keeps the background)';
        };
        const modelPick = el('select', {class: 'rvo-einput', onchange: e => { s.exprModel = e.target.value; saveStore(); describe(); }},
            ...(editModels.length ? editModels : [{id: 'qwen_image_21'}]).map(m => el('option', {value: m.id, text: modelName(m.id)})));
        modelPick.value = exprModel();
        const promptBox = el('textarea', {class: 'rvo-einput rim-prompt rim-expr-prompt', rows: '6', spellcheck: 'false', placeholder: defaultPrompt,
            oninput: e => { s.exprPrompt = e.target.value.trim() === defaultPrompt.trim() ? '' : e.target.value; saveStore(); describe(); }});
        promptBox.value = s.exprPrompt || defaultPrompt;
        const exprSettings = el('details', {class: 'rim-expr-settings', open: !!s.exprSettingsOpen, ontoggle: e => { s.exprSettingsOpen = e.target.open; saveStore(); }},
            el('summary', {}, icon('fa-sliders'), el('span', {text: 'Model & guidance prompt'}), summaryNote),
            el('label', {class: 'rim-field'}, el('span', {text: 'Image model'}), modelPick),
            el('small', {class: 'rvo-muted', text: 'Only models that edit a picture can keep the character the same across expressions: Qwen Image 2.1 and MiniMax H3 Image. Used for the master reference too. Applies to every character.'}),
            el('label', {class: 'rim-field'}, el('span', {text: 'Guidance prompt (sent once per expression)'}), promptBox),
            el('small', {class: 'rvo-muted', text: 'Placeholders: {{char}} the name · {{label}} the expression · {{hint}} what that expression looks like · {{picture}} the master picture (“<Picture 1>” for H3) · {{background}} transparent or keep · {{appearance}}. Without {{label}} or {{hint}}, the expression is added at the end.'}),
            el('div', {class: 'rim-row'}, button('Reset to default', 'fa-rotate-left', () => { s.exprPrompt = ''; saveStore(); promptBox.value = defaultPrompt; describe(); })));
        describe();
        drawMaster();
        const progress = el('div', {class: 'rim-progress'});
        const start = async overwrite => {
            try {
                const task = await post('/image/expressions', {avatar: ch.avatar, name: ch.name, labels: overwrite ? labels : missing, hints: state.descriptions || {}, transparent: transparent.checked, overwrite,
                    masterFrom: masterFrom(), newMaster: overwrite && state.master?.source !== 'upload', card: {...cardOf(ch), model: chatModel()},
                    model: exprModel(), promptTemplate: s.exprPrompt || ''});
                follow(task);
            } catch (error) { progress.replaceChildren(el('span', {class: 'rim-bad', text: error.message})); }
        };
        const follow = async task => {
            const stop = button('Stop', 'fa-stop', () => post(`/image/task/${task.id}/stop`).catch(() => {}));
            const line = el('span');
            progress.replaceChildren(icon('fa-spinner fa-spin'), line, stop);
            actions.hidden = true;
            const done = await waitTask(task.id, t => {
                const made = t.items.filter(i => i.state === 'done').length;
                line.textContent = t.kind === 'master' ? t.stage : `${made} of ${t.items.length} · ${t.stage}`;
                for (const item of t.items) {
                    const cell = grid.querySelector(`[data-label="${CSS.escape(item.label)}"]`);
                    if (!cell) continue;
                    cell.classList.toggle('busy', item.state === 'running');
                    cell.classList.toggle('failed', item.state === 'failed');
                    if (item.state === 'failed') cell.title = `${item.label}: ${item.error}`;
                    if (item.state === 'done' && !cell.classList.contains('fresh')) {
                        cell.classList.add('has', 'fresh');
                        cell.firstChild.replaceWith(el('img', {src: `/characters/${encodeURIComponent(ch.name)}/${encodeURIComponent(item.label)}.png?t=${Date.now()}`, alt: ''}));
                    }
                }
            }, alive);
            if (!done) return;
            if (done.kind === 'master' && done.state === 'done') toastr.success(`Master reference for ${ch.name} is ready`, 'Rocsy Image');
            const failed = done.items.filter(i => i.state === 'failed');
            if (done.state === 'done' && done.items.some(i => i.state === 'done')) { toastr.success(`Expressions for ${ch.name}: ${done.stage.replace(/^Done: /, '')}`, 'Rocsy Image'); offerExpressionsOn(); }
            if (done.state === 'failed' && done.error) toastr.error(done.error, done.items.every(i => i.state !== 'done') && /master|reference|Cancelled/i.test(done.stage + done.error) ? 'Master reference' : 'Expressions');
            if (failed.length) toastr.warning(failed.slice(0, 3).map(i => `${i.label}: ${i.error}`).join('\n'), `${failed.length} expression(s) failed`);
            refresh();
        };
        const actions = el('div', {class: 'rim-row'},
            missing.length ? button(have.size ? `Make the ${missing.length} missing` : 'Generate all expressions', 'fa-masks-theater', () => start(false), {primary: true}) : null,
            have.size ? button('Redo all', 'fa-rotate', async () => {
                const ok = await ctx().callGenericPopup(`Replace all ${labels.length} expressions of ${ch.name}? This makes a new master reference, then ${labels.length} pictures one after another.`, ctx().POPUP_TYPE?.CONFIRM ?? 2);
                if (ok) start(true);
            }) : null);
        fill(exprBox,
            el('div', {class: 'rim-head'}, icon('fa-masks-theater'), el('strong', {text: 'Expressions'}), el('span', {class: 'rim-count-badge', text: `${labels.filter(l => have.has(l)).length} / ${labels.length}`})),
            intro,
            el('small', {class: 'rvo-muted'}, `Classifier: ${classifierName()} (${labels.length} labels) · `,
                el('a', {href: '#', onclick: e => { e.preventDefault(); globalThis.RocsyAddons?.open?.('image'); }, text: 'change'})),
            el('label', {class: 'rim-check', title: state.stock ? 'The character still has the stock picture, so the master is painted from the description' : ''}, fromPicture,
                el('span', {text: state.stock ? 'Base the master on the character’s picture (no picture yet: using the description)' : 'Base the master on the character’s picture (off: paint it from the description)'})),
            exprSettings,
            masterBox, upload,
            el('label', {class: 'rim-check'}, transparent, transparentText),
            actions, progress, grid);
        const running = (state.running || []).find(t => t.kind === 'expressions' || t.kind === 'master');
        if (running) follow(running);
    };

    const refresh = async () => {
        box.classList.add('loading');
        try {
            await imageInfo();
            if (!info.online) { box.replaceChildren(el('small', {class: 'rim-bad', text: 'Rocsy Image is offline (Rocsy → Image).'})); return; }
            status = await api(`/image/character?avatar=${encodeURIComponent(ch.avatar)}&name=${encodeURIComponent(ch.name)}`);
            Object.assign(status, await classifierInfo());   // labels + what each means (pseudo models)
            if (!alive() && box.parentNode) return;
            if (pictureBox.parentNode !== box) box.replaceChildren(pictureBox, exprBox);   // after an error message
            box.querySelector('.rim-loading')?.remove();
            drawPicture(status.stock);
            drawExpressions(status);
        } catch (error) {
            box.replaceChildren(el('small', {class: 'rim-bad', text: error.message}));
        } finally {
            box.classList.remove('loading');
        }
    };
    box.append(el('small', {class: 'rvo-muted rim-loading', text: 'Loading…'}));
    refresh();
    return box;
}

/** SillyTavern shows sprites only when its Expressions classifier is on: offer to turn it on (local, no extra chat calls). */
async function offerExpressionsOn() {
    const ext = ctx().extensionSettings;
    if (classifier().mode !== 'off' && !ext.disabledExtensions?.includes('expressions')) return;
    const ok = await ctx().callGenericPopup('Turn on SillyTavern’s character expressions? A local classifier (RoBERTa GoEmotions) picks the sprite for each reply: it downloads once, then runs on the CPU. Other classifiers are in Rocsy Addons → Image.', ctx().POPUP_TYPE?.CONFIRM ?? 2);
    if (!ok) return;
    classifier().mode = 'onnx';
    applyMode();
    if (ext.disabledExtensions?.includes('expressions')) {
        ext.disabledExtensions = ext.disabledExtensions.filter(x => x !== 'expressions');
        ctx().saveSettingsDebounced();
        toastr.info('Reload the page to start the Expressions extension.');
    }
}

// --------------------------------------------------------------------------- voice --

async function waitVoiceJob(id, onUpdate, isAlive) {
    for (;;) {
        if (!isAlive()) return null;
        const job = await api(`/voice/job/${id}`);
        onUpdate(job);
        if (['complete', 'failed', 'cancelled'].includes(job.state)) return job;
        await sleep(2000);
    }
}

/** The editor's "Voice" card: the picker, and making a new Rocsy Voice for this character. */
function voiceTools(ch, {onChange} = {}) {
    const box = el('div', {class: 'rim'});
    const alive = () => box.isConnected;
    const pickerSlot = el('div');
    const drawPicker = () => pickerSlot.replaceChildren(globalThis.RocsyAddons?.voiceControl?.(ch.name) || el('small', {class: 'rvo-muted', text: 'Rocsy Addons voice settings aren’t loaded.'}));
    drawPicker();
    const maker = el('div', {class: 'rim-maker', hidden: true});
    const toggle = button('Create a voice', 'fa-microphone-lines', () => { maker.hidden = !maker.hidden; if (!maker.hidden && !maker.childElementCount) drawMaker('describe'); });

    const finish = el('div', {class: 'rim-finish'});
    const showClip = (clipId, transcript, summary) => {
        const audio = el('audio', {controls: true, src: `${API}/voice/clip/${clipId}?t=${Date.now()}`});
        const text = el('textarea', {class: 'rvo-einput', rows: '2', placeholder: 'What the clip says (helps the voice engines)'});
        text.value = transcript || '';
        const use = button('Use this voice', 'fa-check', async () => {
            use.disabled = true;
            try {
                const {voice} = await post('/voice/save', {clip: clipId, name: ch.name, transcript: text.value.trim(), avatar: ch.avatar, summary});
                await globalThis.RocsyAddons?.setCharacterVoice?.(ch.name, voice.name);
                toastr.success(`${ch.name} now speaks with the Rocsy Voice “${voice.name}”`, 'Rocsy Voice');
                drawPicker();
                onChange?.();
                maker.hidden = true;
                maker.replaceChildren();
            } catch (error) { toastr.error(error.message, 'Rocsy Voice'); use.disabled = false; }
        }, {primary: true});
        finish.replaceChildren(audio, text, el('div', {class: 'rim-row'}, use));
        return text;
    };

    const drawMaker = mode => {
        finish.replaceChildren();
        const tabs = el('div', {class: 'rim-tabs'},
            el('button', {type: 'button', class: mode === 'describe' ? 'on' : '', onclick: () => drawMaker('describe')}, icon('fa-pen-fancy'), el('span', {text: 'Describe'})),
            el('button', {type: 'button', class: mode === 'recording' ? 'on' : '', onclick: () => drawMaker('recording')}, icon('fa-file-audio'), el('span', {text: 'From a recording'})));
        const progress = el('div', {class: 'rim-progress'});
        let body;
        if (mode === 'describe') {
            const description = el('textarea', {class: 'rvo-einput', rows: '3', placeholder: 'How they sound: gender, age, pitch, pace, accent, attitude…'});
            const line = el('textarea', {class: 'rvo-einput', rows: '2', placeholder: 'A line for them to say (the voice sample)'});
            const write = button('Write from the card', 'fa-feather', async () => {
                write.disabled = true;
                progress.replaceChildren(icon('fa-spinner fa-spin'), el('span', {text: 'Your chat model is reading the card…'}));
                try {
                    const out = await post('/voice/describe', {model: chatModel(), card: cardOf(ch)});
                    description.value = out.description; line.value = out.line;
                    progress.replaceChildren();
                } catch (error) { progress.replaceChildren(el('span', {class: 'rim-bad', text: error.message})); }
                write.disabled = false;
            });
            const make = button('Hear it', 'fa-play', async () => {
                make.disabled = true;
                finish.replaceChildren();
                try {
                    const job = await post('/voice/design', {description: description.value.trim(), text: line.value.trim()});
                    const label = el('span', {text: job.stage});
                    progress.replaceChildren(icon('fa-spinner fa-spin'), label);
                    const done = await waitVoiceJob(job.id, j => { label.textContent = j.stage; }, alive);
                    if (!done) return;
                    if (done.state !== 'complete') throw new Error(done.error || 'Voice creation failed');
                    progress.replaceChildren(el('small', {class: 'rvo-muted', text: 'Not quite? Change the description and try again.'}));
                    showClip(done.id, line.value.trim(), description.value.trim());
                } catch (error) { progress.replaceChildren(el('span', {class: 'rim-bad', text: error.message})); }
                make.disabled = false;
            }, {primary: true});
            body = [el('small', {class: 'rvo-muted', text: 'Qwen3-TTS VoiceDesign makes a brand-new voice from a description.'}),
                description, line, el('div', {class: 'rim-row'}, write, make)];
        } else {
            const file = el('input', {type: 'file', accept: 'audio/*,video/*', class: 'rim-file'});
            const start = el('input', {type: 'number', class: 'rvo-einput rim-start', min: '0', step: '1', value: '0', title: 'Start of the 15 seconds to use'});
            const clean = button('Clean it up', 'fa-broom', async () => {
                const picked = file.files?.[0];
                if (!picked) { toastr.info('Pick an audio or video file first'); return; }
                clean.disabled = true;
                finish.replaceChildren();
                try {
                    progress.replaceChildren(icon('fa-spinner fa-spin'), el('span', {text: 'Uploading…'}));
                    const job = await api(`/voice/recording?start=${Number(start.value) || 0}&name=${encodeURIComponent(picked.name)}`, {method: 'POST', raw: true, headers: {'Content-Type': picked.type || 'application/octet-stream'}, body: picked});
                    const label = el('span', {text: job.stage});
                    progress.replaceChildren(icon('fa-spinner fa-spin'), label);
                    const done = await waitVoiceJob(job.id, j => { label.textContent = j.stage; }, alive);
                    if (!done) return;
                    if (done.state !== 'complete') throw new Error(done.error || 'Cleanup failed');
                    const text = showClip(done.id, '', '');
                    label.textContent = 'Transcribing…';
                    const tjob = await post(`/voice/transcribe/${done.id}`);
                    const heard = await waitVoiceJob(tjob.id, j => { label.textContent = j.stage; }, alive);
                    if (heard?.state === 'complete' && !text.value) text.value = heard.text || '';
                    progress.replaceChildren(el('small', {class: 'rvo-muted', text: '15 seconds, music removed. Listen, fix the words if needed, then use it.'}));
                } catch (error) { progress.replaceChildren(el('span', {class: 'rim-bad', text: error.message})); }
                clean.disabled = false;
            }, {primary: true});
            body = [el('small', {class: 'rvo-muted', text: 'A clip of the voice you want (any length; music is removed). 15 seconds are used.'}),
                file, el('label', {class: 'rim-row'}, el('span', {class: 'rvo-muted', text: 'Start at (s)'}), start), el('div', {class: 'rim-row'}, clean)];
        }
        maker.replaceChildren(tabs, ...body, progress, finish);
    };
    box.append(pickerSlot, el('div', {class: 'rim-row'}, toggle), maker);
    return box;
}

// ----------------------------------------------- SillyTavern's Image Generation --

/** Point SillyTavern's Image Generation at Rocsy Image (source "Stable Diffusion WebUI", the bridge URL). */
async function useForImageGeneration({quiet = false, model = ''} = {}) {
    const ext = ctx().extensionSettings;
    const sd = ext.sd ||= {};
    await imageInfo(true);
    const title = model || info.sdModel || info.models.find(m => m.ready)?.title || '';
    Object.assign(sd, {source: 'auto', auto_url: BRIDGE, auto_auth: '', model: title, sampler: 'Rocsy (model default)'});
    if (!sd.width || sd.width <= 512) { sd.width = 832; sd.height = 1216; }
    store().sdSetup = true;
    ctx().saveSettingsDebounced();
    if ($('#sd_source').length) {
        $('#sd_source').val('auto').trigger('change');
        $('#sd_auto_url').val(BRIDGE).trigger('input');
        setTimeout(() => $('#sd_auto_validate').trigger('click'), 400);
    }
    if (!quiet) toastr.success('SillyTavern’s image generation (/sd, the wand menu) now uses Rocsy Image', 'Rocsy Image');
}


const usingRocsy = () => { const sd = ctx().extensionSettings.sd || {}; return sd.source === 'auto' && sd.auto_url === BRIDGE; };

/** One-time: SillyTavern's image generation still points at the old Extras API (does nothing) → Rocsy Image. */
/** Once: switch expressions to Rocsy's RoBERTa GoEmotions classifier (more accurate than SillyTavern's default
 *  DistilBERT, same 28 labels, CPU) when they were off or on SillyTavern's own local model. */
function expressionsOnce() {
    const s = store();
    if (s.expressionsSetup) return;
    s.expressionsSetup = true;
    const c = classifier();
    if (c.mode === 'off' || c.mode === 'st') {
        c.mode = 'onnx';
        c.onnx = 'Cohee/roberta-base-go_emotions-onnx';
        applyMode();
    }
    saveStore();
}

async function autoSetup() {
    try { expressionsOnce(); } catch (error) { console.warn('[Rocsy Image] expressions setup', error); }
    const s = store();
    if (s.sdSetup) return;
    const sd = ctx().extensionSettings.sd || {};
    if (sd.source && sd.source !== 'extras') { s.sdSetup = true; saveStore(); return; }
    try {
        await imageInfo(true);
        if (info.online && info.bridgeUp) await useForImageGeneration({quiet: true});
    } catch { /* server part not updated yet */ }
}

/** Rocsy Addons → Image tab. */
function imageSettings() {
    const box = el('div', {class: 'rim rim-settings'});
    const draw = async () => {
        try { await imageInfo(true); } catch (error) { box.replaceChildren(el('p', {class: 'rad-error', text: error.message})); return; }
        const on = usingRocsy();
        const pick = el('select', {class: 'text_pole', onchange: async e => {
            ctx().extensionSettings.sd.model = e.target.value;
            await fetch(`${API}/image/sd-model`, {method: 'POST', headers: ctx().getRequestHeaders(), body: JSON.stringify({model: e.target.value})}).catch(() => {});
            $('#sd_model').val(e.target.value).trigger('change');
            ctx().saveSettingsDebounced();
        }}, ...info.models.filter(m => m.ready).map(m => el('option', {value: m.title, text: m.title.replace(/^Rocsy · /, '')})));
        pick.value = ctx().extensionSettings.sd?.model || info.sdModel;
        fill(box,
            el('p', {text: info.online ? 'Rocsy Image is online.' : 'Rocsy Image is offline (start it in Rocsy).'}),
            el('p', {class: 'rad-muted', text: 'SillyTavern’s Image Generation can render with Rocsy Image: /sd you, /sd me, the wand menu’s “Generate image” and the per-character prompt prefixes. Renders wait their turn in the Rocsy GPU queue and also show in Image → Generations.'}),
            on ? el('div', {class: 'rim-row'}, el('span', {text: 'Model'}), pick)
                : el('button', {class: 'menu_button', type: 'button', onclick: async () => { await useForImageGeneration(); draw(); }}, icon('fa-plug'), el('span', {text: 'Use Rocsy Image for image generation'})),
            on ? el('p', {class: 'rad-muted', text: `Connected (Stable Diffusion WebUI source → ${BRIDGE}).`}) : null,
            el('p', {class: 'rad-muted', text: 'Character pictures, expressions and voices are made in each character’s editor (Characters → a character → Edit).'}),
            el('h4', {class: 'rim-subhead'}, icon('fa-face-smile-beam'), el('span', {text: 'Expression classifier'})),
            el('p', {class: 'rad-muted', text: 'Picks which expression sprite shows for each reply. The editor makes sprites for this classifier’s labels.'}),
            classifierPicker(el, icon));
    };
    box.append(el('small', {text: 'Loading…'}));
    draw();
    return box;
}

globalThis.RocsyImage = {pictureTools, voiceTools, useForImageGeneration, imageSettings, autoSetup, usingRocsy,
    classifierPicker: () => classifierPicker(el, icon), classifierLabels};
