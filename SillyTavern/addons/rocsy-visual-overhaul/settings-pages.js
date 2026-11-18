// Rocsy Visual Overhaul: SillyTavern's settings as pages of the site.
// Model & connection, Response settings (with the prompt list), Persona, Groups, Quick Replies, Regex,
// Memory (Summarize + Vector Storage) and Extras (Expressions + Image generation). SillyTavern's own panels are
// never shown: plain settings are "mirrored" (a Rocsy control that writes through SillyTavern's own, hidden
// input and fires its events, so SillyTavern's handlers save exactly as before); personas, groups, prompts,
// quick replies and regex scripts are edited through SillyTavern's modules and API, the same ones its panels use.

let H = null;   // helpers from index.js (setup)
export function setup(hooks) { H = hooks; }

const el = (...args) => H.el(...args);
const icon = name => H.icon(name);
const ctx = () => H.ctx();
const $q = (selector, root = document) => root.querySelector(selector);
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const imports = {};
const load = (key, path) => (imports[key] ||= import(path));
const scriptModule = () => load('script', '../../../../script.js');
const personasModule = () => load('personas', '../../../personas.js');
const groupsModule = () => load('groups', '../../../group-chats.js');
const openaiModule = () => load('openai', '../../../openai.js');
const regexModule = () => load('regex', '../../regex/engine.js');
const extensionsModule = () => load('extensions', '../../../extensions.js');
const worldModule = () => load('world', '../../../world-info.js');
const backgroundsModule = () => load('backgrounds', '../../../backgrounds.js');
const uid = () => (globalThis.crypto?.randomUUID?.() || `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}-${Math.random().toString(16).slice(2, 10)}`);

export const PAGES = {
    connection: {label: 'Model & connection', sub: 'Where replies come from'},
    response: {label: 'Response settings', sub: 'Length, sampling and the prompt'},
    persona: {label: 'Persona', sub: 'Who you are in the chat'},
    groups: {label: 'Groups', sub: 'Group chats with several characters'},
    quickreplies: {label: 'Quick Replies', sub: 'Buttons above the chat box'},
    regex: {label: 'Regex', sub: 'Find and replace in messages and prompts'},
    memory: {label: 'Memory', sub: 'Summarize and Vector Storage'},
    extras: {label: 'Expressions & images', sub: 'Character expressions and image generation'},
    formatting: {label: 'Prompt formatting', sub: 'System prompt, templates, reasoning and stop strings'},
    extensions: {label: 'Extensions', sub: 'Installed extensions and their settings'},
    backgrounds: {label: 'Backgrounds', sub: 'The picture behind the chat'},
    interface: {label: 'Interface', sub: 'Layout, phone, notifications, themes and chat behaviour'},
    authorsnote: {label: 'Author’s Note', sub: 'A note added to the prompt for this chat'},
};
export const isSettingsPage = id => Object.hasOwn(PAGES, id);

const state = {persona: '', personaQ: '', group: '', groupQ: '', memberQ: '', qrSet: '', regexType: 0, open: new Set(), promptOpen: new Set()};

// --------------------------------------------------------------- navigation --
export function navInfo(id) {
    if (id === 'persona') return {view: state.persona ? 'one' : '', avatar: state.persona};
    if (id === 'groups') return {view: state.group ? 'one' : '', avatar: state.group};
    if (id === 'quickreplies') return {view: '', avatar: state.qrSet};
    if (id === 'regex') return {view: String(state.regexType), avatar: ''};
    return {view: '', avatar: ''};
}
export function optionsFromNav(nav) {
    if (nav.page === 'persona') return {persona: nav.avatar || ''};
    if (nav.page === 'groups') return {group: nav.avatar || ''};
    if (nav.page === 'quickreplies') return {set: nav.avatar || ''};
    if (nav.page === 'regex') return {type: Number(nav.view) || 0};
    return {};
}
export function samePlace(id, nav) {
    const here = navInfo(id);
    return here.view === (nav.view || '') && here.avatar === (nav.avatar || '');
}

// ------------------------------------------------------------------ render --
const renderers = {};
let renderToken = 0;

export function render(id, root, options = {}) {
    if ('persona' in options) state.persona = options.persona || '';
    if ('group' in options) state.group = options.group || '';
    if ('set' in options && options.set) state.qrSet = options.set;
    if ('type' in options) state.regexType = Number(options.type) || 0;
    root.classList.add('rvo-spage');
    return draw(id, root);
}

// SillyTavern panels shown as they are (Extensions → "Original controls") are borrowed and put back on leaving.
const adopted = [];
export function leave() {
    for (const a of adopted.splice(0)) {
        a.node.style.display = a.display;
        a.parent.insertBefore(a.node, a.next?.parentNode === a.parent ? a.next : null);
    }
}
function adopt(content, host) {
    adopted.push({node: content, parent: content.parentElement, next: content.nextSibling, display: content.style.display});
    content.style.display = 'block';
    host.append(content);
}

async function draw(id, root) {
    const token = ++renderToken;
    leave();
    const place = JSON.stringify([id, navInfo(id)]);
    const scroll = root.dataset.place === place ? root.scrollTop : 0;
    root.dataset.place = place;
    const wrap = el('div', {class: 'rvo-cwrap rvo-swrap'});
    try {
        await renderers[id](wrap);
    } catch (error) {
        console.error('[Rocsy] settings page', id, error);
        wrap.replaceChildren(el('p', {class: 'rvo-empty', text: `This page couldn’t be shown: ${error.message}`}));
    }
    if (token !== renderToken && root.dataset.drawn) return;   // a newer draw of a page started meanwhile
    root.dataset.drawn = '1';
    root.replaceChildren(wrap);
    root.scrollTop = scroll;
    H.renderHeader();
}

/** Redraw the page if it's on screen, unless something in it is being typed in (then once it loses focus). */
export function refresh(id) {
    const root = document.getElementById(`rvo-${id}`);
    if (!root || H.page() !== id) return;
    const active = document.activeElement;
    if (active && root.contains(active) && /^(INPUT|TEXTAREA)$/.test(active.tagName) && active.type !== 'checkbox' && active.type !== 'radio') {
        active.addEventListener('blur', () => setTimeout(() => refresh(id), 50), {once: true});
        return;
    }
    draw(id, root);
}
let refreshTimer = 0;
const refreshSoon = (id = H.page(), delay = 250) => { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => refresh(id), delay); };

// ------------------------------------------------------------------ pieces --
function section(key, title, iconName, children, {open = true, sub = '', grid = true} = {}) {
    const body = grid ? el('div', {class: 'rvo-lset-grid'}, ...children.filter(Boolean)) : el('div', {class: 'rvo-sbody'}, ...children.filter(Boolean));
    const isOpen = state.open.has(key) ? true : state.open.has(`-${key}`) ? false : open;
    return el('details', {class: 'rvo-lsettings rvo-ssection', 'data-k': key, open: isOpen || undefined,
        ontoggle: event => { state.open.delete(key); state.open.delete(`-${key}`); state.open.add(event.target.open ? key : `-${key}`); }},
        el('summary', {}, icon(iconName), el('span', {text: title}), sub ? el('small', {text: sub}) : null), body);
}
const put = (node, ...children) => node.append(...children.filter(Boolean));
const note = (text, extra = '') => el('p', {class: `rvo-lnote ${extra}`, text});
const button = (label, faIcon, onclick, title = label, extra = '') => H.rvoButton(label, faIcon, onclick, title, extra);
const field = (label, control, hint = '', wide = false) => el('label', {class: `rvo-lfield${wide ? ' wide' : ''}`}, el('span', {text: label}), control, hint ? el('small', {text: hint}) : null);
function textInput(value, onchange, attrs = {}) {
    return el('input', {type: 'text', class: 'rvo-cinput', value: value ?? '', ...attrs, onchange: event => onchange(event.target.value)});
}
function numberInput(value, onchange, attrs = {}) {
    return el('input', {type: 'number', class: 'rvo-cinput', value: value ?? '', ...attrs, onchange: event => onchange(event.target.value)});
}
function area(value, oninput, attrs = {}) {
    const node = el('textarea', {class: 'rvo-cinput rvo-sarea', rows: attrs.rows || 4, ...attrs});
    node.value = value ?? '';
    node.addEventListener('input', () => oninput(node.value));
    return node;
}
function choose(options, value, onchange, attrs = {}) {
    const node = el('select', {class: 'rvo-cinput', ...attrs, onchange: event => onchange(event.target.value)},
        ...options.map(([v, label]) => el('option', {value: String(v), text: label, selected: String(v) === String(value) || undefined})));
    return node;
}
function check(label, checked, onchange, hint = '') {
    return el('label', {class: 'rvo-lcheck', title: hint || undefined},
        el('input', {type: 'checkbox', checked: checked || undefined, onchange: event => onchange(event.target.checked)}),
        el('span', {text: label}));
}
const toggle = (checked, onchange, label = '', title = label) => H.switchControl(
    el('input', {type: 'checkbox', checked: checked || undefined, onchange: event => onchange(event.target.checked)}), label, title);
function empty(iconName, title, text, ...actions) {
    return el('div', {class: 'rvo-lempty'}, icon(iconName), el('strong', {text: title}), text ? el('span', {text}) : null, ...actions);
}
const debounce = (fn, ms) => { let t = 0; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); }; };
const saveSettings = () => ctx().saveSettingsDebounced();
const confirmPopup = async (title, text) => {
    const {Popup} = ctx();
    if (Popup?.show?.confirm) return !!(await Popup.show.confirm(title, text));
    return window.confirm(`${title}\n\n${text}`);
};
const askText = async (title, value = '') => {
    const {Popup} = ctx();
    if (Popup?.show?.input) { const v = await Popup.show.input(title, '', value); return typeof v === 'string' ? v.trim() : ''; }
    return (window.prompt(title, value) || '').trim();
};

// ---------------------------------------------------------------- mirroring --
// A Rocsy control bound to one of SillyTavern's own (hidden) inputs. Its value is read when the page is drawn
// and written back through the input with the events SillyTavern listens for. Inputs SillyTavern itself hides
// for the current API / source (display: none on them or a parent) are left out.

function shown(node) {
    for (let n = node; n && n !== document.body; n = n.parentElement) {
        // Fold-outs the user opens and closes (drawers, Summarize's "Summary Settings") don't count as hidden.
        if (n.classList.contains('inline-drawer-content') || n.classList.contains('drawer-content') || n.id === 'summarySettingsBlock') continue;
        if (n.style?.display === 'none' || n.classList.contains('displayNone') || n.hidden || n.type === 'hidden') return false;
    }
    return true;
}
const fire = (node, events) => { for (const name of events) $(node).trigger(name); };
let mirrorBusy = false;

/** Build the control for one SillyTavern input. spec: selector or element; returns null if missing / hidden. */
function mirror(target, label, hint = '', {wide = false, events = null, redraw = null, rows = 4, always = false, password = false} = {}) {
    const src = typeof target === 'string' ? $q(target) : target;
    if (!src || (!always && !shown(src))) return null;
    const type = (src.type || '').toLowerCase();
    const tag = src.tagName;
    const after = (isChoice) => { if (redraw ?? isChoice) refreshSoon(H.page(), 300); };
    if (type === 'checkbox') {
        return el('label', {class: `rvo-lcheck rvo-mcheck${wide ? ' wide' : ''}`, title: hint || undefined},
            el('input', {type: 'checkbox', checked: src.checked || undefined, onchange: event => {
                src.checked = event.target.checked;
                fire(src, events || ['input', 'change']);
                after(true);
            }}),
            el('span', {text: label}), hint ? el('small', {text: hint}) : null);
    }
    let control;
    if (tag === 'SELECT') {
        control = el('select', {class: 'rvo-cinput'});
        for (const child of src.children) {
            if (child.tagName === 'OPTGROUP') {
                const group = el('optgroup', {label: child.label});
                for (const option of child.children) group.append(el('option', {value: option.value, text: option.textContent.trim(), disabled: option.disabled || undefined}));
                control.append(group);
            } else if (child.tagName === 'OPTION') control.append(el('option', {value: child.value, text: child.textContent.trim(), disabled: child.disabled || undefined}));
        }
        control.value = src.value;
        if (src.multiple) { control.multiple = true; for (const [i, option] of [...src.options].entries()) control.options[i].selected = option.selected; }
        control.addEventListener('change', () => {
            if (src.multiple) [...control.options].forEach((option, i) => { src.options[i].selected = option.selected; });
            else $(src).val(control.value);
            fire(src, events || ['change']);
            after(true);
        });
    } else if (tag === 'TEXTAREA') {
        control = area(src.value, value => { src.value = value; fire(src, events || ['input']); }, {rows});
        control.addEventListener('change', () => fire(src, ['change']));
        wide = true;
    } else if (type === 'range' || type === 'number') {
        const attrs = {min: src.min || undefined, max: src.max || undefined, step: src.step || 'any'};
        const number = el('input', {type: 'number', class: 'rvo-cinput rvo-snum', value: src.value, ...attrs});
        const write = value => { src.value = value; fire(src, events || (type === 'range' ? ['input', 'change'] : ['input', 'change'])); };
        if (type === 'range') {
            const slider = el('input', {type: 'range', class: 'rvo-srange', value: src.value, ...attrs});
            slider.addEventListener('input', () => { number.value = slider.value; write(slider.value); });
            number.addEventListener('change', () => { slider.value = number.value; write(number.value); number.value = src.value; slider.value = src.value; });
            control = el('div', {class: 'rvo-srow'}, slider, number);
        } else {
            number.addEventListener('change', () => { write(number.value); setTimeout(() => { number.value = src.value; }, 30); });
            control = number;
        }
    } else {
        control = el('input', {type: password || type === 'password' ? 'password' : 'text', class: 'rvo-cinput', value: src.value, placeholder: src.placeholder || undefined, autocomplete: 'off'});
        control.addEventListener('input', () => { src.value = control.value; fire(src, events || ['input']); });
        control.addEventListener('change', () => fire(src, ['change']));
    }
    return field(label, control, hint, wide);
}

/** A group of radio buttons (same name) as a select. */
function mirrorRadios(name, label, hint = '') {
    const radios = [...document.querySelectorAll(`input[type="radio"][name="${name}"]`)];
    if (!radios.length || !radios.some(shown)) return null;
    const text = radio => (radio.closest('label')?.textContent || radio.nextElementSibling?.textContent || radio.value).trim();
    return field(label, choose(radios.map(r => [r.value, text(r)]), radios.find(r => r.checked)?.value ?? '', value => {
        const radio = radios.find(r => r.value === value);
        if (!radio) return;
        radio.checked = true;
        fire(radio, ['input', 'change', 'click']);
    }), hint);
}

/** A SillyTavern button (it does the work; popups it opens are SillyTavern's small ones). */
function mirrorButton(target, label, faIcon, {after = 500, primary = false} = {}) {
    const src = typeof target === 'string' ? $q(target) : target;
    if (!src) return null;
    return button(label, faIcon, () => { $(src).trigger('click'); if (after) refreshSoon(H.page(), after); }, label, primary ? 'primary' : '');
}

// Labels for a whole extension panel (Summarize, Vector Storage, Expressions, Image generation): the panel's
// inputs in order, each with the text SillyTavern shows next to it.
function labelOf(node) {
    const clean = text => String(text || '').replace(/\s+/g, ' ').trim();
    if (node.id) {
        const forLabel = document.querySelector(`label[for="${CSS.escape(node.id)}"]`);
        if (forLabel && clean(forLabel.textContent)) return clean(forLabel.textContent);
    }
    const wrapper = node.closest('label');
    if (wrapper) {
        const copy = wrapper.cloneNode(true);
        copy.querySelectorAll('input, select, textarea, .fa-solid').forEach(n => n.remove());
        if (clean(copy.textContent)) return clean(copy.textContent);
    }
    for (let n = node, depth = 0; n && depth < 3; n = n.parentElement, depth++) {
        for (let prev = n.previousElementSibling; prev; prev = prev.previousElementSibling) {
            if (prev.matches('input, select, textarea, .menu_button')) break;
            const text = clean(prev.textContent);
            if (text && text.length < 90) return text;
        }
    }
    return clean(node.title || node.placeholder || node.getAttribute('aria-label') || node.id.replace(/[_-]+/g, ' '));
}

function mirrorPanel(panel, {skip = []} = {}) {
    if (!panel) return [];
    const out = [];
    const radiosDone = new Set();
    const nodes = panel.querySelectorAll('input, select, textarea, .menu_button[id], .menu_button[data-action]');
    for (const node of nodes) {
        if (skip.some(s => node.matches(s)) || !shown(node)) continue;
        if (node.matches('input[type="hidden"], input[type="file"], input[type="search"]')) continue;
        if (node.matches('.menu_button')) {
            if (node.closest('.inline-drawer-header')) continue;
            const text = (node.textContent || '').replace(/\s+/g, ' ').trim() || node.title;
            if (text) out.push(el('div', {class: 'rvo-lfield rvo-sbtn'}, button(text.slice(0, 40), 'fa-play', () => { $(node).trigger('click'); refreshSoon(H.page(), 800); }, node.title || text)));
            continue;
        }
        // A number box paired with a slider (SillyTavern's "counter") is the slider's: skip it.
        if (node.type === 'number' || node.type === 'text') {
            const near = [node.parentElement, node.parentElement?.parentElement].filter(Boolean);
            if (/_counter$|_value$/.test(node.id) || (node.type === 'number' && near.some(n => n.querySelector('input[type="range"]')))) continue;
        }
        if (node.type === 'radio') {
            if (radiosDone.has(node.name)) continue;
            radiosDone.add(node.name);
            const group = node.closest('.radio_group, .flex-container, div') || node.parentElement;
            const named = document.querySelector(`label[for="${CSS.escape(node.name)}"]`)?.textContent?.trim();
            const heading = named || group?.previousElementSibling?.textContent?.trim() || labelOf(group || node);
            const control = mirrorRadios(node.name, (heading || node.name).slice(0, 80));
            if (control) out.push(control);
            continue;
        }
        const control = mirror(node, labelOf(node).slice(0, 90));
        if (control) out.push(control);
    }
    return out;
}

function extensionPanel(anchor) {
    const node = $q(anchor);
    return node?.closest('.inline-drawer')?.querySelector('.inline-drawer-content') || node?.closest('.inline-drawer') || null;
}

// --------------------------------------------------------- model & connection --
const PP_FIX = 'strict';
function postProcessingWarning() {
    const c = ctx();
    if (c.mainApi !== 'openai' || c.chatCompletionSettings?.chat_completion_source !== 'custom') return null;
    const value = c.chatCompletionSettings?.custom_prompt_post_processing || '';
    if (value && value !== 'merge' && value !== 'merge_tools') return null;
    return el('div', {class: 'rvo-swarn'}, icon('fa-triangle-exclamation'),
        el('span', {text: 'Prompt post-processing is off. Local chat templates (Qwen, Gemma) then fail on “Continue” when the chat has only the greeting (“No user query found”). Strict puts a user message first and alternates the roles.'}),
        button('Use Strict', 'fa-wand-magic-sparkles', () => applyStrict(), 'Set Prompt Post-Processing to Strict and save it in the connection profile', 'primary'));
}

export async function applyStrict({quiet = false} = {}) {
    const select = $q('#custom_prompt_post_processing');
    if (select) $(select).val(PP_FIX).trigger('change');
    const cm = ctx().extensionSettings.connectionManager;
    const profile = cm?.profiles?.find(p => p.id === cm.selectedProfile);
    if (profile && profile.mode === 'cc') { profile['prompt-post-processing'] = PP_FIX; saveSettings(); }
    if (!quiet) toastr.success('Prompt post-processing: Strict', 'Model & connection');
    refreshSoon('connection', 200);
}

async function renderConnection(wrap) {
    const c = ctx();
    const api = $q('#main_api')?.value || c.mainApi;
    const status = [...document.querySelectorAll('#rm_api_block .online_status_text')].find(shown)?.textContent?.trim() || '';
    const online = !!c.onlineStatus && c.onlineStatus !== 'no_connection';
    const rocsy = el('div', {class: 'rvo-sbody rvo-srocsy'});
    if (globalThis.RocsyAddons?.mountConnection) globalThis.RocsyAddons.mountConnection(rocsy);
    else rocsy.append(note('Rocsy Addons isn’t loaded, so the llama-swap shortcuts aren’t available.'));

    const profiles = $q('#connection_profiles');
    const profileSection = profiles ? section('profiles', 'Connection profile', 'fa-address-book', [
        mirror(profiles, 'Profile', 'A saved API, model, preset and prompt settings. Picking one applies it.', {always: true, redraw: true, wide: true}),
        el('div', {class: 'rvo-lfield wide rvo-sbtns'},
            mirrorButton('#update_connection_profile', 'Save changes to it', 'fa-floppy-disk'),
            mirrorButton('#create_connection_profile', 'Save as new', 'fa-plus'),
            mirrorButton('#edit_connection_profile', 'Rename / choose fields', 'fa-pen'),
            mirrorButton('#reload_connection_profile', 'Reapply', 'fa-rotate'),
            mirrorButton('#delete_connection_profile', 'Delete', 'fa-trash-can')),
    ]) : null;

    const apiBlock = $q('#rm_api_block');
    const apiFields = mirrorPanel(apiBlock, {skip: ['#main_api', '#connection_profiles', '#api_button_openai', '.api_button', '[id^="api_key_"]', '#custom_prompt_post_processing', '.api_loading', '#openai_api_usage']});
    const keyBox = [...(apiBlock?.querySelectorAll('[id^="api_key_"]') || [])].find(shown);
    const connect = [...(apiBlock?.querySelectorAll('.api_button') || [])].find(shown);
    put(wrap, 
        el('div', {class: `rvo-sstatus${online ? ' ok' : ''}`}, icon(online ? 'fa-circle-check' : 'fa-circle-xmark'),
            el('div', {}, el('strong', {text: online ? 'Connected' : 'Not connected'}), el('small', {text: status || (online ? String(c.onlineStatus) : '')})),
            connect ? button(online ? 'Reconnect' : 'Connect', 'fa-plug', () => { $(connect).trigger('click'); refreshSoon('connection', 2500); }, 'Connect with these settings', 'primary') : null),
        postProcessingWarning(),
        section('rocsy', 'Rocsy llama-swap', 'fa-server', [rocsy], {grid: false, sub: 'your local models'}),
        profileSection,
        section('api', 'API', 'fa-plug', [
            mirror('#main_api', 'API type', 'Chat Completion fits llama-swap and most services.', {always: true, redraw: true}),
            ...apiFields,
            keyBox ? field('API key', el('div', {class: 'rvo-srow'},
                el('input', {type: 'password', class: 'rvo-cinput', placeholder: keyBox.placeholder || 'Paste a key to replace the saved one', autocomplete: 'off',
                    onchange: event => { if (!event.target.value) return; $(keyBox).val(event.target.value).trigger('input').trigger('change'); event.target.value = ''; toastr.info('Key saved. Connect to use it.'); }})),
            'Saved on the server as a SillyTavern secret; it isn’t shown again.', true) : null,
            api === 'openai' ? mirror('#custom_prompt_post_processing', 'Prompt post-processing', 'Strict is the safe choice for local models.', {redraw: true}) : null,
        ], {sub: api}),
    );
}

// ---------------------------------------------------------- response settings --
const SAMPLERS = [
    ['#temp_openai', 'Temperature', 'Higher is more varied, lower more predictable.'],
    ['#top_p_openai', 'Top P', ''], ['#top_k_openai', 'Top K', '0 = off.'], ['#min_p_openai', 'Min P', ''],
    ['#top_a_openai', 'Top A', ''], ['#freq_pen_openai', 'Frequency penalty', ''], ['#pres_pen_openai', 'Presence penalty', ''],
    ['#repetition_penalty_openai', 'Repetition penalty', ''], ['#seed_openai', 'Seed', '-1 = random.'],
];
const UTILITY_PROMPTS = [
    ['#impersonation_prompt_textarea', 'Impersonation prompt'], ['#wi_format_textarea', 'World Info format'],
    ['#scenario_format_textarea', 'Scenario format'], ['#personality_format_textarea', 'Personality format'],
    ['#group_nudge_prompt_textarea', 'Group nudge'], ['#newchat_prompt_textarea', 'New chat'],
    ['#newgroupchat_prompt_textarea', 'New group chat'], ['#newexamplechat_prompt_textarea', 'New example chat'],
    ['#continue_nudge_prompt_textarea', 'Continue nudge'], ['#send_if_empty_textarea', 'Send if the box is empty'],
];

async function renderResponse(wrap) {
    const c = ctx();
    const api = $q('#main_api')?.value || c.mainApi;
    if (api !== 'openai') {
        const panel = $q('#left-nav-panel');
        put(wrap, note(`Settings for the ${api} API.`),
            section('all', 'Response settings', 'fa-sliders', mirrorPanel(panel, {skip: ['#lm_button_panel_pin', '[id$="_counter"]', '[id^="logit_bias"]']})));
        return;
    }
    const oai = c.chatCompletionSettings || {};
    const thinking = oai.show_thoughts || /think|boulesis|qwen3|gemma4|r1/i.test(oai.custom_model || '');
    const short = thinking && Number(oai.openai_max_tokens) < 2048;
    put(wrap, 
        short ? el('div', {class: 'rvo-swarn'}, icon('fa-brain'),
            el('span', {text: `Response length is ${oai.openai_max_tokens} tokens. A thinking model spends part of that on its thinking, so replies can come back empty.`}),
            button('Use 8192', 'fa-wand-magic-sparkles', () => { const n = $q('#openai_max_tokens'); if (n) { $(n).val(8192).trigger('input').trigger('change'); refreshSoon('response', 200); } }, 'Set the response length to 8192 tokens', 'primary')) : null,
        section('preset', 'Preset', 'fa-layer-group', [
            mirror('#settings_preset_openai', 'Preset', 'Picking one loads all of its settings. Changes stay unsaved in the preset until you update it.', {always: true, redraw: true, wide: true}),
            el('div', {class: 'rvo-lfield wide rvo-sbtns'},
                mirrorButton('#update_oai_preset', 'Update preset', 'fa-floppy-disk'),
                mirrorButton('#new_oai_preset', 'Save as new', 'fa-plus'),
                mirrorButton('[data-preset-manager-rename="openai"]', 'Rename', 'fa-pen'),
                mirrorButton('#import_oai_preset', 'Import', 'fa-file-import'),
                mirrorButton('#export_oai_preset', 'Export', 'fa-file-export', {after: 0}),
                mirrorButton('#delete_oai_preset', 'Delete', 'fa-trash-can')),
        ]),
        section('length', 'Length', 'fa-ruler-horizontal', [
            mirror('#openai_max_context', 'Context size (tokens)', 'How much of the chat the model sees. Keep it at or under the model’s context.'),
            mirror('#oai_max_context_unlocked', 'Unlock context size', 'Allow more than the usual maximum.', {redraw: true}),
            mirror('#openai_max_tokens', 'Response length (tokens)', 'A thinking model counts its thinking too.'),
            mirror('#n_openai', 'Swipes per generation', ''),
            mirror('#stream_toggle', 'Stream replies', 'Show the reply while it’s written.'),
        ]),
        section('sampling', 'Sampling', 'fa-dice', SAMPLERS.map(([sel, label, hint]) => mirror(sel, label, hint))),
        section('reasoning', 'Thinking', 'fa-brain', [
            mirror('#openai_show_thoughts', 'Request model reasoning', 'Show the model’s thinking above the reply.'),
            mirror('#openai_reasoning_effort', 'Reasoning effort', ''),
            mirror('#openai_verbosity', 'Verbosity', ''),
        ]),
        section('behaviour', 'Behaviour', 'fa-gears', [
            mirrorRadios('names_behavior_radio', 'Character names', 'How names are added to messages.') || mirrorNamesBehaviour(),
            mirrorContinuePostfix(),
            mirror('#continue_prefill', 'Continue prefill', 'Continue by sending the last message as the assistant’s start.'),
            mirror('#squash_system_messages', 'Squash system messages', 'Join system messages that follow each other.'),
            mirror('#use_sysprompt', 'Use system prompt', ''),
            mirror('#openai_function_calling', 'Enable function calling', ''),
            mirror('#openai_media_inlining', 'Send images', 'Attach images in the chat to the prompt (vision models).'),
            mirror('#openai_inline_image_quality', 'Image quality', ''),
            mirror('#openai_request_images', 'Request images', ''),
            mirror('#openai_enable_web_search', 'Web search', ''),
            mirror('#claude_assistant_prefill', 'Assistant prefill', ''),
            mirror('#claude_assistant_impersonation', 'Assistant impersonation prefill', ''),
        ]),
        await promptsSection(),
        section('utility', 'Utility prompts', 'fa-scroll', UTILITY_PROMPTS.map(([sel, label]) => mirror(sel, label, '', {rows: 3})), {open: false, sub: 'formats and nudges'}),
    );
}

function mirrorNamesBehaviour() {
    const radios = ['#character_names_none', '#character_names_default', '#character_names_completion', '#character_names_content'].map(s => $q(s)).filter(Boolean);
    if (!radios.length || !radios.some(shown)) return null;
    const labels = ['None', 'Default', 'Completion object', 'Message content'];
    return field('Character names', choose(radios.map((r, i) => [r.id, labels[i]]), radios.find(r => r.checked)?.id || '', id => {
        const r = $q(`#${id}`); if (!r) return; r.checked = true; fire(r, ['input', 'change', 'click']);
    }), 'How names are added to messages.');
}
function mirrorContinuePostfix() {
    const ids = [['continue_postfix_none', 'None'], ['continue_postfix_space', 'Space'], ['continue_postfix_newline', 'Newline'], ['continue_postfix_double_newline', 'Double newline']];
    const radios = ids.map(([id]) => $q(`#${id}`)).filter(Boolean);
    if (!radios.length || !radios.some(shown)) return null;
    return field('Continue postfix', choose(ids.filter(([id]) => $q(`#${id}`)), radios.find(r => r.checked)?.id || '', id => {
        const r = $q(`#${id}`); if (!r) return; r.checked = true; fire(r, ['input', 'change', 'click']);
    }), 'Added between the old text and the continuation.');
}

// The prompt list (SillyTavern's Prompt Manager): order, on/off, and each prompt's text.
const ROLES = [['system', 'System'], ['user', 'User'], ['assistant', 'Assistant']];
async function promptsSection() {
    let pm;
    try { pm = (await openaiModule()).promptManager; } catch { pm = null; }
    if (!pm?.serviceSettings) return section('prompts', 'Prompts', 'fa-list-ol', [note('The prompt list isn’t available yet.')], {grid: false});
    const character = pm.activeCharacter;
    const order = pm.getPromptOrderForCharacter(character);
    const list = el('div', {class: 'rvo-plist'});
    const saveAll = debounce(() => { pm.saveServiceSettings(); try { pm.render(false); } catch { /* fine */ } }, 500);
    const saveNow = () => { pm.saveServiceSettings(); try { pm.render(false); } catch { /* fine */ } refreshSoon('response', 100); };
    const tokens = id => pm.tokenHandler?.getCounts?.()?.[id] ?? pm.tokenUsage?.[id];
    for (const [index, entry] of order.entries()) {
        const prompt = pm.getPromptById(entry.identifier);
        if (!prompt) continue;
        const canToggle = pm.isPromptToggleAllowed(prompt);
        const canEdit = pm.isPromptEditAllowed(prompt) && !prompt.marker;
        const canDelete = pm.isPromptDeletionAllowed(prompt);
        const open = state.promptOpen.has(prompt.identifier);
        const n = tokens(prompt.identifier);
        const head = el('div', {class: 'rvo-phead'},
            canToggle ? toggle(entry.enabled, on => { entry.enabled = on; saveNow(); }, '', 'On / off') : el('span', {class: 'rvo-pfixed', title: 'Always on'}, icon('fa-lock')),
            el('button', {class: 'rvo-pname', type: 'button', disabled: !canEdit || undefined, onclick: () => { open ? state.promptOpen.delete(prompt.identifier) : state.promptOpen.add(prompt.identifier); refreshSoon('response', 0); }},
                el('strong', {text: prompt.name || prompt.identifier}),
                el('small', {text: [prompt.marker ? 'placeholder' : (prompt.role || 'system'), prompt.injection_position === 1 ? `in chat @ ${prompt.injection_depth ?? 4}` : '', n ? `${n} tokens` : ''].filter(Boolean).join(' · ')})),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Move up', disabled: index === 0 || undefined, onclick: () => { order.splice(index - 1, 0, order.splice(index, 1)[0]); saveNow(); }}, icon('fa-arrow-up')),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Move down', disabled: index === order.length - 1 || undefined, onclick: () => { order.splice(index + 1, 0, order.splice(index, 1)[0]); saveNow(); }}, icon('fa-arrow-down')),
            canDelete ? el('button', {class: 'rvo-icon-button rvo-ldelete', type: 'button', title: 'Delete this prompt', onclick: async () => {
                if (!await confirmPopup(`Delete “${prompt.name}”?`, 'The prompt is removed from the list and deleted.')) return;
                pm.detachPrompt(prompt, character);
                const i = pm.serviceSettings.prompts.indexOf(prompt);
                if (i >= 0) pm.serviceSettings.prompts.splice(i, 1);
                saveNow();
            }}, icon('fa-trash-can')) : null);
        const card = el('article', {class: `rvo-prompt${entry.enabled ? ' on' : ''}${open ? ' open' : ''}`}, head);
        if (open && canEdit) {
            const set = (key, value) => { prompt[key] = value; saveAll(); };
            card.append(el('div', {class: 'rvo-lset-grid'},
                field('Name', textInput(prompt.name, v => set('name', v))),
                field('Role', choose(ROLES, prompt.role || 'system', v => set('role', v))),
                field('Position', choose([[0, 'Relative (in the list order)'], [1, 'In chat, at a depth']], prompt.injection_position || 0, v => { set('injection_position', Number(v)); refreshSoon('response', 100); })),
                prompt.injection_position === 1 ? field('Depth', numberInput(prompt.injection_depth ?? 4, v => set('injection_depth', Math.max(0, Number(v) || 0)), {min: 0})) : null,
                prompt.injection_position === 1 ? field('Order', numberInput(prompt.injection_order ?? 100, v => set('injection_order', Number(v) || 0))) : null,
                field('Text', area(prompt.content, v => set('content', v), {rows: 8}), 'Macros like {{char}} and {{user}} work here.', true)));
        }
        list.append(card);
    }
    const add = button('Add a prompt', 'fa-plus', async () => {
        const name = await askText('Name of the new prompt');
        if (!name) return;
        const id = uid();
        pm.addPrompt({name, role: 'system', content: '', system_prompt: false, marker: false, injection_position: 0, injection_depth: 4}, id);
        pm.appendPrompt(pm.getPromptById(id), character);
        const added = pm.getPromptOrderEntry(character, id);
        if (added) added.enabled = true;
        state.promptOpen.add(id);
        saveNow();
    });
    return section('prompts', 'Prompts', 'fa-list-ol', [note('The prompt is built from these, top to bottom. Placeholders (chat history, character card, examples …) are filled in by SillyTavern.'), list, el('div', {class: 'rvo-sbtns'}, add)], {grid: false, sub: `${order.filter(e => e.enabled).length} of ${order.length} on`});
}

// ------------------------------------------------------------------ persona --
const personaImage = id => ctx().getThumbnailUrl('persona', id);
const personaDesc = id => (ctx().powerUserSettings.persona_descriptions ||= {})[id] ||= {description: '', position: 0, depth: 2, role: 0, lorebook: '', connections: [], title: ''};

const personaFull = id => `/User Avatars/${encodeURIComponent(id)}`;
const personaFlags = (id, current) => {
    const c = ctx(); const pu = c.powerUserSettings;
    const desc = pu.persona_descriptions?.[id] || {};
    return {current: id === current, isDefault: pu.default_persona === id, chat: c.chatMetadata?.persona === id, connections: desc.connections || [], desc};
};

async function renderPersona(wrap) {
    const pm = await personasModule();
    const c = ctx();
    const pu = c.powerUserSettings;
    if (state.persona && !Object.hasOwn(pu.personas || {}, state.persona)) state.persona = '';
    if (state.persona) return renderOnePersona(wrap, pm, state.persona);
    const current = pm.user_avatar;
    const ids = Object.keys(pu.personas || {});
    const flags = Object.fromEntries(ids.map(id => [id, personaFlags(id, current)]));
    state.personaFilter ||= 'all';
    state.personaSort ||= 'name';
    const grid = el('div', {class: 'rvo-cgrid rvo-pgallery'});
    const chipBox = el('div');
    const FILTERS = [['all', 'All', () => true], ['default', 'Default', id => flags[id].isDefault], ['connected', 'Connected', id => flags[id].connections.length > 0],
        ['nodesc', 'No description', id => !String(flags[id].desc.description || '').trim()]];
    const draw = () => {
        chipBox.replaceChildren(H.filterChips(FILTERS.map(([k, label, fn]) => [k, label, ids.filter(fn).length]), state.personaFilter, v => { state.personaFilter = v; draw(); }));
        const q = state.personaQ.toLowerCase();
        const test = FILTERS.find(f => f[0] === state.personaFilter)?.[2] || (() => true);
        const list = ids.filter(id => test(id) && (!q || `${pu.personas[id]} ${flags[id].desc.title || ''} ${flags[id].desc.description || ''}`.toLowerCase().includes(q)))
            .sort(state.personaSort === 'connections' ? (a, b) => flags[b].connections.length - flags[a].connections.length || String(pu.personas[a]).localeCompare(String(pu.personas[b]))
                : (a, b) => String(pu.personas[a]).localeCompare(String(pu.personas[b])));
        grid.replaceChildren(...list.map(id => personaTile(pm, id, flags[id])));
        if (!list.length) grid.replaceChildren(el('p', {class: 'rvo-empty', text: 'No personas match.'}));
    };
    const defaultName = pu.default_persona ? pu.personas[pu.default_persona] : '';
    put(wrap,
        H.pageBanner('fa-masks-theater', 'Personas', 'You in the chat: the persona’s name replaces {{user}} and its description goes into the prompt. Connect one to a character and it’s picked for you.', [
            ['Personas', ids.length, 'fa-user'], ['Connected', ids.filter(id => flags[id].connections.length).length, 'fa-link'],
            ['Default', defaultName || 'none', 'fa-star']]),
        current && pu.personas[current] ? personaSpotlight(pm, current, flags[current]) : null,
        el('div', {class: 'rvo-ctools'},
            el('input', {type: 'search', class: 'rvo-cinput', placeholder: 'Search personas', value: state.personaQ, oninput: event => { state.personaQ = event.target.value; draw(); }}),
            choose([['name', 'Name A–Z'], ['connections', 'Most connected']], state.personaSort, v => { state.personaSort = v; draw(); }, {'aria-label': 'Sort'}),
            button('Create', 'fa-plus', () => createPersona(), 'Create a persona', 'primary'),
            button('From a picture', 'fa-image', () => { $('#avatar_upload_overwrite').val(''); $('#avatar_upload_file').trigger('click'); waitForPersonas(); }, 'Upload a picture and make a persona from it')),
        chipBox,
        grid,
        section('personaopts', 'Persona options', 'fa-sliders', [
            mirror('#persona_show_notifications', 'Show notifications when switching', '', {always: true}),
            mirror('#persona_allow_multi_connections', 'Allow several personas per character', '', {always: true}),
            mirror('#persona_auto_lock', 'Lock the chosen persona to the chat', 'Picking a persona also locks it to the open chat.', {always: true}),
        ], {open: false}));
    draw();
}

function connectionFaces(connections) {
    const c = ctx();
    const who = connections.map(conn => conn.type === 'character' ? c.characters.find(ch => ch.avatar === conn.id) : null).filter(Boolean);
    const groups = connections.filter(conn => conn.type === 'group').length;
    if (!who.length && !groups) return null;
    return el('span', {class: 'rvo-faces', title: `Connected: ${[...who.map(ch => ch.name), groups ? `${groups} group${groups === 1 ? '' : 's'}` : ''].filter(Boolean).join(', ')}`},
        ...who.slice(0, 4).map(ch => el('img', {src: c.getThumbnailUrl('avatar', ch.avatar), alt: '', loading: 'lazy'})),
        who.length > 4 || groups ? el('span', {text: `+${Math.max(0, who.length - 4) + groups}`}) : null);
}

function personaSpotlight(pm, id, f) {
    const c = ctx(); const pu = c.powerUserSettings;
    const inChat = !!c.getCurrentChatId?.();
    return el('section', {class: 'rvo-dhero rvo-pspot'}, H.heroBackdrop(personaFull(id)),
        el('div', {class: 'rvo-dpic'}, el('img', {src: personaFull(id), alt: ''})),
        el('div', {class: 'rvo-dhead'},
            el('small', {class: 'rvo-dkicker', text: 'You are chatting as'}),
            el('h2', {text: pu.personas[id]}),
            f.desc.title ? el('div', {class: 'rvo-dcreator'}, icon('fa-tag'), el('span', {text: f.desc.title})) : null,
            el('p', {class: 'rvo-dtagline', text: String(f.desc.description || 'No description yet: open the persona to write one.').slice(0, 280)}),
            el('div', {class: 'rvo-dactions'},
                button('Edit', 'fa-pen', () => H.showPage('persona', {persona: id}), 'Edit this persona', 'primary'),
                el('span', {class: 'rvo-pswitches'},
                    toggle(f.isDefault, on => { pu.default_persona = on ? id : null; saveSettings(); refreshSoon('persona', 50); }, 'Default for new chats'),
                    inChat ? toggle(f.chat, async on => { if (on) c.chatMetadata.persona = id; else delete c.chatMetadata.persona; await c.saveMetadata(); refreshSoon('persona', 50); }, 'Locked to this chat') : null),
                connectionFaces(f.connections))));
}

function personaTile(pm, id, f) {
    const pu = ctx().powerUserSettings;
    const name = pu.personas[id];
    const open = () => H.showPage('persona', {persona: id});
    return el('article', {class: `rvo-ccard rvo-ptile${f.current ? ' on' : ''}`},
        el('button', {class: 'rvo-ccard-open', type: 'button', title: `Open ${name}`, onclick: open},
            el('span', {class: 'rvo-ccard-pic'}, el('img', {src: personaFull(id), alt: '', loading: 'lazy', decoding: 'async'}),
                el('span', {class: 'rvo-pflags'},
                    f.current ? el('em', {class: 'in-use', title: 'In use'}, icon('fa-circle-check'), el('span', {text: 'In use'})) : null,
                    f.isDefault ? el('em', {title: 'Default for new chats'}, icon('fa-star')) : null,
                    f.chat ? el('em', {title: 'Locked to this chat'}, icon('fa-lock')) : null)),
            el('span', {class: 'rvo-ccard-body'},
                el('strong', {class: 'rvo-ccard-name', text: name}),
                f.desc.title ? el('small', {class: 'rvo-ptitle', text: f.desc.title}) : null,
                el('span', {class: `rvo-ccard-text${f.desc.description ? '' : ' rvo-faint'}`, text: String(f.desc.description || 'No description').slice(0, 220)}))),
        el('div', {class: 'rvo-ccard-actions'},
            f.current ? el('span', {class: 'rvo-pbadge-on'}, icon('fa-circle-check'), el('span', {text: 'In use'}))
                : button('Use', 'fa-check', async () => { await pm.setUserAvatar(id); refreshSoon('persona', 100); }, `Use ${name} now`, 'primary'),
            connectionFaces(f.connections),
            el('button', {class: 'rvo-icon-button', type: 'button', title: `Edit ${name}`, onclick: open}, icon('fa-pen'))));
}

async function waitForPersonas() {
    const before = Object.keys(ctx().powerUserSettings.personas || {}).length;
    for (let i = 0; i < 120; i++) {
        await wait(500);
        if (Object.keys(ctx().powerUserSettings.personas || {}).length !== before) { refreshSoon('persona', 100); return; }
    }
}

async function createPersona() {
    const name = await askText('Name of the new persona');
    if (!name) return;
    const pm = await personasModule();
    const id = `${Date.now()}-${name.replace(/[^a-zA-Z0-9]/g, '')}.png`;
    await pm.initPersona(id, name, '', '');
    try {
        const blob = await (await fetch('img/user-default.png')).blob();
        const form = new FormData();
        form.append('avatar', new File([blob], 'avatar.png', {type: 'image/png'}));
        form.append('overwrite_name', id);
        const headers = ctx().getRequestHeaders({omitContentType: true});
        delete headers['Content-Type'];
        await fetch('/api/avatars/upload', {method: 'POST', headers, cache: 'no-cache', body: form});
        await pm.getUserAvatars(false);
    } catch (error) { console.warn('[Rocsy] persona picture', error); }
    H.showPage('persona', {persona: id});
}

async function deletePersona(pm, id) {
    const pu = ctx().powerUserSettings;
    const name = pu.personas[id] || id;
    if (!await confirmPopup(`Delete ${name}?`, 'The persona, its picture and its description are deleted.')) return;
    const response = await fetch('/api/avatars/delete', {method: 'POST', headers: ctx().getRequestHeaders(), body: JSON.stringify({avatar: id})});
    if (!response.ok) return toastr.error(`Couldn’t delete ${name}.`);
    const wasCurrent = pm.user_avatar === id;
    delete pu.personas[id];
    delete pu.persona_descriptions[id];
    if (pu.default_persona === id) pu.default_persona = null;
    if (ctx().chatMetadata?.persona === id) { delete ctx().chatMetadata.persona; await ctx().saveMetadata(); }
    saveSettings();
    await pm.getUserAvatars(false);
    if (wasCurrent) {
        const next = pu.default_persona || Object.keys(pu.personas)[0];
        if (next) await pm.setUserAvatar(next);
    }
    toastr.success(`Deleted ${name}`);
    H.showPage('persona', {persona: ''});
}

async function renderOnePersona(wrap, pm, id) {
    const c = ctx();
    const pu = c.powerUserSettings;
    const desc = personaDesc(id);
    const isCurrent = () => pm.user_avatar === id;
    const name = pu.personas[id];
    const save = debounce(() => saveSettings(), 400);
    const tokens = el('strong', {id: 'rvo-pstat-tokens', text: '…'});
    const countTokens = debounce(text => (text ? c.getTokenCountAsync(text) : Promise.resolve(0)).then(n => { tokens.textContent = H.compactNumber(n); }).catch(() => { tokens.textContent = '?'; }), 400);
    const setDesc = (key, value) => {
        desc[key] = value;
        if (isCurrent()) {
            if (key === 'description') $('#persona_description').val(value).trigger('input');
            else if (key === 'lorebook') pu.persona_description_lorebook = value;
            else if (key === 'position') $('#persona_description_position').val(value).trigger('input').trigger('change');
            else if (key === 'depth') $('#persona_depth_value').val(value).trigger('input');
            else if (key === 'role') $('#persona_depth_role').val(value).trigger('input').trigger('change');
        }
        if (key === 'description') countTokens(value);
        save();
    };
    const positions = [...($q('#persona_description_position')?.options || [])].map(o => [o.value, o.textContent.trim()]);
    let worlds = [];
    try { const wi = await worldModule(); worlds = wi.world_names || []; } catch { /* fine */ }
    const inChat = !!c.getCurrentChatId?.();
    const chId = c.characterId;
    const character = !c.groupId && chId !== undefined ? c.characters[chId] : null;
    const connected = (desc.connections || []).map(conn => {
        const who = conn.type === 'group' ? c.groups.find(g => g.id === conn.id) : c.characters.find(ch => ch.avatar === conn.id);
        return {conn, label: who?.name || conn.id, image: conn.type === 'character' && who ? c.getThumbnailUrl('avatar', who.avatar) : ''};
    });
    const connList = el('div', {class: 'rvo-pconns'}, ...connected.map(({conn, label, image}) => el('span', {class: 'rvo-pconn'},
        image ? el('img', {src: image, alt: ''}) : el('span', {class: 'rvo-pconn-group'}, icon('fa-users')), el('span', {text: label}),
        el('button', {class: 'rvo-icon-button', type: 'button', title: `Disconnect ${label}`, onclick: () => {
            desc.connections = desc.connections.filter(x => !(x.type === conn.type && x.id === conn.id)); saveSettings(); refreshSoon('persona', 50);
        }}, icon('fa-xmark')))));
    const addConn = choose([['', 'Connect a character or group…'], ...c.characters.filter(ch => ch?.avatar && !(desc.connections || []).some(x => x.id === ch.avatar)).map(ch => [`character:${ch.avatar}`, ch.name]),
        ...c.groups.filter(g => !(desc.connections || []).some(x => x.id === g.id)).map(g => [`group:${g.id}`, `Group: ${g.name}`])], '', value => {
        if (!value) return;
        const [type, ...rest] = value.split(':');
        const conn = {type, id: rest.join(':')};
        if (!pu.persona_allow_multi_connections) for (const other of Object.values(pu.persona_descriptions)) {
            if (other !== desc && other.connections) other.connections = other.connections.filter(x => !(x.type === conn.type && x.id === conn.id));
        }
        (desc.connections ||= []).push(conn); saveSettings(); refreshSoon('persona', 50);
    });
    const picture = el('button', {class: 'rvo-dpic rvo-ppick', type: 'button', title: 'Change the picture', onclick: () => {
        $('#avatar_upload_overwrite').val(id); $('#avatar_upload_file').trigger('click');
    }}, el('img', {src: `${personaFull(id)}?t=${Date.now()}`, alt: ''}), el('span', {class: 'rvo-ppick-cam'}, icon('fa-camera')));
    const words = String(desc.description || '').trim().split(/\s+/).filter(Boolean).length;
    const hero = el('section', {class: 'rvo-dhero'}, H.heroBackdrop(personaFull(id)), picture,
        el('div', {class: 'rvo-dhead'},
            el('small', {class: 'rvo-dkicker', text: isCurrent() ? 'Persona · in use' : 'Persona'}),
            el('input', {class: 'rvo-dname-input', value: name, 'aria-label': 'Name', title: 'Name (replaces {{user}})', onchange: async event => {
                const value = event.target.value.trim();
                if (!value) { event.target.value = pu.personas[id]; return; }
                pu.personas[id] = value;
                if (isCurrent()) (await scriptModule()).setUserName(value, {toastPersonaNameChange: false});
                saveSettings(); H.renderHeader();
            }}),
            el('input', {class: 'rvo-dtitle-input', value: desc.title || '', placeholder: 'Add a title (shown only here)', 'aria-label': 'Title', onchange: event => setDesc('title', event.target.value.trim())}),
            el('div', {class: 'rvo-dactions'},
                isCurrent() ? el('span', {class: 'rvo-pbadge-on'}, icon('fa-circle-check'), el('span', {text: 'In use'}))
                    : button('Use this persona', 'fa-check', async () => { await pm.setUserAvatar(id); refreshSoon('persona', 100); }, 'Use it now', 'primary'),
                button('Delete', 'fa-trash-can', () => deletePersona(pm, id), 'Delete this persona', 'danger'))));
    const main = el('div', {class: 'rvo-dmain'},
        section('pdesc', 'Description', 'fa-align-left', [
            field('', area(desc.description, v => setDesc('description', v), {rows: 10, placeholder: 'Who you are: looks, personality, background. {{user}} is this persona’s name.'}), '', true),
        ], {sub: 'what the characters know about you'}),
        section('pplace', 'Where it goes in the prompt', 'fa-arrow-down-short-wide', [
            positions.length ? field('Position', choose(positions, desc.position ?? 0, v => { setDesc('position', Number(v)); refreshSoon('persona', 100); })) : null,
            Number(desc.position) === 4 ? field('Depth', numberInput(desc.depth ?? 2, v => setDesc('depth', Math.max(0, Number(v) || 0)), {min: 0}), '0 = after the last message.') : null,
            Number(desc.position) === 4 ? field('Role', choose([[0, 'System'], [1, 'User'], [2, 'Assistant']], desc.role ?? 0, v => setDesc('role', Number(v)))) : null,
            field('Persona lorebook', choose([['', 'None'], ...worlds.map(w => [w, w])], desc.lorebook || '', v => setDesc('lorebook', v)), 'Used whenever this persona is.'),
        ]),
        section('pconn', 'Connections', 'fa-link', [
            note('Opening a chat with a connected character or group picks this persona for you.'),
            connected.length ? connList : note('Not connected to anyone yet.', 'rvo-faint'),
            el('div', {class: 'rvo-srow'}, addConn,
                character && !(desc.connections || []).some(x => x.id === character.avatar)
                    ? button(`Connect ${character.name}`, 'fa-link', () => { addConn.value = `character:${character.avatar}`; addConn.dispatchEvent(new Event('change')); }) : null),
        ], {grid: false, sub: `${connected.length}`}));
    const side = el('aside', {class: 'rvo-dside'},
        H.sideCard('Use', 'fa-toggle-on',
            el('div', {class: 'rvo-pside-switches'},
                toggle(pu.default_persona === id, on => { pu.default_persona = on ? id : null; saveSettings(); refreshSoon('persona', 50); }, 'Default for new chats'),
                inChat ? toggle(c.chatMetadata?.persona === id, async on => {
                    if (on) { if (!isCurrent()) await pm.setUserAvatar(id); c.chatMetadata.persona = id; } else delete c.chatMetadata.persona;
                    await c.saveMetadata(); refreshSoon('persona', 50);
                }, 'Locked to this chat') : el('small', {class: 'rvo-faint', text: 'Open a chat to lock a persona to it.'}))),
        H.sideCard('Stats', 'fa-chart-simple', el('div', {class: 'rvo-stats'},
            el('div', {class: 'rvo-stat', title: 'Tokens'}, icon('fa-coins'), tokens, el('small', {text: 'Tokens'})),
            el('div', {class: 'rvo-stat', title: 'Words'}, icon('fa-font'), el('strong', {text: H.compactNumber(words)}), el('small', {text: 'Words'})),
            el('div', {class: 'rvo-stat', title: 'Connections'}, icon('fa-link'), el('strong', {text: String(connected.length)}), el('small', {text: 'Connected'})))),
        H.sideCard('About', 'fa-circle-info', H.facts([['Position', positions.find(([v]) => String(v) === String(desc.position ?? 0))?.[1] || ''],
            ['Lorebook', desc.lorebook || 'none'], ['File', id]])));
    put(wrap, el('div', {class: 'rvo-details rvo-pdetails'},
        el('div', {class: 'rvo-dtop'}, button('All personas', 'fa-arrow-left', () => H.showPage('persona', {persona: ''}), 'All personas', 'rvo-back')),
        hero,
        el('div', {class: 'rvo-dcols'}, main, side)));
    countTokens(desc.description || '');
}

// ------------------------------------------------------------------- groups --
const STRATEGIES = [[0, 'Natural order (who was mentioned, talkativeness)'], [1, 'List order (each in turn)'], [3, 'Pooled order (who hasn’t spoken yet)'], [2, 'Manual (you pick who answers)']];
const GEN_MODES = [[0, 'Swap character cards'], [1, 'Join character cards (leave out muted)'], [2, 'Join character cards (include muted)']];
const groupThumbs = group => (group.members || []).slice(0, 4).map(avatar => ctx().characters.find(ch => ch.avatar === avatar)).filter(Boolean);

async function renderGroups(wrap) {
    const c = ctx();
    if (state.group === 'new') return renderNewGroup(wrap);
    if (state.group && !c.groups.some(g => g.id === state.group)) state.group = '';
    if (state.group) return renderOneGroup(wrap, state.group);
    const list = el('div', {class: 'rvo-ggrid'});
    const draw = () => {
        const q = state.groupQ.toLowerCase();
        const groups = c.groups.filter(g => !q || g.name.toLowerCase().includes(q)).sort((a, b) => (b.date_last_chat || 0) - (a.date_last_chat || 0) || a.name.localeCompare(b.name));
        list.replaceChildren(...groups.map(groupCard));
        if (!c.groups.length) list.replaceChildren(empty('fa-users', 'No groups yet', 'A group chat lets several characters talk with you and each other.', button('Create a group', 'fa-plus', () => H.showPage('groups', {group: 'new'}), 'Create a group', 'primary')));
        else if (!groups.length) list.replaceChildren(el('p', {class: 'rvo-empty', text: 'No groups match.'}));
    };
    put(wrap, el('div', {class: 'rvo-ctools'},
        el('input', {type: 'search', class: 'rvo-cinput', placeholder: 'Search groups', value: state.groupQ, oninput: event => { state.groupQ = event.target.value; draw(); }}),
        button('Create a group', 'fa-plus', () => H.showPage('groups', {group: 'new'}), 'Create a group', 'primary')), list);
    draw();
}

function groupCard(group) {
    const c = ctx();
    const thumbs = groupThumbs(group);
    return el('article', {class: `rvo-gcard${c.groupId === group.id ? ' on' : ''}`},
        el('button', {class: 'rvo-gcard-main', type: 'button', title: `Open ${group.name}`, onclick: () => H.showPage('groups', {group: group.id})},
            el('span', {class: `rvo-gpics n${Math.min(4, thumbs.length)}`}, ...thumbs.map(ch => el('img', {src: c.getThumbnailUrl('avatar', ch.avatar), alt: '', loading: 'lazy'}))),
            el('span', {class: 'rvo-pcard-text'}, el('strong', {text: group.name}),
                el('small', {text: `${group.members?.length || 0} characters · ${group.chats?.length || 0} chat${group.chats?.length === 1 ? '' : 's'}`}),
                el('span', {class: 'rvo-pcard-desc', text: thumbs.map(ch => ch.name).join(', ')}))),
        button('Chat', 'fa-comments', () => openGroup(group.id), `Chat with ${group.name}`));
}

async function openGroup(id, chatId = '') {
    const gm = await groupsModule();
    H.hidePage();
    if (chatId) await gm.openGroupChat(id, chatId);
    else if (ctx().groupId !== id) await gm.openGroupById(id);
    H.closeSidebarOnMobile();
}

async function saveGroup(group, {now = false} = {}) {
    const gm = await groupsModule();
    await gm.editGroup(group.id, now, false);
    if (now) ctx().eventSource.emit(ctx().eventTypes.GROUP_UPDATED);
}
const saveGroupSoon = debounce(group => saveGroup(group, {now: true}), 600);

function memberPicker(members, onAdd) {
    const c = ctx();
    const box = el('div', {class: 'rvo-gpick'});
    const listBox = el('div', {class: 'rvo-gpick-list'});
    const draw = () => {
        const q = state.memberQ.toLowerCase();
        const available = c.characters.filter(ch => ch?.avatar && !members.includes(ch.avatar) && (!q || ch.name.toLowerCase().includes(q)))
            .sort((a, b) => a.name.localeCompare(b.name)).slice(0, 120);
        listBox.replaceChildren(...available.map(ch => el('button', {class: 'rvo-gpick-item', type: 'button', title: `Add ${ch.name}`, onclick: () => onAdd(ch.avatar)},
            el('img', {src: c.getThumbnailUrl('avatar', ch.avatar), alt: '', loading: 'lazy'}), el('span', {text: ch.name}), icon('fa-plus'))));
        if (!available.length) listBox.replaceChildren(el('p', {class: 'rvo-empty', text: q ? 'No characters match.' : 'Everyone is already in.'}));
    };
    box.append(el('input', {type: 'search', class: 'rvo-cinput', placeholder: 'Find characters to add', value: state.memberQ, oninput: event => { state.memberQ = event.target.value; draw(); }}), listBox);
    draw();
    return box;
}

function membersList(group, {onChange}) {
    const c = ctx();
    const members = group.members || [];
    const disabled = group.disabled_members || [];
    return el('div', {class: 'rvo-gmembers'}, ...members.map((avatar, index) => {
        const ch = c.characters.find(x => x.avatar === avatar);
        const muted = disabled.includes(avatar);
        return el('div', {class: `rvo-gmember${muted ? ' muted' : ''}`},
            el('img', {src: c.getThumbnailUrl('avatar', avatar), alt: ''}),
            el('span', {class: 'rvo-gmember-name'}, el('strong', {text: ch?.name || avatar}), muted ? el('small', {text: 'muted'}) : null),
            el('button', {class: 'rvo-icon-button', type: 'button', title: muted ? 'Unmute (can reply again)' : 'Mute (won’t reply)', onclick: () => {
                group.disabled_members = muted ? disabled.filter(x => x !== avatar) : [...disabled, avatar]; onChange();
            }}, icon(muted ? 'fa-volume-xmark' : 'fa-volume-high')),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Move up', disabled: index === 0 || undefined, onclick: () => { members.splice(index - 1, 0, members.splice(index, 1)[0]); onChange(); }}, icon('fa-arrow-up')),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Move down', disabled: index === members.length - 1 || undefined, onclick: () => { members.splice(index + 1, 0, members.splice(index, 1)[0]); onChange(); }}, icon('fa-arrow-down')),
            el('button', {class: 'rvo-icon-button rvo-ldelete', type: 'button', title: 'Remove from the group', onclick: () => {
                group.members = members.filter(x => x !== avatar); group.disabled_members = disabled.filter(x => x !== avatar); onChange();
            }}, icon('fa-user-minus')));
    }));
}

function groupOptions(group, set) {
    return [
        field('Who replies', choose(STRATEGIES, group.activation_strategy ?? 0, v => set('activation_strategy', Number(v))), 'Natural: whoever fits the last message. List: everyone in turn.'),
        field('Character cards', choose(GEN_MODES, group.generation_mode ?? 0, v => { set('generation_mode', Number(v)); refreshSoon('groups', 100); }), 'Swap: only the speaker’s card is in the prompt. Join: all of them.'),
        Number(group.generation_mode) > 0 ? field('Join prefix', area(group.generation_mode_join_prefix ?? '', v => set('generation_mode_join_prefix', v), {rows: 2}), '{{char}} = that character.', true) : null,
        Number(group.generation_mode) > 0 ? field('Join suffix', area(group.generation_mode_join_suffix ?? '', v => set('generation_mode_join_suffix', v), {rows: 2}), '', true) : null,
        field('Auto mode delay (seconds)', numberInput(group.auto_mode_delay ?? 5, v => set('auto_mode_delay', Math.max(1, Number(v) || 5)), {min: 1}), 'Pause between replies when auto mode is on.'),
        el('div', {class: 'rvo-lset-checks'},
            check('Allow self responses', !!group.allow_self_responses, v => set('allow_self_responses', v), 'A character may answer itself.'),
            check('Hide muted characters’ sprites', !!group.hideMutedSprites, v => set('hideMutedSprites', v)),
            check('Favourite', !!group.fav, v => set('fav', v))),
    ];
}

async function renderNewGroup(wrap) {
    const draft = (state.draft ||= {name: '', members: [], disabled_members: [], activation_strategy: 0, generation_mode: 0, allow_self_responses: false, hideMutedSprites: false, auto_mode_delay: 5, fav: false});
    const set = (key, value) => { draft[key] = value; };
    const create = async () => {
        if (!draft.members.length) return toastr.warning('Add at least one character.');
        const c = ctx();
        const names = draft.members.map(a => c.characters.find(ch => ch.avatar === a)?.name).filter(Boolean).join(', ');
        const chatName = (await scriptModule()).humanizedDateTime?.() || new Date().toISOString().replace(/[:.]/g, '-');
        const body = {...draft, name: draft.name.trim() || `Group: ${names}`, avatar_url: 'img/ai4.png', chat_id: chatName, chats: [chatName]};
        const response = await fetch('/api/groups/create', {method: 'POST', headers: c.getRequestHeaders(), body: JSON.stringify(body)});
        if (!response.ok) return toastr.error('Couldn’t create the group.');
        const data = await response.json();
        state.draft = null;
        await c.getCharacters();
        toastr.success(`Created ${body.name}`);
        H.showPage('groups', {group: data.id});
    };
    put(wrap, 
        el('div', {class: 'rvo-ctools'}, button('All groups', 'fa-arrow-left', () => { state.draft = null; H.showPage('groups', {group: ''}); })),
        section('gnew', 'New group', 'fa-users', [
            field('Name', textInput(draft.name, v => set('name', v), {placeholder: 'Leave empty to name it after its members'}), '', true),
            ...groupOptions(draft, set),
        ]),
        section('gmembers', `Members (${draft.members.length})`, 'fa-user-group', [
            draft.members.length ? membersList(draft, {onChange: () => refreshSoon('groups', 0)}) : note('No one yet: add characters below.', 'rvo-faint'),
            memberPicker(draft.members, avatar => { draft.members.push(avatar); refreshSoon('groups', 0); }),
        ], {grid: false}),
        el('div', {class: 'rvo-sbtns'}, button('Create group', 'fa-check', create, 'Create the group', 'primary')));
}

async function renderOneGroup(wrap, id) {
    const c = ctx();
    const gm = await groupsModule();
    const group = gm.groups.find(g => g.id === id) || c.groups.find(g => g.id === id);
    const set = (key, value) => { group[key] = value; saveGroupSoon(group); };
    const changed = () => { saveGroup(group, {now: true}); refreshSoon('groups', 0); };
    const chats = [...(group.chats || [])].reverse();
    const thumbs = groupThumbs(group);
    put(wrap, 
        el('div', {class: 'rvo-ctools'}, button('All groups', 'fa-arrow-left', () => H.showPage('groups', {group: ''}))),
        el('section', {class: 'rvo-phero'},
            el('span', {class: `rvo-gpics big n${Math.min(4, thumbs.length)}`}, ...thumbs.map(ch => el('img', {src: c.getThumbnailUrl('avatar', ch.avatar), alt: ''}))),
            el('div', {class: 'rvo-phero-text'},
                el('input', {class: 'rvo-cinput rvo-pname-input', value: group.name, 'aria-label': 'Group name', onchange: event => {
                    const value = event.target.value.trim(); if (!value) { event.target.value = group.name; return; } group.name = value; saveGroup(group, {now: true}); H.renderHeader();
                }}),
                el('div', {class: 'rvo-sbtns'},
                    button(c.groupId === id ? 'Back to the chat' : 'Chat', 'fa-comments', () => openGroup(id), 'Open the group chat', 'primary'),
                    button('New chat', 'fa-comment-medical', async () => { H.hidePage(); await gm.createNewGroupChat(id); }, 'Start a new chat with this group'),
                    button('Delete group', 'fa-trash-can', () => deleteGroup(id), 'Delete the group and its chats', 'danger')))),
        section('gmembers', `Members (${group.members?.length || 0})`, 'fa-user-group', [
            group.members?.length ? membersList(group, {onChange: changed}) : note('No members.', 'rvo-faint'),
            el('details', {class: 'rvo-gadd'}, el('summary', {}, icon('fa-user-plus'), el('span', {text: 'Add characters'})),
                memberPicker(group.members || [], avatar => { (group.members ||= []).push(avatar); changed(); })),
        ], {grid: false}),
        section('gopts', 'Group settings', 'fa-sliders', groupOptions(group, set)),
        section('gchats', `Chats (${chats.length})`, 'fa-comments', [
            el('div', {class: 'rvo-gchats'}, ...chats.map(chatId => el('button', {class: `rvo-gchat${group.chat_id === chatId ? ' on' : ''}`, type: 'button', onclick: () => openGroup(id, chatId)},
                icon('fa-message'), el('span', {text: globalThis.RocsyAddons?.titles?.get(`g:${id}`, chatId)?.title || chatId}), group.chat_id === chatId ? el('small', {text: 'latest'}) : null))),
        ], {grid: false}),
    );
}

async function deleteGroup(id) {
    const c = ctx();
    const gm = await groupsModule();
    const group = gm.groups.find(g => g.id === id);
    if (!group || !await confirmPopup(`Delete ${group.name}?`, 'The group and all of its chats are deleted. The characters stay.')) return;
    if (c.groupId === id) await gm.deleteGroup(id);
    else {
        const response = await fetch('/api/groups/delete', {method: 'POST', headers: c.getRequestHeaders(), body: JSON.stringify({id})});
        if (!response.ok) return toastr.error('Couldn’t delete the group.');
        for (const chatId of group.chats || []) await c.eventSource.emit(c.eventTypes.GROUP_CHAT_DELETED, chatId);
        await c.getCharacters();
    }
    toastr.success(`Deleted ${group.name}`);
    H.showPage('groups', {group: ''});
}

// ------------------------------------------------------------- quick replies --
const QR_AUTO = [['executeOnStartup', 'When SillyTavern starts'], ['executeOnUser', 'After you send'], ['executeOnAi', 'After a reply'],
    ['executeOnChatChange', 'When a chat opens'], ['executeOnNewChat', 'On a new chat'], ['executeOnGroupMemberDraft', 'Before a group member replies'], ['executeBeforeGeneration', 'Before each generation']];

async function renderQuickReplies(wrap) {
    const api = globalThis.quickReplyApi;
    if (!api) { put(wrap, empty('fa-bolt', 'Quick Replies isn’t loaded', 'Turn on the Quick Replies extension in SillyTavern.')); return; }
    const settings = api.settings;
    const sets = api.listSets();
    if (!sets.includes(state.qrSet)) state.qrSet = sets[0] || '';
    const linkRows = (config, scope) => (config?.setList || []).map(link => el('div', {class: 'rvo-qrlink'},
        H.switchControl(el('input', {type: 'checkbox', checked: link.isVisible || undefined, onchange: event => { link.isVisible = event.target.checked; link.update(); }}), '', 'Show its buttons'),
        el('button', {class: 'rvo-pname', type: 'button', onclick: () => H.showPage('quickreplies', {set: link.set.name})}, el('strong', {text: link.set.name}), el('small', {text: `${link.set.qrList.length} buttons`})),
        el('button', {class: 'rvo-icon-button rvo-ldelete', type: 'button', title: `Remove from ${scope}`, onclick: () => { config.removeSet(link.set); refreshSoon('quickreplies', 50); }}, icon('fa-xmark'))));
    const addTo = (config, scope, ...more) => {
        const options = sets.filter(name => !config?.setList?.some(link => link.set.name === name));
        if (!config || !options.length) return null;
        return choose([['', `Add a set to ${scope}…`], ...options.map(n => [n, n])], '', name => {
            if (!name) return; config.addSet(api.getSetByName(name), true); refreshSoon('quickreplies', 50);
        }, ...more);
    };
    const chatConfig = settings.chatConfig;
    const charConfig = settings.charConfig;
    put(wrap, 
        el('div', {class: 'rvo-sstatus' + (settings.isEnabled ? ' ok' : '')}, icon('fa-bolt'),
            el('div', {}, el('strong', {text: settings.isEnabled ? 'Quick Replies are on' : 'Quick Replies are off'}), el('small', {text: 'Buttons that send a message or run a command, shown above the chat box.'})),
            toggle(settings.isEnabled, on => { const box = $q('#qr--isEnabled'); if (box) { box.checked = on; fire(box, ['input', 'change']); } else { settings.isEnabled = on; settings.save(); } refreshSoon('quickreplies', 100); }, 'On')),
        section('qrwhere', 'Shown sets', 'fa-eye', [
            el('h4', {class: 'rvo-sh4', text: 'Every chat'}), ...linkRows(settings.config, 'every chat'), addTo(settings.config, 'every chat'),
            charConfig ? el('h4', {class: 'rvo-sh4', text: 'This character'}) : null, ...(charConfig ? linkRows(charConfig, 'this character') : []), charConfig ? addTo(charConfig, 'this character') : null,
            chatConfig ? el('h4', {class: 'rvo-sh4', text: 'This chat'}) : null, ...(chatConfig ? linkRows(chatConfig, 'this chat') : []), chatConfig ? addTo(chatConfig, 'this chat') : null,
            el('div', {class: 'rvo-lset-checks'},
                mirror('#qr--isCombined', 'Combine the buttons of all sets', '', {always: true}),
                mirror('#qr--showPopoutButton', 'Show the pop-out button', '', {always: true})),
        ], {grid: false}),
        await qrSetEditor(api, sets));
}

async function qrSetEditor(api, sets) {
    const set = api.getSetByName(state.qrSet);
    const picker = el('div', {class: 'rvo-srow'},
        sets.length ? choose(sets.map(n => [n, n]), state.qrSet, name => H.showPage('quickreplies', {set: name})) : null,
        button('New set', 'fa-plus', async () => {
            const name = await askText('Name of the new set');
            if (!name) return;
            if (api.getSetByName(name)) return toastr.warning('A set with that name exists.');
            await api.createSet(name);
            H.showPage('quickreplies', {set: name});
        }),
        set ? button('Delete set', 'fa-trash-can', async () => {
            if (!await confirmPopup(`Delete the set “${set.name}”?`, 'All of its buttons are deleted.')) return;
            await api.deleteSet(set.name); state.qrSet = ''; refreshSoon('quickreplies', 50);
        }, 'Delete this set', 'danger') : null);
    if (!set) return section('qrsets', 'Sets', 'fa-layer-group', [picker, note('No sets yet.')], {grid: false});
    const opts = () => ({disableSend: set.disableSend, placeBeforeInput: set.placeBeforeInput, injectInput: set.injectInput});
    const setOpt = (key, value) => { const o = opts(); o[key] = value; api.updateSet(set.name, o); };
    const update = debounce((qr, patch) => { try { api.updateQuickReply(set.name, qr.id, patch); } catch (error) { toastr.error(error.message); } }, 400);
    const rows = set.qrList.map(qr => {
        const open = state.open.has(`qr:${set.name}:${qr.id}`);
        const card = el('article', {class: `rvo-prompt rvo-qr${open ? ' open' : ''}`},
            el('div', {class: 'rvo-phead'},
                qr.icon ? el('span', {class: 'rvo-pfixed'}, icon(qr.icon)) : el('span', {class: 'rvo-pfixed'}, icon('fa-bolt')),
                el('button', {class: 'rvo-pname', type: 'button', onclick: () => { const k = `qr:${set.name}:${qr.id}`; open ? state.open.delete(k) : state.open.add(k); refreshSoon('quickreplies', 0); }},
                    el('strong', {text: qr.label || '(no label)'}), el('small', {text: String(qr.message || '').split('\n')[0].slice(0, 80)})),
                el('button', {class: 'rvo-icon-button', type: 'button', title: 'Run it now', onclick: () => api.executeQuickReply(set.name, qr.label).catch(error => toastr.error(error.message))}, icon('fa-play')),
                el('button', {class: 'rvo-icon-button rvo-ldelete', type: 'button', title: 'Delete', onclick: async () => {
                    if (!await confirmPopup(`Delete “${qr.label}”?`, '')) return; api.deleteQuickReply(set.name, qr.id); refreshSoon('quickreplies', 50);
                }}, icon('fa-trash-can'))));
        if (open) card.append(el('div', {class: 'rvo-lset-grid'},
            field('Label', textInput(qr.label, v => update(qr, {newLabel: v}))),
            field('Icon', textInput(qr.icon, v => update(qr, {icon: v.trim()}), {placeholder: 'fa-star'}), 'A Font Awesome name, optional.'),
            field('Tooltip', textInput(qr.title, v => update(qr, {title: v}))),
            field('Message or command', area(qr.message, v => update(qr, {message: v}), {rows: 6, spellcheck: 'false'}), 'Text to send, or slash commands like /echo hello.', true),
            el('div', {class: 'rvo-lset-checks'},
                check('Hidden', !!qr.isHidden, v => update(qr, {isHidden: v})),
                check('Show the label with the icon', !!qr.showLabel, v => update(qr, {showLabel: v})),
                ...QR_AUTO.map(([key, label]) => check(label, !!qr[key], v => update(qr, {[key]: v})))),
            field('Automation ID', textInput(qr.automationId, v => update(qr, {automationId: v.trim()})), 'Runs when a lorebook entry with this ID fires.')));
        return card;
    });
    return section('qrsets', `Set: ${set.name}`, 'fa-layer-group', [
        picker,
        el('div', {class: 'rvo-lset-checks'},
            check('Don’t send, put the text in the chat box', !!set.disableSend, v => setOpt('disableSend', v)),
            check('Place before the box text', !!set.placeBeforeInput, v => setOpt('placeBeforeInput', v)),
            check('Insert at the cursor', !!set.injectInput, v => setOpt('injectInput', v))),
        el('div', {class: 'rvo-plist'}, ...rows),
        el('div', {class: 'rvo-sbtns'}, button('Add a button', 'fa-plus', async () => {
            const label = await askText('Label of the new button');
            if (!label) return;
            const qr = api.createQuickReply(set.name, label, {message: ''});
            state.open.add(`qr:${set.name}:${qr.id}`);
            refreshSoon('quickreplies', 50);
        })),
    ], {grid: false, sub: `${set.qrList.length} buttons`});
}

// -------------------------------------------------------------------- regex --
const PLACEMENTS = [[1, 'Your messages'], [2, 'AI replies'], [3, 'Slash commands'], [5, 'World Info'], [6, 'Reasoning']];
const SUBSTITUTE = [[0, 'Don’t substitute'], [1, 'Raw'], [2, 'Escaped']];

async function renderRegex(wrap) {
    let rx;
    try { rx = await regexModule(); } catch (error) { put(wrap, empty('fa-code', 'Regex isn’t loaded', error.message)); return; }
    const c = ctx();
    const T = rx.SCRIPT_TYPES;
    const character = !c.groupId && c.characterId !== undefined ? c.characters[c.characterId] : null;
    const types = [[T.GLOBAL, 'Global', 'Every chat'], [T.SCOPED, character ? `${character.name}` : 'Character', 'Only this character’s chats'], [T.PRESET, 'Preset', 'Only with the current preset']];
    if (!types.some(([t]) => t === state.regexType)) state.regexType = T.GLOBAL;
    const type = state.regexType;
    const scripts = rx.getScriptsByType(type);
    const save = debounce(async () => { await rx.saveScriptsByType(scripts, type); dirty.hidden = false; }, 600);
    const dirty = el('div', {class: 'rvo-swarn', hidden: true}, icon('fa-rotate'), el('span', {text: 'Saved. Messages already on screen show the old text until the chat is reloaded.'}),
        button('Reload chat', 'fa-rotate', () => { c.reloadCurrentChat(); dirty.hidden = true; }));
    const tabs = el('div', {class: 'rvo-stabs'}, ...types.map(([t, label, hint]) => el('button', {class: `rvo-stab${t === type ? ' on' : ''}`, type: 'button', title: hint,
        disabled: (t === T.SCOPED && !character) || undefined, onclick: () => H.showPage('regex', {type: t})}, el('span', {text: label}), el('small', {text: `${rx.getScriptsByType(t).length}`}))));
    let allow = null;
    if (type === T.SCOPED && character) allow = toggle(rx.isScopedScriptsAllowed(character), on => { on ? rx.allowScopedScripts(character) : rx.disallowScopedScripts(character); }, 'Allowed to run', 'Character scripts only run once you allow them');
    if (type === T.PRESET) {
        const apiId = rx.getCurrentPresetAPI(); const preset = rx.getCurrentPresetName();
        allow = toggle(rx.isPresetScriptsAllowed(apiId, preset), on => { on ? rx.allowPresetScripts(apiId, preset) : rx.disallowPresetScripts(apiId, preset); }, 'Allowed to run', 'Preset scripts only run once you allow them');
    }
    const rows = scripts.map((script, index) => regexCard(rx, scripts, script, index, save));
    put(wrap, 
        note('Regex scripts find text and replace it: in what you send, in replies, or only in what the model sees. Higher scripts run first.'),
        tabs,
        allow ? el('div', {class: 'rvo-srow'}, allow) : null,
        dirty,
        el('div', {class: 'rvo-plist'}, ...rows),
        scripts.length ? null : note(type === T.SCOPED && !character ? 'Open a character’s chat to see its scripts.' : 'No scripts here yet.', 'rvo-faint'),
        (type !== T.SCOPED || character) ? el('div', {class: 'rvo-sbtns'}, button('Add a script', 'fa-plus', () => {
            const script = {id: uid(), scriptName: 'New script', findRegex: '', replaceString: '', trimStrings: [], placement: [2], disabled: false, markdownOnly: false, promptOnly: false, runOnEdit: true, substituteRegex: 0, minDepth: null, maxDepth: null};
            scripts.push(script); state.open.add(`rx:${script.id}`);
            rx.saveScriptsByType(scripts, type).then(() => refreshSoon('regex', 0));
        })) : null,
        section('rxtest', 'Try it', 'fa-flask', [regexTester(rx, scripts)], {grid: false, open: false, sub: 'see what the scripts here do to a text'}));
}

function regexCard(rx, scripts, script, index, save) {
    const key = `rx:${script.id}`;
    const open = state.open.has(key);
    const set = (name, value) => { script[name] = value; save(); };
    const where = PLACEMENTS.filter(([p]) => script.placement?.includes(p)).map(([, l]) => l).join(', ') || 'nowhere';
    const scope = script.promptOnly ? 'prompt only' : script.markdownOnly ? 'display only' : '';
    const card = el('article', {class: `rvo-prompt${script.disabled ? '' : ' on'}${open ? ' open' : ''}`},
        el('div', {class: 'rvo-phead'},
            toggle(!script.disabled, on => { script.disabled = !on; save(); card.classList.toggle('on', on); }, '', 'On / off'),
            el('button', {class: 'rvo-pname', type: 'button', onclick: () => { open ? state.open.delete(key) : state.open.add(key); refreshSoon('regex', 0); }},
                el('strong', {text: script.scriptName || '(no name)'}), el('small', {text: [where, scope].filter(Boolean).join(' · ')})),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Move up', disabled: index === 0 || undefined, onclick: () => { scripts.splice(index - 1, 0, scripts.splice(index, 1)[0]); save(); refreshSoon('regex', 0); }}, icon('fa-arrow-up')),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Move down', disabled: index === scripts.length - 1 || undefined, onclick: () => { scripts.splice(index + 1, 0, scripts.splice(index, 1)[0]); save(); refreshSoon('regex', 0); }}, icon('fa-arrow-down')),
            el('button', {class: 'rvo-icon-button rvo-ldelete', type: 'button', title: 'Delete', onclick: async () => {
                if (!await confirmPopup(`Delete “${script.scriptName}”?`, '')) return; scripts.splice(index, 1); save(); refreshSoon('regex', 0);
            }}, icon('fa-trash-can'))));
    if (!open) return card;
    const findError = el('small', {class: 'rvo-serror'});
    const checkFind = value => {
        findError.textContent = '';
        const m = /^\/([\s\S]*)\/([a-z]*)$/.exec(value);
        if (!m) return;
        try { new RegExp(m[1], m[2]); } catch (error) { findError.textContent = error.message; }
    };
    const find = textInput(script.findRegex, v => { checkFind(v); set('findRegex', v); }, {spellcheck: 'false', placeholder: '/pattern/flags'});
    checkFind(script.findRegex);
    card.append(el('div', {class: 'rvo-lset-grid'},
        field('Name', textInput(script.scriptName, v => set('scriptName', v.trim() || 'Script'))),
        el('label', {class: 'rvo-lfield wide'}, el('span', {text: 'Find'}), find, findError, el('small', {text: 'A /regex/ with flags (g, i, s …) or plain text.'})),
        field('Replace with', area(script.replaceString, v => set('replaceString', v), {rows: 3, spellcheck: 'false'}), '$1, $2 … for groups; {{match}} for the whole match.', true),
        field('Trim out', area((script.trimStrings || []).join('\n'), v => set('trimStrings', v.split('\n').filter(Boolean)), {rows: 2}), 'One per line: removed from the match before replacing.', true),
        el('div', {class: 'rvo-lfield wide'}, el('span', {text: 'Affects'}), el('div', {class: 'rvo-lset-checks'}, ...PLACEMENTS.map(([p, label]) => check(label, script.placement?.includes(p), on => {
            const list = new Set(script.placement || []); on ? list.add(p) : list.delete(p); set('placement', [...list].sort());
        })))),
        field('Changes', choose([['both', 'What you see and what the model gets'], ['display', 'Only what you see'], ['prompt', 'Only what the model gets']],
            script.markdownOnly ? 'display' : script.promptOnly ? 'prompt' : 'both', v => { script.markdownOnly = v === 'display'; script.promptOnly = v === 'prompt'; save(); })),
        field('Macros in Find', choose(SUBSTITUTE, script.substituteRegex ?? 0, v => set('substituteRegex', Number(v)))),
        field('Min depth', numberInput(script.minDepth ?? '', v => set('minDepth', v === '' ? null : Number(v)), {min: -1, placeholder: 'any'}), '0 = the last message.'),
        field('Max depth', numberInput(script.maxDepth ?? '', v => set('maxDepth', v === '' ? null : Number(v)), {min: 0, placeholder: 'any'})),
        el('div', {class: 'rvo-lset-checks'}, check('Run on edit', !!script.runOnEdit, v => set('runOnEdit', v), 'Also when you edit a message.'))));
    return card;
}

function regexTester(rx, scripts) {
    const output = el('pre', {class: 'rvo-stest-out'});
    const input = area('', () => run(), {rows: 4, placeholder: 'Type or paste a message here'});
    const run = () => {
        let text = input.value;
        for (const script of scripts) if (!script.disabled) { try { text = rx.runRegexScript(script, text); } catch { /* shown in the editor */ } }
        output.textContent = text;
    };
    return el('div', {class: 'rvo-stest'}, input, el('small', {class: 'rvo-faint', text: 'Result (all scripts on this tab, in order):'}), output);
}

// ------------------------------------------------------- memory and extras --
async function renderMemory(wrap) {
    const summarize = extensionPanel('#memory_settings') || extensionPanel('#summary_source');
    const vectors = extensionPanel('#vectors_source');
    put(wrap, note('Summarize keeps a running summary of the chat in the prompt. Vector Storage finds older messages and files that match what’s being said and adds them back.'),
        summarize && $q('#summary_source')?.value === 'extras' ? el('div', {class: 'rvo-swarn'}, icon('fa-circle-info'),
            el('span', {text: 'Summarize uses the old Extras server, which Rocsy doesn’t run. Pick “Main API” to summarize with your own model.'}),
            button('Use Main API', 'fa-check', () => { $('#summary_source').val('main').trigger('change'); refreshSoon('memory', 200); }, 'Summarize with the model you chat with', 'primary')) : null,
        summarize ? section('sum', 'Summarize', 'fa-file-lines', mirrorPanel(summarize, {skip: ['[id$="_value"]']}), {sub: 'chat summary'}) : note('The Summarize extension isn’t loaded.'),
        vectors ? section('vec', 'Vector Storage', 'fa-diagram-project', mirrorPanel(vectors), {sub: 'retrieval', open: false}) : note('The Vector Storage extension isn’t loaded.'));
}

/** Rocsy's own phone display of the expression sprite (SillyTavern hides its sprite on narrow screens). */
function phoneExpressionSection() {
    const m = H.mobileExpressionSettings;
    const dimValue = el('output', {text: `${m.dim()}%`});
    const dim = el('input', {type: 'range', min: '10', max: '100', step: '5', value: String(m.dim()), class: 'rvo-srange',
        oninput: event => { dimValue.textContent = `${event.target.value}%`; m.setDim(event.target.value); }});
    const dimField = field('Background strength', el('div', {class: 'rvo-srow'}, dim, dimValue), 'How visible the picture is behind the messages.');
    dimField.hidden = m.mode() !== 'background';
    return section('phoneexpr', 'On phones', 'fa-mobile-screen', [
        field('Show the expression as', choose([['corner', 'A portrait in the corner (tap to enlarge)'], ['background', 'The chat background'], ['off', 'Nothing']], m.mode(),
            value => { m.setMode(value); dimField.hidden = value !== 'background'; }), 'SillyTavern hides its expression picture on narrow screens; this is Rocsy’s own.'),
        dimField]);
}

async function renderExtras(wrap) {
    const expressions = extensionPanel('#expression_translate') || extensionPanel('#expressions_allow_multiple');
    const images = extensionPanel('#sd_refine_mode') || extensionPanel('#sd_source');
    put(wrap, note('Expressions shows a character picture that matches the mood of each reply. Image generation makes pictures from the chat (/sd).'),
        globalThis.RocsyImage?.classifierPicker ? section('classifier', 'Classifier', 'fa-tags', [globalThis.RocsyImage.classifierPicker()], {grid: false}) : null,
        H.mobileExpressionSettings ? phoneExpressionSection() : null,
        expressions ? section('expr', 'Character expressions', 'fa-face-smile-beam', mirrorPanel(expressions), {open: false}) : note('The Expressions extension isn’t loaded.'),
        images ? section('sd', 'Image generation', 'fa-image', mirrorPanel(images), {open: false}) : note('The Image Generation extension isn’t loaded.'));
}


// --------------------------------------------------------- prompt formatting --
function presetButtons(kind) {
    const b = (attr, label, faIcon, after = 500) => mirrorButton(`[data-preset-manager-${attr}="${kind}"]`, label, faIcon, {after});
    return el('div', {class: 'rvo-lfield wide rvo-sbtns'},
        b('update', 'Save', 'fa-floppy-disk'), b('new', 'Save as new', 'fa-plus'), b('rename', 'Rename', 'fa-pen'),
        b('import', 'Import', 'fa-file-import'), b('export', 'Export', 'fa-file-export', 0), b('restore', 'Restore default', 'fa-rotate-left'),
        b('delete', 'Delete', 'fa-trash-can'));
}

async function renderFormatting(wrap) {
    const c = ctx();
    const chatCompletion = ($q('#main_api')?.value || c.mainApi) === 'openai';
    const m = (sel, label, hint = '', opts = {}) => mirror(sel, label, hint, {always: true, ...opts});
    put(wrap,
        chatCompletion ? el('div', {class: 'rvo-swarn'}, icon('fa-circle-info'),
            el('span', {text: 'You use Chat Completion: the model’s own chat template formats the messages, so the Context and Instruct templates below aren’t used. The system prompt, reasoning and stop strings still are (the prompt list is under Response settings).'}),
            button('Response settings', 'fa-sliders', () => H.showPage('response'))) : null,
        section('fmt-sys', 'System prompt', 'fa-terminal', [
            m('#sysprompt_enabled', 'Use a system prompt', ''),
            m('#sysprompt_select', 'Preset', '', {wide: true, redraw: true}),
            presetButtons('sysprompt'),
            m('#sysprompt_content', 'Prompt', '{{char}}, {{user}} and other macros work here.', {rows: 8}),
            m('#sysprompt_post_history', 'Post-history instructions', 'Added after the chat history.', {rows: 3}),
        ]),
        section('fmt-reason', 'Reasoning', 'fa-brain', [
            m('#reasoning_auto_parse', 'Auto-parse', 'Take the thinking out of the reply and show it on its own.'),
            m('#reasoning_auto_expand', 'Auto-expand', 'Show the thinking opened.'),
            m('#reasoning_show_hidden', 'Show hidden', 'Show that the model thought even when the thinking is hidden.'),
            m('#reasoning_add_to_prompts', 'Add to prompts', 'Send earlier thinking back to the model.'),
            m('#reasoning_max_additions', 'Max additions', 'How many earlier thinking blocks are sent.'),
            m('#reasoning_select', 'Template', '', {wide: true, redraw: true}),
            presetButtons('reasoning'),
            m('#reasoning_prefix', 'Prefix', '', {rows: 2}), m('#reasoning_suffix', 'Suffix', '', {rows: 2}), m('#reasoning_separator', 'Separator', '', {rows: 2}),
        ]),
        section('fmt-misc', 'Stop strings, tokenizer and reply start', 'fa-gears', [
            m('#custom_stopping_strings', 'Custom stop strings', 'A JSON list, e.g. ["\\nUser:", "</s>"]. Generation stops at any of them.', {rows: 2}),
            m('#custom_stopping_strings_macro', 'Replace macros in stop strings', ''),
            m('#tokenizer', 'Tokenizer', 'Used for counting tokens.'),
            m('#token_padding', 'Token padding', 'Safety margin when the context is nearly full.'),
            m('#start_reply_with', 'Start reply with', 'The model continues from this text.', {rows: 2}),
            m('#chat-show-reply-prefix-checkbox', 'Show the reply start in the chat', ''),
            m('#bind_model_templates', 'Pick templates from the model', 'Choose the context/instruct template that matches the model.'),
            m('#markdown_escape_strings', 'Non-markdown strings', 'Comma separated; not formatted as markdown.'),
        ]),
        section('fmt-ctx', 'Context template', 'fa-file-lines', [
            m('#context_presets', 'Template', '', {wide: true, redraw: true}),
            presetButtons('context'),
            m('#context_derived', 'Take it from the model’s metadata', ''),
            m('#context_story_string', 'Story string', 'How the card, persona and scenario are put together.', {rows: 8}),
            m('#context_story_string_position', 'Position', '', {redraw: true}),
            m('#context_story_string_depth', 'Depth', ''), m('#context_story_string_role', 'Role', ''),
            m('#context_example_separator', 'Example separator', '', {rows: 2}), m('#context_chat_start', 'Chat start', '', {rows: 2}),
            el('div', {class: 'rvo-lset-checks'},
                m('#always-force-name2-checkbox', 'Always add the character’s name'), m('#single_line', 'One line per reply'),
                m('#collapse-newlines-checkbox', 'Collapse consecutive newlines'), m('#trim_spaces', 'Trim spaces'),
                m('#trim_sentences_checkbox', 'Trim incomplete sentences'), m('#context_use_stop_strings', 'Separators as stop strings'),
                m('#context_names_as_stop_strings', 'Names as stop strings')),
        ], {open: !chatCompletion, sub: chatCompletion ? 'Text Completion only' : ''}),
        section('fmt-instruct', 'Instruct template', 'fa-list-check', [
            m('#instruct_enabled', 'Instruct mode', '', {redraw: true}),
            m('#instruct_presets', 'Template', '', {wide: true, redraw: true}),
            presetButtons('instruct'),
            m('#instruct_derived', 'Take it from the model’s metadata', ''),
            m('#instruct_bind_to_context', 'Bind to the context template', ''),
            m('#instruct_activation_regex', 'Activation regex', 'Picks this template when the model name matches.'),
            m('#instruct_names_behavior', 'Include names', ''),
            el('div', {class: 'rvo-lset-checks'},
                m('#instruct_wrap', 'Wrap sequences with newlines'), m('#instruct_macro', 'Replace macros in sequences'),
                m('#instruct_sequences_as_stop_strings', 'Sequences as stop strings'), m('#instruct_skip_examples', 'Skip example dialogues formatting'),
                m('#instruct_system_same_as_user', 'System messages like user messages')),
            ...[['story_string_prefix', 'Story string prefix'], ['story_string_suffix', 'Story string suffix'], ['input_sequence', 'User message prefix'], ['input_suffix', 'User message suffix'],
                ['output_sequence', 'Assistant message prefix'], ['output_suffix', 'Assistant message suffix'], ['system_sequence', 'System message prefix'], ['system_suffix', 'System message suffix'],
                ['first_output_sequence', 'First assistant prefix'], ['last_output_sequence', 'Last assistant prefix'], ['first_input_sequence', 'First user prefix'], ['last_input_sequence', 'Last user prefix'],
                ['last_system_sequence', 'System instruction prefix'], ['stop_sequence', 'Stop sequence'], ['user_alignment_message', 'User filler message']]
                .map(([id, label]) => m(`#instruct_${id}`, label, '', {rows: 2})),
        ], {open: !chatCompletion, sub: chatCompletion ? 'Text Completion only' : ''}),
        el('div', {class: 'rvo-sbtns'},
            mirrorButton('#af_master_import', 'Import all formatting', 'fa-file-import'),
            mirrorButton('#af_master_export', 'Export all formatting', 'fa-file-export', {after: 0})));
}

// --------------------------------------------------------------- extensions --
// Extensions with a Rocsy page of their own link there; the rest are mirrored (or shown as they are).
const OWN_PAGES = [[/^regex/i, 'regex', 'Regex'], [/quick ?repl/i, 'quickreplies', 'Quick Replies'], [/summari[sz]e/i, 'memory', 'Memory'],
    [/vector/i, 'memory', 'Memory'], [/expression/i, 'extras', 'Expressions & images'], [/image generation|stable.?diffusion/i, 'extras', 'Expressions & images'],
    [/connection profile/i, 'connection', 'Model & connection']];
const ROCSY_EXTENSIONS = ['rocsy-addons', 'rocsy-visual-overhaul'];
const ext = {q: '', reload: false, original: new Set()};

function extensionPanels() {
    const roots = ['#extensions_settings', '#extensions_settings2'].map(s => $q(s)).filter(Boolean);
    const drawers = roots.flatMap(root => [...root.querySelectorAll('.inline-drawer')].filter(d => !d.parentElement.closest('.inline-drawer')));
    return drawers.map(drawer => {
        const header = drawer.querySelector(':scope > .inline-drawer-header, .inline-drawer-header');
        const title = (header?.querySelector('b')?.textContent || header?.textContent || '').replace(/\s+/g, ' ').trim();
        return {drawer, title, content: drawer.querySelector(':scope > .inline-drawer-content') || drawer.querySelector('.inline-drawer-content')};
    }).filter(p => p.title && p.content);
}

const EXT_CATEGORIES = [
    ['rocsy', 'Rocsy', 'fa-feather', /^rocsy/i],
    ['chat', 'Chat & writing', 'fa-pen-nib', /translat|quick ?repl|regex|objective|randomiz|dice|hypebot|variable|token|author|cfg|slash|swipe|bookmark|stepped/i],
    ['voice', 'Voice & audio', 'fa-microphone-lines', /tts|speech|voice|rvc|audio|music|whisper/i],
    ['images', 'Images & expressions', 'fa-image', /image|expression|gallery|caption|background|stable|sd\b|sprite|visual/i],
    ['memory', 'Memory & knowledge', 'fa-brain', /vector|summar|data ?bank|attach|memory|chroma|world/i],
    ['connect', 'Connections & tools', 'fa-plug', /connection|asset|tool|function|api|web ?search|download/i],
    ['other', 'Other', 'fa-puzzle-piece', /./],
];
const EXT_ICONS = [[/translat/i, 'fa-language'], [/quick ?repl/i, 'fa-bolt'], [/regex/i, 'fa-code'], [/tts|speech|voice/i, 'fa-microphone-lines'],
    [/expression/i, 'fa-face-smile-beam'], [/image gen|stable/i, 'fa-wand-magic-sparkles'], [/caption/i, 'fa-closed-captioning'], [/gallery/i, 'fa-images'],
    [/vector/i, 'fa-diagram-project'], [/summar/i, 'fa-file-lines'], [/data ?bank|attach/i, 'fa-paperclip'], [/connection/i, 'fa-plug'],
    [/asset|download/i, 'fa-cloud-arrow-down'], [/rocsy/i, 'fa-feather'], [/token/i, 'fa-coins'], [/dice|random/i, 'fa-dice']];
const extIcon = title => EXT_ICONS.find(([re]) => re.test(title))?.[1] || 'fa-puzzle-piece';
const extCategory = title => EXT_CATEGORIES.find(([, , , re]) => re.test(title));
const normName = text => String(text || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const PANEL_ALIASES = {downloadextensionsassets: 'assets', imageprompttemplates: 'imagegeneration', summarize: 'summarize', vectorstorage: 'vectorstorage'};

/** Which extension a settings panel belongs to (by its title). */
function panelOwner(panel, list) {
    const t = PANEL_ALIASES[normName(panel.title)] || normName(panel.title);
    return list.find(x => normName(x.title) === t || normName(x.short) === t)
        || list.find(x => { const n = normName(x.title); return n.length > 3 && (t.includes(n) || n.includes(t)); }) || null;
}

function extensionSettingsBody(p) {
    const own = OWN_PAGES.find(([re]) => re.test(p.title));
    if (own) return el('div', {class: 'rvo-srow rvo-extgo'}, el('span', {class: 'rvo-faint', text: `${p.title} has a page of its own.`}),
        button(`Open ${own[2]}`, 'fa-arrow-right', () => H.showPage(own[1]), `Open ${own[2]}`, 'primary'));
    if (/^rocsy addons$/i.test(p.title)) return el('div', {class: 'rvo-srow rvo-extgo'}, el('span', {class: 'rvo-faint', text: 'Voice, connection, chat names and the Library are in Rocsy Addons.'}),
        button('Open Rocsy Addons', 'fa-arrow-right', () => globalThis.RocsyAddons?.open(), 'Open Rocsy Addons', 'primary'));
    const body = el('div', {class: 'rvo-extbody'});
    const mirrored = ext.original.has(p.title) ? [] : mirrorPanel(p.content);
    const original = ext.original.has(p.title) || !mirrored.length;
    body.append(el('div', {class: 'rvo-srow rvo-extmode'},
        el('small', {class: 'rvo-faint', text: p.title}),
        toggle(original, on => { on ? ext.original.add(p.title) : ext.original.delete(p.title); refreshSoon('extensions', 0); }, 'Original controls',
            'Show the extension’s own controls (for settings that don’t come through here)')));
    if (original) { const host = el('div', {class: 'rvo-adopt'}); body.append(host); adopt(p.content, host); }
    else body.append(el('div', {class: 'rvo-lset-grid rvo-extgrid'}, ...mirrored));
    return body;
}

async function renderExtensions(wrap) {
    const em = await extensionsModule();
    const c = ctx();
    const disabled = new Set(c.extensionSettings.disabledExtensions || []);
    const names = [...(em.extensionNames || [])];
    const info = name => {
        const manifest = em.getExtensionManifest(name) || {};
        const short = name.replace(/^third-party\//, '');
        const title = manifest.display_name || short;
        return {name, short, title, version: manifest.version || '', author: manifest.author || '', homePage: manifest.homePage || '',
            thirdParty: name.startsWith('third-party'), rocsy: ROCSY_EXTENSIONS.includes(short), enabled: !disabled.has(name), panels: []};
    };
    const all = names.map(info);
    const panels = extensionPanels();
    const loose = [];
    for (const p of panels) { const owner = panelOwner(p, all); if (owner) owner.panels.push(p); else loose.push(p); }
    ext.filter ||= 'all';
    const FILTERS = [['all', 'All', () => true], ['on', 'On', x => x.enabled], ['off', 'Off', x => !x.enabled],
        ['settings', 'With settings', x => x.panels.length > 0], ['mine', 'Installed by you', x => x.thirdParty && !x.rocsy], ['builtin', 'Built-in', x => !x.thirdParty]];
    const q = ext.q.toLowerCase();
    const test = FILTERS.find(f => f[0] === ext.filter)?.[2] || (() => true);
    const shown = all.filter(x => test(x) && (!q || `${x.title} ${x.short} ${x.author} ${x.panels.map(p => p.title).join(' ')}`.toLowerCase().includes(q)));
    const card = x => {
        const key = `extc:${x.name}`;
        const open = state.open.has(key) || (!!q && x.panels.length > 0 && shown.length <= 3);
        const cat = extCategory(x.title);
        const body = el('div', {class: 'rvo-extcard-body'});
        const node = el('article', {class: `rvo-extcard${x.enabled ? ' on' : ''}${open ? ' open' : ''}`, style: `--rvo-hue: ${EXT_CATEGORIES.indexOf(cat) * 52 + 20}`},
            el('div', {class: 'rvo-extcard-head'},
                el('span', {class: 'rvo-exticon'}, icon(extIcon(x.title))),
                el('button', {class: 'rvo-pname', type: 'button', disabled: !x.panels.length || undefined, title: x.panels.length ? 'Settings' : 'No settings',
                    onclick: () => { open ? state.open.delete(key) : state.open.add(key); refreshSoon('extensions', 0); }},
                    el('strong', {text: x.title}),
                    el('small', {text: [x.rocsy ? 'Rocsy' : x.thirdParty ? 'Installed by you' : 'Built-in', x.version ? `v${x.version}` : '', x.author].filter(Boolean).join(' · ')})),
                x.panels.length ? el('button', {class: `rvo-cbtn rvo-extset${open ? ' on' : ''}`, type: 'button', onclick: () => { open ? state.open.delete(key) : state.open.add(key); refreshSoon('extensions', 0); }},
                    icon('fa-sliders'), el('span', {text: open ? 'Hide settings' : 'Settings'})) : null,
                x.thirdParty && !x.rocsy ? el('button', {class: 'rvo-icon-button', type: 'button', title: 'Update', onclick: () => updateExtension(x)}, icon('fa-download')) : null,
                x.thirdParty && !x.rocsy ? el('button', {class: 'rvo-icon-button rvo-ldelete', type: 'button', title: 'Delete', onclick: () => deleteExtension(em, x)}, icon('fa-trash-can')) : null,
                toggle(x.enabled, async on => {
                    if (on) await em.enableExtension(x.name, false); else await em.disableExtension(x.name, false);
                    ext.reload = true; refreshSoon('extensions', 50);
                }, '', x.enabled ? 'On (turning it off applies after a reload)' : 'Off (turning it on applies after a reload)')),
            body);
        if (open) for (const p of x.panels) body.append(extensionSettingsBody(p));
        if (open && !x.enabled) body.append(note('Turned off: its settings come back after it’s on and SillyTavern has reloaded.', 'rvo-faint'));
        return node;
    };
    const groups = EXT_CATEGORIES.map(([k, label, iconName]) => {
        const list = shown.filter(x => extCategory(x.title)?.[0] === k).sort((a, b) => Number(b.enabled) - Number(a.enabled) || a.title.localeCompare(b.title));
        if (!list.length) return null;
        return el('section', {class: 'rvo-lgroup'},
            el('h3', {class: 'rvo-lgroup-title'}, icon(iconName), el('span', {text: label}), el('small', {text: String(list.length)})),
            el('div', {class: 'rvo-extlist'}, ...list.map(card)));
    }).filter(Boolean);
    const looseShown = loose.filter(p => !q || p.title.toLowerCase().includes(q));
    put(wrap,
        H.pageBanner('fa-cubes', 'Extensions', 'Add-ons that give SillyTavern more features. Switch them on or off, change their settings, or install new ones from a Git link.', [
            ['Installed', all.length, 'fa-cubes'], ['On', all.filter(x => x.enabled).length, 'fa-toggle-on'],
            ['By you', all.filter(x => x.thirdParty && !x.rocsy).length, 'fa-user'], ['With settings', all.filter(x => x.panels.length).length, 'fa-sliders']]),
        ext.reload ? el('div', {class: 'rvo-swarn'}, icon('fa-rotate'), el('span', {text: 'Turning extensions on or off takes effect after SillyTavern reloads.'}),
            button('Reload now', 'fa-rotate', () => location.reload(), 'Reload SillyTavern', 'primary')) : null,
        el('div', {class: 'rvo-ctools'},
            el('input', {type: 'search', class: 'rvo-cinput', placeholder: 'Search extensions and their settings', value: ext.q, oninput: debounce(event => { ext.q = event.target.value; refreshSoon('extensions', 0); }, 250)}),
            button('Install extension', 'fa-cloud-arrow-down', () => em.openThirdPartyExtensionMenu?.(), 'Install an extension from a Git URL', 'primary')),
        H.filterChips(FILTERS.map(([k, label, fn]) => [k, label, all.filter(fn).length]), ext.filter, v => { ext.filter = v; refreshSoon('extensions', 0); }),
        ...groups,
        groups.length ? null : el('p', {class: 'rvo-empty', text: 'No extensions match.'}),
        looseShown.length ? el('section', {class: 'rvo-lgroup'},
            el('h3', {class: 'rvo-lgroup-title'}, icon('fa-sliders'), el('span', {text: 'More settings'}), el('small', {text: String(looseShown.length)})),
            ...looseShown.map(p => {
                const box = section(`ext:${p.title}`, p.title, extIcon(p.title), [], {grid: false, open: false});
                const fill = () => box.querySelector('.rvo-sbody').append(extensionSettingsBody(p));
                if (box.open) fill();
                box.addEventListener('toggle', () => { if (box.open && !box.querySelector('.rvo-sbody').childElementCount) fill(); });
                return box;
            })) : null);
}

async function updateExtension(x) {
    const c = ctx();
    try {
        const response = await fetch('/api/extensions/update', {method: 'POST', headers: c.getRequestHeaders(), body: JSON.stringify({extensionName: x.short, global: false})});
        if (!response.ok) throw new Error((await response.text()) || response.statusText);
        const data = await response.json();
        if (data.isUpToDate) toastr.success(`${x.title} is up to date`);
        else { toastr.success(`${x.title} updated to ${data.shortCommitHash}`); ext.reload = true; refreshSoon('extensions', 50); }
    } catch (error) { toastr.error(error.message, `Update ${x.title}`); }
}

async function deleteExtension(em, x) {
    if (!await confirmPopup(`Delete ${x.title}?`, 'The extension’s files are removed. SillyTavern reloads afterwards.')) return;
    await em.deleteExtension(`/${x.short}`);
    toastr.success(`Deleted ${x.title}`);
    setTimeout(() => location.reload(), 800);
}

Object.assign(renderers, {
    connection: renderConnection, response: renderResponse, persona: renderPersona, groups: renderGroups,
    quickreplies: renderQuickReplies, regex: renderRegex, memory: renderMemory, extras: renderExtras,
    formatting: renderFormatting, extensions: renderExtensions,
    backgrounds: renderBackgrounds, interface: renderInterface, authorsnote: renderAuthorsNote,
});

// ------------------------------------------------------------------- events --

// ------------------------------------------------------------- backgrounds --
// SillyTavern's backgrounds, in-site: pick one for every chat, or lock one to this chat; upload, rename, delete.
// Picking writes what SillyTavern's own picker writes (background_settings, or the chat's custom_background), so
// its panel and the /bg command stay in step.
const bgState = {q: ''};
const bgCss = (file, custom) => (custom ? `url("${encodeURI(file)}")` : `url("backgrounds/${encodeURIComponent(file)}")`);
const bgThumb = (file, custom) => (custom ? encodeURI(file) : `/thumbnail?type=bg&file=${encodeURIComponent(file)}`);

async function pickBackground(file, custom, {lock = false} = {}) {
    const bg = await backgroundsModule();
    const c = ctx();
    const url = bgCss(file, custom);
    const locked = !!c.chatMetadata?.custom_background;
    if (lock || custom || locked) {
        if (!c.getCurrentChatId?.()) return toastr.warning('Open a chat first to give it its own background.');
        c.chatMetadata.custom_background = url;
        c.saveMetadataDebounced?.() ?? c.saveMetadata?.();
        $('#bg1').css('background-image', url);
    } else {
        bg.background_settings.name = file;
        bg.background_settings.url = url;
        $('#bg1').css('background-image', url);
        saveSettings();
    }
    refreshSoon('backgrounds', 100);
}

async function unlockBackground() {
    const bg = await backgroundsModule();
    const c = ctx();
    delete c.chatMetadata.custom_background;
    c.saveMetadataDebounced?.() ?? c.saveMetadata?.();
    $('#bg1').css('background-image', bg.background_settings.url);
    refreshSoon('backgrounds', 100);
}

async function renameBackground(file) {
    const dot = file.lastIndexOf('.');
    const name = await askText('Rename background', dot > 0 ? file.slice(0, dot) : file);
    if (!name) return;
    const next = `${name.trim()}${dot > 0 ? file.slice(dot) : ''}`;
    if (next === file) return;
    const response = await fetch('/api/backgrounds/rename', {method: 'POST', headers: ctx().getRequestHeaders(), body: JSON.stringify({old_bg: file, new_bg: next})});
    if (!response.ok) return toastr.error('Couldn’t rename it (is the name taken?).');
    const bg = await backgroundsModule();
    if (bg.background_settings.name === file) { bg.background_settings.name = next; bg.background_settings.url = bgCss(next, false); saveSettings(); }
    await bg.getBackgrounds?.();
    refreshSoon('backgrounds', 50);
}

async function deleteBackground(file) {
    if (!await confirmPopup('Delete background', `Delete “${file}”? This can’t be undone.`)) return;
    const response = await fetch('/api/backgrounds/delete', {method: 'POST', headers: ctx().getRequestHeaders(), body: JSON.stringify({bg: file})});
    if (!response.ok) return toastr.error('Couldn’t delete it.');
    const bg = await backgroundsModule();
    await bg.getBackgrounds?.();
    refreshSoon('backgrounds', 50);
}

async function renderBackgrounds(wrap) {
    const c = ctx();
    const bg = await backgroundsModule();
    let images = [];
    try {
        const response = await fetch('/api/backgrounds/all', {method: 'POST', headers: c.getRequestHeaders(), body: '{}'});
        images = response.ok ? (await response.json()).images || [] : [];
    } catch { /* shown as empty */ }
    const current = bg.background_settings?.name || '';
    const locked = String(c.chatMetadata?.custom_background || '');
    const chatOnes = Array.isArray(c.chatMetadata?.chat_backgrounds) ? c.chatMetadata.chat_backgrounds : [];
    const hasChat = !!c.getCurrentChatId?.();
    const q = bgState.q.trim().toLowerCase();
    const tile = (file, custom) => {
        const url = bgCss(file, custom);
        const selected = locked ? locked === url : !custom && file === current;
        const label = (custom ? file.split('/').pop() : file).replace(/\.[^.]+$/, '');
        return el('div', {class: `rvo-bgtile${selected ? ' on' : ''}`},
            el('button', {class: 'rvo-bgpick', type: 'button', title: locked ? `Use for this chat: ${label}` : `Use for every chat: ${label}`, onclick: () => pickBackground(file, custom)},
                el('img', {src: bgThumb(file, custom), alt: '', loading: 'lazy'}), el('span', {text: label}),
                selected ? el('i', {class: 'fa-solid fa-circle-check fa-fw rvo-bgcheck', 'aria-hidden': 'true'}) : null),
            el('div', {class: 'rvo-bgacts'},
                hasChat && !custom ? el('button', {class: 'rvo-icon-button', type: 'button', title: 'Lock to this chat only', onclick: () => pickBackground(file, false, {lock: true})}, icon('fa-lock')) : null,
                custom ? null : el('button', {class: 'rvo-icon-button', type: 'button', title: 'Rename', onclick: () => renameBackground(file)}, icon('fa-pen')),
                custom ? null : el('button', {class: 'rvo-icon-button', type: 'button', title: 'Delete', onclick: () => deleteBackground(file)}, icon('fa-trash-can'))));
    };
    const shown = images.filter(img => !q || img.filename.toLowerCase().includes(q));
    const search = el('input', {type: 'search', class: 'rvo-cinput', placeholder: 'Search backgrounds', value: bgState.q,
        oninput: debounce(event => { bgState.q = event.target.value; refresh('backgrounds'); }, 250)});
    put(wrap,
        H.pageBanner('fa-panorama', 'Backgrounds', 'The picture behind the chat: one for every chat, or a different one locked to a chat.',
            [['Backgrounds', images.length, 'fa-image'], ['This chat', locked ? 'own' : 'shared', locked ? 'fa-lock' : 'fa-lock-open']]),
        H.layout && !H.layout.get('showBackground') ? el('div', {class: 'rvo-swarn'}, icon('fa-circle-info'), el('span', {text: 'The Rocsy layout is set to hide the chat background picture.'}),
            button('Show it', 'fa-eye', () => { H.layout.set('showBackground', true); refreshSoon('backgrounds', 50); }, 'Show the chat background picture', 'primary')) : null,
        locked ? el('div', {class: 'rvo-swarn'}, icon('fa-lock'), el('span', {text: 'This chat has its own background. Picking one now changes it for this chat only.'}),
            button('Use the shared one again', 'fa-lock-open', unlockBackground, 'Unlock: this chat uses the background every chat uses')) : null,
        section('bg-opts', 'Options', 'fa-sliders', [
            mirror('#background_fitting', 'Fit', 'How the picture fills the screen', {always: true}),
            mirror('#background_thumbnails_animation', 'Animate moving thumbnails', '', {always: true}),
            el('div', {class: 'rvo-lfield wide rvo-sbtns'},
                button('Upload a picture…', 'fa-upload', () => $('#add_bg_button').trigger('click'), 'Add a background (it becomes the current one)', 'primary'),
                mirrorButton('#auto_background', 'Pick one for this chat automatically', 'fa-wand-magic-sparkles', {after: 4000}))]),
        chatOnes.length ? section('bg-chat', 'This chat’s pictures', 'fa-comments', [el('div', {class: 'rvo-bggrid wide'}, ...chatOnes.map(file => tile(file, true)))], {sub: 'made for or added to this chat'}) : null,
        el('div', {class: 'rvo-chattools'}, el('label', {class: 'rvo-search rvo-chatsearch'}, icon('fa-magnifying-glass'), search)),
        shown.length ? el('div', {class: 'rvo-bggrid'}, ...shown.map(img => tile(img.filename, false))) : empty('fa-image', q ? 'No backgrounds match' : 'No backgrounds', q ? '' : 'Upload a picture to use it behind the chat.'));
}

// --------------------------------------------------------------- interface --
/** An on/off switch with its label and a hint (not inside a <label>: the switch is one already). */
const sw = (label, checked, onchange, hint = '') => el('div', {class: 'rvo-lfield rvo-swfield'}, toggle(checked, onchange, label), hint ? el('small', {text: hint}) : null);
// Rocsy's own layout, phone and notification settings first, then SillyTavern's User Settings grouped by its own
// headings (each control writes SillyTavern's input). Colour pickers and Custom CSS are SillyTavern's own
// controls, shown as they are under "Theme colours & custom CSS".
const UI_ICONS = [[/theme/i, 'fa-palette'], [/character/i, 'fa-user'], [/misc/i, 'fa-ellipsis'], [/css/i, 'fa-code'], [/chat|message/i, 'fa-comments'], [/script/i, 'fa-terminal'], [/language|general/i, 'fa-language']];
const ROCSY_THEMES = [['rocsy', 'Rocsy (dark)'], ['midnight', 'Midnight'], ['light', 'Light'], ['system', 'Follow the device'], ['sillytavern', 'SillyTavern theme']];

function stGroups(block) {
    const groups = [];
    let current = {title: 'General', nodes: []};
    groups.push(current);
    for (const node of block.querySelectorAll('h4, input, select, textarea, .menu_button[id]')) {
        if (node.tagName === 'H4') {
            const title = node.textContent.replace(/\s+/g, ' ').trim();
            if (title && shown(node)) { current = {title, nodes: []}; groups.push(current); }
            continue;
        }
        current.nodes.push(node);
    }
    return groups.filter(g => g.nodes.length);
}

function mirrorNodes(nodes) {
    const out = [];
    const radiosDone = new Set();
    for (const node of nodes) {
        if (!shown(node) || node.matches('input[type="hidden"], input[type="file"], input[type="search"], #settingsSearch')) continue;
        if (node.matches('.menu_button')) {
            const text = (node.textContent || '').replace(/\s+/g, ' ').trim() || node.title;
            if (text && !/logout|log out/i.test(text)) out.push(el('div', {class: 'rvo-lfield rvo-sbtn'}, button(text.slice(0, 40), 'fa-play', () => { $(node).trigger('click'); refreshSoon(H.page(), 800); }, node.title || text)));
            continue;
        }
        if (node.type === 'number' || node.type === 'text') {
            const near = [node.parentElement, node.parentElement?.parentElement].filter(Boolean);
            if (/_counter$|_value$/.test(node.id) || (node.type === 'number' && near.some(n => n.querySelector('input[type="range"]')))) continue;
        }
        if (node.type === 'radio') {
            if (radiosDone.has(node.name)) continue;
            radiosDone.add(node.name);
            const group = node.closest('.radio_group, .flex-container, div') || node.parentElement;
            const heading = document.querySelector(`label[for="${CSS.escape(node.name)}"]`)?.textContent?.trim() || group?.previousElementSibling?.textContent?.trim() || labelOf(group || node);
            const control = mirrorRadios(node.name, (heading || node.name).slice(0, 80));
            if (control) out.push(control);
            continue;
        }
        const control = mirror(node, labelOf(node).slice(0, 90));
        if (control) out.push(control);
    }
    return out;
}

async function notificationsSection() {
    const n = globalThis.RocsyAddons?.notify;
    if (!n) return section('phone', 'Phone & notifications', 'fa-mobile-screen', [note('Needs Rocsy Addons.')]);
    const phone = n.settings();
    const st = await n.state().catch(() => ({supported: false, reason: 'Couldn’t check.'}));
    const status = el('small', {class: 'rvo-muted'});
    const busy = async (fn, done) => { status.textContent = 'Working…'; try { await fn(); status.textContent = done; } catch (error) { status.textContent = error.message; toastr.error(error.message, 'Notifications'); } refreshSoon('interface', 600); };
    const notifyControls = !st.supported ? [note(st.reason || 'Not available here.')] : [
        sw('“Reply ready” notifications on this device', st.subscribed, on => busy(on ? () => n.enable({preview: st.preview}) : n.disable, on ? 'On for this device.' : 'Off for this device.'), st.permission === 'denied' ? 'Blocked in the browser: allow notifications for this site in its site settings.' : 'When a reply finishes while the phone is locked or you’re in another app. Works with the screen off.'),
        st.subscribed ? sw('Show the start of the reply', st.preview, on => busy(() => n.setPreview(on), on ? 'Previews on.' : 'Previews off.'), 'Off: just “<name> replied”, so nothing of the chat shows on the lock screen.') : null,
        st.subscribed ? el('div', {class: 'rvo-lfield rvo-sbtns'}, button('Send a test', 'fa-bell', () => busy(n.test, 'Test sent: it shows up in a few seconds.'), 'Send a test notification to every device that has them on'), status)
            : el('div', {class: 'rvo-lfield'}, status),
        st.devices ? note(`${st.devices} device${st.devices === 1 ? ' has' : 's have'} notifications on.`) : null,
    ];
    return section('phone', 'Phone & notifications', 'fa-mobile-screen', [
        ...notifyControls,
        sw('Keep the screen on while a reply is written', phone.keepAwake !== false, on => { phone.keepAwake = on; n.save(); }, n.wakeSupported() ? 'So the phone doesn’t lock half-way through and drop the connection.' : 'This browser can’t keep the screen on (needs the https address).'),
        field('…and for this long after (for the voice)', choose([['0', 'No longer'], ['30', '30 seconds'], ['90', '1½ minutes'], ['180', '3 minutes'], ['600', '10 minutes']],
            String(phone.awakeAfter ?? 90), value => { phone.awakeAfter = Number(value); n.save(); })),
        sw('Mark the tab when a reply is ready', phone.titleBadge !== false, on => { phone.titleBadge = on; n.save(); }, '“● Reply ready” in the tab title until you look.'),
        installButton(),
    ]);
}

// "Add to Home screen": Chrome's install prompt, when it offers one; otherwise how to do it by hand.
let installEvent = null;
addEventListener('beforeinstallprompt', event => { event.preventDefault(); installEvent = event; });
function installButton() {
    const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
    if (standalone) return note('RocsyTavern is running as an app from the home screen.');
    return el('div', {class: 'rvo-lfield wide rvo-sbtns'},
        button('Add RocsyTavern to the home screen', 'fa-mobile-screen-button', async () => {
            if (installEvent) { installEvent.prompt(); await installEvent.userChoice.catch(() => {}); installEvent = null; return; }
            toastr.info(/iPhone|iPad/.test(navigator.userAgent) ? 'In Safari: Share → Add to Home Screen.' : 'In the browser menu (⋮): Add to Home screen / Install app.', 'Home screen');
        }, 'Opens RocsyTavern full-screen like an app'));
}

async function renderInterface(wrap) {
    const L = H.layout;
    const block = $q('#user-settings-block-content') || $q('#user-settings-block');
    const groups = block ? stGroups(block) : [];
    put(wrap,
        section('layout', 'Rocsy layout', 'fa-wand-magic-sparkles', [
            field('Theme', choose(ROCSY_THEMES, L.get('theme'), value => L.setTheme(value)), '“SillyTavern theme” uses the colours from Theme colours below.'),
            sw('Wide chat column', !!L.get('wideChat'), on => L.set('wideChat', on)),
            sw('Show the chat background picture', !!L.get('showBackground'), on => L.set('showBackground', on), 'Off: a plain background in the Rocsy themes.'),
            sw('Show message details', !!L.get('showDetails'), on => L.set('showDetails', on), 'Message number, timer and tokens.'),
            sw('Expression picture on wide screens too', !!L.get('desktopExpression'), on => L.set('desktopExpression', on), 'Rocsy’s corner portrait / chat background (Expressions & images → On phones) also on a computer, instead of SillyTavern’s own.'),
            el('div', {class: 'rvo-lfield wide rvo-sbtns'},
                button('Keyboard shortcuts', 'fa-keyboard', () => H.showShortcuts?.(), 'All shortcuts (Ctrl+/)'),
                button('Classic SillyTavern layout', 'fa-table-columns', () => L.set('enabled', false), 'Switch the Rocsy layout off (the wand icon in the top bar brings it back)'))]),
        await notificationsSection(),
        ...groups.map((g, i) => section(`ui-${i}`, g.title.replace(/:$/, ''), UI_ICONS.find(([re]) => re.test(g.title))?.[1] || 'fa-sliders',
            mirrorNodes(g.nodes), {open: false})),
        section('ui-raw', 'Theme colours & custom CSS', 'fa-fill-drip', [el('div', {class: 'rvo-lfield wide rvo-adopt', 'data-adopt': 'colors'},
            button('Show SillyTavern’s colour pickers and Custom CSS here', 'fa-eye', event => {
                const host = event.currentTarget.closest('.rvo-adopt');
                const parts = ['#color-picker-block', '#CustomCSS-block'].map(id => $q(id)).filter(Boolean);
                host.replaceChildren();
                for (const part of parts) adopt(part, host);
            }, 'These are SillyTavern’s own controls, borrowed into this page'))], {open: false, sub: 'used by the “SillyTavern theme”'}));
}

// ------------------------------------------------------------ author's note --
// SillyTavern's Author's Note (its floating panel stays closed): this chat's note, the character's note and the
// default note for new chats. Every control writes SillyTavern's own input.
async function renderAuthorsNote(wrap) {
    const c = ctx();
    const open = !!c.getCurrentChatId?.();
    const m = (sel, label, hint = '', opts = {}) => mirror(sel, label, hint, {always: true, ...opts});
    const tokens = async (selector, out) => { try { out.textContent = `${await c.getTokenCountAsync($q(selector)?.value || '')} tokens`; } catch { /* fine */ } };
    const count = selector => { const out = el('small', {class: 'rvo-muted'}); tokens(selector, out); return out; };
    const charName = c.groupId ? '' : c.characters[c.characterId]?.name || '';
    put(wrap,
        note('The Author’s Note is put into the prompt every few messages, at the depth you choose: good for reminders the model should keep in mind (the scene, the time, a writing style).'),
        open ? section('an-chat', 'This chat’s note', 'fa-note-sticky', [
            m('#extension_floating_prompt', 'Note', 'Only this chat.', {rows: 6}), count('#extension_floating_prompt'),
            m('#extension_floating_interval', 'Every … messages', '0 = never'),
            m('#extension_floating_depth', 'Depth', 'How many messages from the end it goes in.'),
            mirrorRadios('extension_floating_position', 'Where'),
            m('#extension_floating_role', 'As'),
            m('#extension_floating_allow_wi_scan', 'Lorebooks can be triggered by it')]) : el('div', {class: 'rvo-swarn'}, icon('fa-circle-info'), el('span', {text: 'Open a chat to write its note. The default note below is used for new chats.'})),
        open && charName ? section('an-char', `${charName}’s note`, 'fa-user-pen', [
            m('#extension_use_floating_chara', 'Use a note for every chat with this character'),
            m('#extension_floating_chara', 'Character’s note', `Added to the chat’s own note in every chat with ${charName}.`, {rows: 4}),
            mirrorRadios('extension_floating_char_position', 'Combined with the chat’s note')], {open: !!$q('#extension_floating_chara')?.value}) : null,
        section('an-default', 'Default note for new chats', 'fa-file-circle-plus', [
            m('#extension_floating_default', 'Default note', 'New chats start with this note.', {rows: 4}),
            m('#extension_default_interval', 'Every … messages'),
            m('#extension_default_depth', 'Depth'),
            mirrorRadios('extension_default_position', 'Where'),
            m('#extension_default_role', 'As')], {open: !!$q('#extension_floating_default')?.value}));
}

export function init() {
    const {eventSource, eventTypes} = ctx();
    const on = (types, pages, delay = 300) => {
        for (const type of types) if (eventTypes[type]) eventSource.on(eventTypes[type], () => { if (pages.includes(H.page())) refreshSoon(H.page(), delay); });
    };
    on(['OAI_PRESET_CHANGED_AFTER', 'PRESET_CHANGED', 'MAIN_API_CHANGED', 'CHATCOMPLETION_SOURCE_CHANGED', 'CONNECTION_PROFILE_LOADED', 'ONLINE_STATUS_CHANGED', 'CHATCOMPLETION_MODEL_CHANGED'], ['connection', 'response'], 400);
    on(['CHAT_CHANGED'], ['persona', 'groups', 'quickreplies', 'regex', 'response', 'connection'], 300);
    on(['PERSONA_CREATED', 'PERSONA_DELETED', 'PERSONA_CHANGED', 'SETTINGS_UPDATED'], ['persona'], 300);
    on(['GROUP_UPDATED', 'GROUP_CHAT_CREATED', 'CHARACTER_DELETED'], ['groups'], 300);
    on(['CHAT_CHANGED'], ['backgrounds', 'authorsnote'], 400);
    $(document).on('change', '#add_bg_button', () => { if (H.page() === 'backgrounds') setTimeout(() => refreshSoon('backgrounds', 0), 2500); });
    // A picture picked for a persona lands after SillyTavern's own crop popup.
    $(document).on('change', '#avatar_upload_file', () => { if (H.page() === 'persona') setTimeout(() => refreshSoon('persona', 0), 1500); });
}
