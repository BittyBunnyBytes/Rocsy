// Rocsy Addons for SillyTavern: Rocsy's services wired in.
//   Voice      - SillyTavern's TTS through Rocsy Voice: pick the engine, the default voice and a voice per
//                character; new characters get the default voice automatically.
//   Connection - "Rocsy llama-swap" is created and used as the default connection profile.
//   Library    - browse Chub (the default), Bot Booru and datacat.run (JanitorAI's characters) and import cards in one click.
//   Chats      - chat names the Master Agent way: after N messages the chat gets a short, specific name (the
//                Rocsy layout's sidebar shows and renames them; a name you give it yourself always wins).
//   Image      - SillyTavern's Image Generation through Rocsy Image (the server's A1111 bridge); the character
//                editor's picture / expressions / voice tools live in rimage.js (globalThis.RocsyImage).
// The server half (plugins/rocsy, /api/plugins/rocsy) talks to Voice and llama-swap on the host and keeps the
// llama-swap key in SillyTavern's secret store.

import './rimage.js';
import {initResilientReplies} from './rgen.js';
import {initPhoneHelpers, notify} from './rnotify.js';

const MODULE = 'rocsy-addons';
const API = '/api/plugins/rocsy';
const PROFILE = 'Rocsy llama-swap';
const PROVIDER = 'OpenAI Compatible';
const DEFAULT_VOICE = '[Default Voice]';
// SillyTavern's "Different voices for quotes, *asterisks* and other text" looks a character up under these keys.
const SEGMENTS = [['("Quotes")', '“Quotes”'], ['(*Text inside asterisks*)', '*Text inside asterisks*'], ['(Other text)', 'Other text']];
const segmentKey = (name, suffix) => `${name} ${suffix}`;
const isSegmentKey = key => SEGMENTS.some(([suffix]) => key.endsWith(` ${suffix}`));
// SillyTavern's own TTS options (Extensions → TTS), shown in the Voice tab: [setting, its input id, label, hint].
const TTS_CHECKS = [
    ['auto_generation', 'tts_auto_generation', 'Read new replies automatically', 'Off: only the speaker button on a message reads it.'],
    ['periodic_auto_generation', 'tts_periodic_auto_generation', 'Narrate by paragraphs while streaming', 'Starts reading before the reply is finished. Needs automatic reading.'],
    ['narrate_by_paragraphs', 'tts_narrate_by_paragraphs', 'Narrate by paragraphs when not streaming', ''],
    ['narrate_user', 'tts_narrate_user', 'Read my messages too', ''],
    ['narrate_quoted_only', 'tts_narrate_quoted', 'Only read “quotes”', ''],
    ['narrate_dialogues_only', 'tts_narrate_dialogues', 'Skip *text inside asterisks*, even “quotes” in it', ''],
    ['narrate_translated_only', 'tts_narrate_translated_only', 'Only read the translated text', ''],
    ['skip_codeblocks', 'tts_skip_codeblocks', 'Skip code blocks', ''],
    ['skip_tags', 'tts_skip_tags', 'Skip <tagged> blocks', ''],
    ['pass_asterisks', 'tts_pass_asterisks', 'Send the asterisks to the voice engine', ''],
    ['multi_voice_enabled', 'tts_multi_voice_enabled', 'Different voices for “quotes”, *asterisks* and other text',
        'Each character gets three voice pickers below. Works best with asterisks sent and the two “only/skip” options off.'],
    ['apply_regex', 'tts_apply_regex', 'Remove text matching a regex before reading', ''],
];
const TTS_EVENTS = {apply_regex: 'change', regex_pattern: 'input', playback_rate: 'input'};
const TTS_INPUTS = {regex_pattern: 'tts_regex_pattern', playback_rate: 'playback_rate'};
const TTS_DEFAULTS = {auto_generation: true, playback_rate: 1, regex_pattern: ''};
const DEFAULTS = {
    ttsEnabled: true, engine: 'chatterbox_turbo', defaultVoice: '', speed: 1, characterVoices: {},
    autoMapVoices: true, autoConnect: true, model: '', tab: 'voice', showSpeed: true,
    library: {site: 'chub', nsfw: true, sort: {booru: 'latest', chub: 'default'}, filters: {}, imported: {}, bookmarks: {}, blurNsfw: false, hideImported: false},
    naming: {auto: true, after: 2, model: '', titles: {}},
    modelLoad: {mode: 'site', timeout: 10},
};

const ctx = () => globalThis.SillyTavern.getContext();
let settings = {...DEFAULTS};
let voiceInfo = null;      // {url, engines, voices} from the server plugin
let llamaInfo = null;      // {url, models}
let serverError = '';

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

function save() {
    ctx().extensionSettings[MODULE] = settings;
    ctx().saveSettingsDebounced();
}

async function api(path, options = {}) {
    const response = await fetch(API + path, {
        ...options, headers: {...ctx().getRequestHeaders(), ...(options.headers || {})}, cache: 'no-cache',
    });
    if (response.status === 404) throw new Error('The Rocsy Addons server part isn’t loaded. Reinstall Rocsy Addons in Rocsy (Core Backend → SillyTavern), which restarts SillyTavern.');
    if (!response.ok) {
        let message = `Request failed (${response.status})`;
        try { message = (await response.json()).error || message; } catch { /* not JSON */ }
        throw new Error(message);
    }
    return response.headers.get('content-type')?.includes('json') ? response.json() : response.blob();
}

// ------------------------------------------------------------------- voice --

async function loadVoice() {
    try {
        voiceInfo = await api('/voice');
        serverError = '';
        const names = voiceInfo.voices.map(v => v.name);
        if (!settings.defaultVoice || !names.includes(settings.defaultVoice)) settings.defaultVoice = names[0] || '';
        if (!voiceInfo.engines.some(e => e.id === settings.engine)) settings.engine = voiceInfo.engines.find(e => e.ready)?.id || settings.engine;
    } catch (error) {
        voiceInfo = null;
        serverError = error.message;
    }
}

/** The characters of the open chat (or the selected character), the names SillyTavern's voice map uses. */
function chatCharacters() {
    const c = ctx();
    if (c.groupId) {
        const group = c.groups.find(g => g.id === c.groupId);
        return (group?.members || []).map(avatar => c.characters.find(ch => ch.avatar === avatar)?.name).filter(Boolean);
    }
    const one = c.characters[c.characterId];
    return one ? [one.name] : [];
}

/** Write SillyTavern's TTS settings for Rocsy Voice, then have the TTS module reload them. */
async function applyVoice({quiet = false} = {}) {
    if (!voiceInfo) await loadVoice();
    if (!voiceInfo) { if (!quiet) toastr.error(serverError || 'Rocsy Voice isn’t reachable'); return false; }
    const ext = ctx().extensionSettings;
    ext.tts = ext.tts || {};
    const provider = ext.tts[PROVIDER] = {...(ext.tts[PROVIDER] || {})};
    const map = {...(typeof provider.voiceMap === 'object' && provider.voiceMap ? provider.voiceMap : {})};
    map[DEFAULT_VOICE] = settings.defaultVoice;
    for (const [name, voice] of Object.entries(settings.characterVoices)) map[name] = voice || DEFAULT_VOICE;
    for (const name of chatCharacters()) if (!(name in map)) map[name] = DEFAULT_VOICE;
    addSegmentVoices(map);
    Object.assign(provider, {
        provider_endpoint: voiceInfo.url, model: settings.engine, speed: Number(settings.speed) || 1,
        available_voices: voiceInfo.voices.map(v => v.name), voiceMap: map,
    });
    ext.tts.currentProvider = PROVIDER;
    ext.tts.enabled = settings.ttsEnabled;
    ctx().saveSettingsDebounced();
    const select = $('#tts_provider');
    if (select.length) {
        select.val(PROVIDER).trigger('change');
        const box = $('#tts_enabled');
        if (box.length && box.is(':checked') !== settings.ttsEnabled) { box.prop('checked', !settings.ttsEnabled); box.trigger('click'); }
    }
    if (!quiet) toastr.success(`Voice: ${engineName(settings.engine)}, default voice ${settings.defaultVoice}`, 'Rocsy Addons');
    return true;
}

/** Each character's quotes / asterisks / other-text voices (multi-voice): their own pick, else the character's voice. */
function addSegmentVoices(map) {
    for (const name of Object.keys(map)) {
        if (name === DEFAULT_VOICE || isSegmentKey(name)) continue;
        for (const [suffix] of SEGMENTS) {
            const key = segmentKey(name, suffix);
            map[key] = settings.characterVoices[key] || map[name];
        }
    }
}

const ttsSettings = () => ctx().extensionSettings.tts || {};
const ttsValue = key => ttsSettings()[key] ?? TTS_DEFAULTS[key] ?? false;

/** Change one of SillyTavern's TTS options through its own input when the TTS panel is loaded (so its handlers
 *  run: voice map refresh, regex warning…), else straight in its settings. */
function setTts(key, value) {
    const ext = ctx().extensionSettings;
    ext.tts = ext.tts || {};
    ext.tts[key] = value;
    const id = TTS_INPUTS[key] || TTS_CHECKS.find(([k]) => k === key)?.[1];
    const input = id && $(`#${id}`);
    if (input?.length) {
        if (input.is(':checkbox')) input.prop('checked', !!value); else input.val(value);
        input.triggerHandler(TTS_EVENTS[key] || 'click');
    }
    ctx().saveSettingsDebounced();
}

/** A character new to the voice map gets the default voice (SillyTavern otherwise errors "not in voicemap"). */
function mapNewCharacters() {
    if (!settings.autoMapVoices || !settings.ttsEnabled) return;
    const tts = ctx().extensionSettings.tts;
    if (!tts || tts.currentProvider !== PROVIDER) return;
    const map = tts[PROVIDER]?.voiceMap;
    if (!map || typeof map !== 'object') return;
    const names = chatCharacters();
    const missing = names.filter(name => !(name in map) || SEGMENTS.some(([suffix]) => !(segmentKey(name, suffix) in map)));
    if (!missing.length) return;
    for (const name of missing) if (!(name in map)) map[name] = settings.characterVoices[name] || DEFAULT_VOICE;
    addSegmentVoices(map);
    ctx().saveSettingsDebounced();
    $('#tts_provider').val(PROVIDER).trigger('change');
}

/** TTS is on but someone in this chat has no voice: SillyTavern would stop with "not in voicemap". Say so, with a fix. */
const voiceWarned = new Set();
function warnMissingVoices() {
    const tts = ctx().extensionSettings.tts;
    if (!tts?.enabled) return;
    const provider = tts.currentProvider;
    const map = tts[provider]?.voiceMap;
    if (!map || typeof map !== 'object') return;
    const missing = chatCharacters().filter(name => !(name in map));
    const fresh = missing.filter(name => !voiceWarned.has(`${provider}:${name}`));
    if (!fresh.length) return;
    fresh.forEach(name => voiceWarned.add(`${provider}:${name}`));
    const who = fresh.length === 1 ? fresh[0] : `${fresh.slice(0, -1).join(', ')} and ${fresh.at(-1)}`;
    toastr.warning(`${who} ${fresh.length === 1 ? 'has' : 'have'} no voice yet, so replies won’t be read out. Click to use the default voice.`, 'Voice', {
        timeOut: 12000, extendedTimeOut: 6000, closeButton: true,
        onclick: () => {
            for (const name of fresh) if (!(name in map)) map[name] = (provider === PROVIDER && settings.characterVoices?.[name]) || DEFAULT_VOICE;
            if (provider === PROVIDER) addSegmentVoices(map);
            ctx().saveSettingsDebounced();
            $('#tts_provider').val(provider).trigger('change');
            toastr.success('Default voice set. Pick another on the character’s page (Voice).', 'Voice');
        },
    });
}

const engineName = id => voiceInfo?.engines.find(e => e.id === id)?.name || id;

async function testVoice(voice, button) {
    if (!voice || voice === DEFAULT_VOICE) voice = settings.defaultVoice;
    button.disabled = true;
    const label = button.innerHTML;
    button.replaceChildren(icon('fa-spinner fa-spin'));
    try {
        const blob = await api('/voice/test', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({voice, engine: settings.engine})});
        const audio = new Audio(URL.createObjectURL(blob));
        audio.addEventListener('ended', () => URL.revokeObjectURL(audio.src));
        await audio.play();
    } catch (error) {
        toastr.error(error.message, 'Voice test');
    } finally {
        button.disabled = false;
        button.innerHTML = label;
    }
}

/** Give a character a voice (e.g. one just made in the character editor) and update SillyTavern's voice map. */
async function setCharacterVoice(name, voice) {
    await loadVoice();
    if (voice && voice !== DEFAULT_VOICE) settings.characterVoices[name] = voice; else delete settings.characterVoices[name];
    save();
    await applyVoice({quiet: true});
}

/** A voice picker for one character (the Rocsy layout's character page uses it). */
function voiceControl(name) {
    const box = el('div', {class: 'rad-voicepick'});
    const draw = () => {
        if (!voiceInfo) { box.replaceChildren(el('small', {class: 'rad-muted', text: serverError ? `Voice: ${serverError}` : 'Loading voices…'})); return; }
        const current = settings.characterVoices[name] || DEFAULT_VOICE;
        const pick = select([[DEFAULT_VOICE, `Default voice (${settings.defaultVoice})`], ...voiceInfo.voices.map(v => [v.name, v.name])], current, value => {
            if (value === DEFAULT_VOICE) delete settings.characterVoices[name]; else settings.characterVoices[name] = value;
            save();
            applyVoice({quiet: true});
        });
        const play = el('button', {class: 'menu_button rad-icon', type: 'button', title: 'Play a short sample'}, icon('fa-play'));
        play.addEventListener('click', () => testVoice(settings.characterVoices[name] || settings.defaultVoice, play));
        box.replaceChildren(el('span', {class: 'rad-label', text: 'Voice'}), pick, play);
    };
    draw();
    if (!voiceInfo) loadVoice().then(draw);
    return box;
}

// -------------------------------------------------------------- connection --

const profiles = () => ctx().extensionSettings.connectionManager?.profiles || [];
const ourProfile = () => profiles().find(p => p.name === PROFILE);
const selectedProfile = () => {
    const id = ctx().extensionSettings.connectionManager?.selectedProfile;
    return profiles().find(p => p.id === id);
};

async function slash(command) {
    const c = ctx();
    return c.executeSlashCommandsWithOptions ? c.executeSlashCommandsWithOptions(command, {handleExecutionErrors: true})
        : c.executeSlashCommands(command);
}

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

/** Point SillyTavern's Chat Completion at llama-swap (Custom, OpenAI-compatible) and connect. */
async function connectLlama(model) {
    const set = (selector, value, event = 'input') => $(selector).val(value).trigger(event);
    set('#main_api', 'openai', 'change');
    await wait(150);
    set('#chat_completion_source', 'custom', 'change');
    await wait(150);
    set('#custom_api_url_text', llamaInfo.url);
    if (model) set('#custom_model_id', model);
    // Local chat templates (Qwen, Gemma) need a user message first and alternating roles, or "Continue" on a
    // greeting fails with "No user query found". Strict is saved in the profile created next.
    set('#custom_prompt_post_processing', 'strict', 'change');
    $('#api_button_openai').trigger('click');
    await wait(1500);
}

/** Make "Rocsy llama-swap" exist and be the active profile. interactive = from the Connection tab. */
async function ensureConnection({interactive = false} = {}) {
    try {
        llamaInfo = await api('/llama/connect', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    } catch (error) {
        if (interactive) toastr.error(error.message, 'Rocsy llama-swap');
        return false;
    }
    if (!llamaInfo.models.length && interactive) toastr.warning(llamaInfo.error || 'llama-swap has no models configured yet.');
    const model = settings.model && llamaInfo.models.some(m => m.id === settings.model) ? settings.model
        : ourProfile()?.model || llamaInfo.models[0]?.id || '';
    const existing = ourProfile();
    if (!existing) {
        await connectLlama(model);
        await slash(`/profile-create ${PROFILE}`);
        await wait(300);
        if (!ourProfile()) { if (interactive) toastr.error('Couldn’t create the connection profile.'); return false; }
    }
    if (selectedProfile()?.name !== PROFILE || interactive) await slash(`/profile ${PROFILE}`);
    if (interactive && model && ourProfile()?.model !== model) await setModel(model);
    return true;
}

async function setModel(model) {
    settings.model = model;
    save();
    if (selectedProfile()?.name !== PROFILE) await slash(`/profile ${PROFILE}`);
    $('#custom_model_id').val(model).trigger('input');
    $('#model_custom_select').val(model).trigger('change');
    await wait(300);
    await slash(`/profile-update`);
    toastr.success(`Model: ${model}`, 'Rocsy llama-swap');
}

// ------------------------------------------------------------- chat names --
// Like the Master Agent's sessions: a chat's name is kept apart from its file (renaming the file would reload
// the chat and cut off TTS), in settings.naming.titles["c:<avatar>/<file>" | "g:<group>/<file>"] =
// {title, by: 'agent'|'user', asks}. The agent names a chat once it has `after` messages (the greeting doesn't
// count) and tries once more at the next message if that failed; a name by the user is never replaced, and an
// empty rename hands naming back.

const naming = () => settings.naming;
const ownerOf = ({avatar, group} = {}) => group ? `g:${group}` : avatar ? `c:${avatar}` : '';
const titleKey = (owner, file) => `${owner}/${String(file || '').replace(/\.jsonl$/, '')}`;
let namingBusy = '';

function currentOwner() {
    const c = ctx();
    if (c.groupId) return `g:${c.groupId}`;
    const character = c.characters[c.characterId];
    return character ? `c:${character.avatar}` : '';
}

/** The Master Agent's clean_title: one line, no quotes or trailing punctuation, at most 80 characters. */
function cleanTitle(text) {
    const title = String(text || '').split('\n').map(x => x.trim()).find(Boolean)?.replace(/\s+/g, ' ')
        .replace(/^(?:title|name)\s*:\s*/i, '').replace(/^["'“”‘’`*_]+|["'“”‘’`*_]+$/g, '').replace(/[.:;,]+$/, '').trim() || '';
    return title.length > 80 ? `${title.slice(0, 80).trimEnd()}…` : title;
}

function titleChanged() {
    save();
    document.dispatchEvent(new CustomEvent('rocsy-chat-titles'));
}

const getTitle = (owner, file) => naming().titles[titleKey(owner, file)] || null;

function setTitle(owner, file, title, by) {
    const key = titleKey(owner, file);
    naming().titles[key] = {...(naming().titles[key] || {}), title, by};
    titleChanged();
}

/** Rename from the sidebar: a name is the user's; an empty one hands naming back (named now if it's due). */
function renameChat(owner, file, title) {
    const clean = cleanTitle(title);
    if (clean) return setTitle(owner, file, clean, 'user');
    delete naming().titles[titleKey(owner, file)];
    titleChanged();
    if (owner === currentOwner() && String(ctx().getCurrentChatId?.()) === String(file).replace(/\.jsonl$/, '')) maybeName();
}

/** Messages that count: no system lines, and not the character's greeting. */
function countedMessages() {
    const chat = (ctx().chat || []).filter(m => !m.is_system);
    return chat.length && !chat[0].is_user ? chat.slice(1) : chat;
}

const chatModel = () => naming().model || ctx().chatCompletionSettings?.custom_model || ourProfile()?.model || settings.model || '';
const onLlamaSwap = () => {
    const cc = ctx().chatCompletionSettings || {};
    return ctx().mainApi === 'openai' && cc.chat_completion_source === 'custom' && /:8400\b|\/Llama\b/i.test(String(cc.custom_url || ''));
};

// ----------------------------------------------------------- model loading --
// Tells the server part, from every open tab, whether this tab needs the chat model right now (settings.modelLoad):
//   site   - while SillyTavern is open        chat - while a character or group chat is open (not on Library etc.)
//   active - while chatting, until `timeout` minutes without typing, sending or a reply        always - never unloaded
// The server loads the model when a tab starts needing it and unloads it (only that model) once no tab on any
// device has needed it for 30 s. A reply being generated always counts as needing it.
const LOAD_MODES = [
    ['site', 'While SillyTavern is open'],
    ['chat', 'Only while a character chat is open'],
    ['active', 'Only while actively chatting (with a timeout)'],
    ['always', 'Always (never unloaded by SillyTavern)'],
];
const loadSettings = () => settings.modelLoad;
const presenceState = {tab: '', started: false, lastActivity: 0, generating: false, sent: '', sentAt: 0, timer: null, info: null};

function presenceTabId() {
    try {
        const saved = sessionStorage.getItem('rocsy-presence-tab');
        if (saved) return saved;
    } catch { /* storage blocked */ }
    const id = `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
    try { sessionStorage.setItem('rocsy-presence-tab', id); } catch { /* storage blocked */ }
    return id;
}

function inCharacterChat() {
    const c = ctx();
    if (document.body.classList.contains('rvo-page-open')) return false;   // Rocsy Library / Characters / … page
    if (!c.getCurrentChatId?.()) return false;
    if (c.groupId) return true;
    const character = c.characterId !== undefined ? c.characters[c.characterId] : null;
    return !!character && character.avatar !== 'default_Assistant.png';
}

function wantsModel() {
    if (presenceState.generating || $('#mes_stop').is(':visible')) return true;
    const {mode, timeout} = loadSettings();
    if (mode === 'site' || mode === 'always') return true;
    if (!inCharacterChat()) return false;
    if (mode === 'chat') return true;
    return Date.now() - presenceState.lastActivity < Math.max(1, Number(timeout) || 10) * 60_000;
}

function sendPresence(force = false) {
    if (!presenceState.started) return;
    const model = onLlamaSwap() ? String(ctx().chatCompletionSettings?.custom_model || '') : '';
    const body = {tab: presenceState.tab, model, want: wantsModel(), mode: loadSettings().mode};
    const key = JSON.stringify(body);
    if (!force && key === presenceState.sent && Date.now() - presenceState.sentAt < 20_000) return;
    presenceState.sent = key;
    presenceState.sentAt = Date.now();
    api('/presence', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: key}).catch(() => { presenceState.sent = ''; });
}

function markActivity() {
    presenceState.lastActivity = Date.now();
    sendPresence();
}

function startPresence() {
    if (presenceState.started) return;
    presenceState.started = true;
    presenceState.tab = presenceTabId();
    const {eventSource, eventTypes} = ctx();
    const on = (type, fn) => { if (eventTypes[type]) eventSource.on(eventTypes[type], fn); };
    on('GENERATION_STARTED', (_type, _options, dryRun) => { if (dryRun) return; presenceState.generating = true; markActivity(); });
    for (const type of ['GENERATION_ENDED', 'GENERATION_STOPPED']) on(type, () => { presenceState.generating = false; markActivity(); });
    for (const type of ['MESSAGE_SENT', 'MESSAGE_SWIPED', 'MESSAGE_RECEIVED']) on(type, markActivity);
    on('CHAT_CHANGED', () => { if (inCharacterChat()) markActivity(); else sendPresence(); });
    document.addEventListener('input', event => { if (event.target?.id === 'send_textarea') markActivity(); }, true);
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') sendPresence(true); });
    window.addEventListener('pagehide', () => {
        try {
            fetch(`${API}/presence/leave`, {method: 'POST', keepalive: true, headers: {...ctx().getRequestHeaders(), 'Content-Type': 'application/json'},
                body: JSON.stringify({tab: presenceState.tab})});
        } catch { /* page is going anyway; the server forgets the tab after 150 s */ }
    });
    // Rocsy pages (Library, Characters) open and close by class on <body>.
    new MutationObserver(() => sendPresence()).observe(document.body, {attributes: true, attributeFilter: ['class']});
    presenceState.timer = setInterval(() => sendPresence(), 5000);
    sendPresence(true);
}

function modelLoadSection() {
    const ml = loadSettings();
    const timeout = el('input', {class: 'text_pole rad-number', type: 'number', min: '1', max: '720', step: '1', value: String(ml.timeout),
        onchange: event => { ml.timeout = Math.min(720, Math.max(1, parseInt(event.target.value, 10) || 10)); event.target.value = String(ml.timeout); save(); sendPresence(true); }});
    const info = presenceState.info;
    const running = info?.running?.map(r => r.model) || [];
    const ours = info?.claimed || [];
    const pending = ours.find(c => c.unloadIn !== null);
    const status = !info ? 'Checking…'
        : `${info.tabs} SillyTavern tab${info.tabs === 1 ? '' : 's'} open, ${info.wanting} need${info.wanting === 1 ? 's' : ''} the model · `
          + (running.length ? `loaded: ${running.join(', ')}` : 'no model loaded')
          + (pending ? ` · unloads ${pending.model} in ${pending.unloadIn} s` : '');
    return el('div', {class: 'rad-section'},
        field('Keep the model loaded', select(LOAD_MODES, ml.mode, value => { ml.mode = value; save(); sendPresence(true); render(); }),
            ml.mode === 'always' ? 'SillyTavern never unloads it. llama-swap’s own idle timer (Llama settings) and other Rocsy jobs that need the GPU can still unload it.'
                : 'Loads when it’s needed and unloads once no one, on any device, has needed it for 30 seconds. Only the model SillyTavern uses is unloaded.'),
        ml.mode === 'active' ? field('Unload after', el('div', {class: 'rad-inline'}, timeout, el('span', {text: 'minutes without chatting'})),
            'Typing, sending, swiping and replies count as chatting.') : null,
        el('p', {class: 'rad-muted', text: status}));
}

async function askForTitle(messages, character) {
    const lines = messages.slice(0, 12).map(m => ({user: !!m.is_user, name: m.name, text: m.mes}));
    if (onLlamaSwap() || naming().model) {
        const data = await api('/name', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({model: chatModel(), character, messages: lines})});
        return data.title;
    }
    // Another kind of connection: SillyTavern's own quiet generation.
    const transcript = lines.map(m => `${m.user ? 'User' : m.name || character}: ${String(m.text || '').replace(/\s+/g, ' ').slice(0, 700)}`).join('\n');
    return ctx().generateRaw({
        systemPrompt: 'You name chat sessions. Reply with only the name: a short, specific name of 2-6 words that says what this chat is about. No quotes, emoji or trailing punctuation.',
        prompt: `Chat with ${character}:\n${transcript}\n\nName this chat.`, responseLength: 60,
    });
}

/** Name the open chat. force = the "Name this chat now" button (also renames one the agent named before). */
async function nameChat({force = false} = {}) {
    const c = ctx();
    const owner = currentOwner();
    const file = c.getCurrentChatId?.();
    if (!owner || !file) { if (force) toastr.info('Open a chat first.'); return; }
    const key = titleKey(owner, file);
    if (namingBusy === key) return;
    const entry = naming().titles[key] || {};
    if (!force && (entry.by || (entry.asks || 0) >= 2)) return;
    if (force && entry.by === 'user') delete entry.by;
    const messages = countedMessages();
    if (!messages.length) { if (force) toastr.info('This chat has no messages yet.'); return; }
    const character = c.groupId ? (c.groups.find(g => g.id === c.groupId)?.name || 'a group') : c.characters[c.characterId]?.name || 'a character';
    namingBusy = key;
    naming().titles[key] = {...entry, asks: (entry.asks || 0) + 1};
    try {
        const title = cleanTitle(await askForTitle(messages, character));
        if (!title) throw new Error('The model gave no name.');
        if (naming().titles[key]?.by === 'user') return;   // renamed by hand meanwhile
        setTitle(owner, file, title, 'agent');
        if (force) toastr.success(`Named “${title}”`, 'Chat name');
    } catch (error) {
        save();
        console.warn('[Rocsy Addons] chat name', error);
        if (force) toastr.error(error.message, 'Chat name');
    } finally {
        namingBusy = '';
    }
}

function maybeName() {
    if (!naming().auto || $('#mes_stop').is(':visible')) return;
    if (countedMessages().length >= Math.max(1, Number(naming().after) || 2)) nameChat().catch(() => {});
}

function onChatRenamed(data) {
    const owner = data?.groupId ? `g:${data.groupId}` : data?.avatarId ? `c:${data.avatarId}` : '';
    const from = owner && naming().titles[titleKey(owner, data.oldFileName)];
    if (!from) return;
    naming().titles[titleKey(owner, data.newFileName)] = from;
    delete naming().titles[titleKey(owner, data.oldFileName)];
    titleChanged();
}

// ------------------------------------------------------------ tokens / second --
// Each reply gets a small "46 tok/s" chip by its name: its tokens (text + reasoning, counted with SillyTavern's
// tokenizer) over the time from the first token to the end. The tooltip has the details.

async function annotateSpeed(messageId) {
    const node = document.querySelector(`#chat .mes[mesid="${messageId}"]`);
    node?.querySelector('.rad-tps')?.remove();
    if (!settings.showSpeed || !node) return;
    const message = ctx().chat[messageId];
    if (!message || message.is_user || message.is_system || !message.gen_started || !message.gen_finished) return;
    const total = Date.parse(message.gen_finished) - Date.parse(message.gen_started);
    const first = Number(message.extra?.time_to_first_token) || 0;
    const generating = (total - first) / 1000;
    if (!(generating > 0.2)) return;
    let tokens = 0;
    try {
        const text = `${message.extra?.reasoning || ''}\n${message.mes || ''}`;
        tokens = ctx().getTokenCountAsync ? await ctx().getTokenCountAsync(text) : ctx().getTokenCount(text);
    } catch { return; }
    if (!tokens) return;
    const rate = tokens / generating;
    const chip = el('span', {class: 'rad-tps', text: `${rate >= 100 ? Math.round(rate) : rate.toFixed(1)} tok/s`,
        title: `≈${tokens} tokens in ${generating.toFixed(1)} s` + (first ? ` · first token after ${(first / 1000).toFixed(2)} s` : '')
            + (message.extra?.model ? ` · ${message.extra.model}` : '')});
    node.querySelector('.rad-tps')?.remove();  // another event for this message may have finished first
    const anchor = node.querySelector('.ch_name .name_text');
    anchor ? anchor.after(chip) : node.querySelector('.ch_name')?.append(chip);
}

function annotateAll() {
    document.querySelectorAll('#chat .rad-tps').forEach(chip => chip.remove());
    if (!settings.showSpeed) return;
    const chat = ctx().chat || [];
    for (let id = Math.max(0, chat.length - 200); id < chat.length; id++) annotateSpeed(id);
}

// --------------------------------------------------------------------- UI --

let dialog = null;

function field(label, control, hint) {
    return el('label', {class: 'rad-field'}, el('span', {class: 'rad-label', text: label}), control, hint ? el('small', {text: hint}) : null);
}

function select(options, value, onchange) {
    const node = el('select', {class: 'text_pole', onchange: event => onchange(event.target.value)},
        ...options.map(([v, label, disabled]) => el('option', {value: v, text: label, disabled: !!disabled})));
    node.value = value;
    return node;
}

function renderVoiceTab(body) {
    if (!voiceInfo) {
        body.replaceChildren(el('p', {class: 'rad-error', text: serverError || 'Loading…'}),
            el('button', {class: 'menu_button', type: 'button', onclick: async () => { await loadVoice(); render(); }}, icon('fa-rotate'), el('span', {text: 'Try again'})));
        return;
    }
    const voices = voiceInfo.voices.map(v => [v.name, v.name]);
    const engines = voiceInfo.engines.map(e => [e.id, `${e.name}${e.ready ? '' : ' · not installed'}`, !e.ready]);
    const enabled = el('input', {type: 'checkbox', onchange: event => { settings.ttsEnabled = event.target.checked; save(); applyVoice(); }});
    enabled.checked = settings.ttsEnabled;
    const testButton = voice => {
        const button = el('button', {class: 'menu_button rad-icon', type: 'button', title: 'Play a short sample'}, icon('fa-play'));
        button.addEventListener('click', () => testVoice(voice(), button));
        return button;
    };
    const defaultVoice = select(voices, settings.defaultVoice, value => { settings.defaultVoice = value; save(); applyVoice({quiet: true}); });
    const rows = chatCharacters().map(name => {
        const current = settings.characterVoices[name] || DEFAULT_VOICE;
        const control = select([[DEFAULT_VOICE, `Default (${settings.defaultVoice})`], ...voices], current, value => {
            if (value === DEFAULT_VOICE) delete settings.characterVoices[name]; else settings.characterVoices[name] = value;
            save();
            applyVoice({quiet: true});
        });
        const row = el('div', {class: 'rad-row'}, el('span', {class: 'rad-name', text: name}), control, testButton(() => settings.characterVoices[name] || settings.defaultVoice));
        if (!ttsValue('multi_voice_enabled')) return row;
        const own = () => settings.characterVoices[name] || settings.defaultVoice;
        const parts = SEGMENTS.map(([suffix, label]) => {
            const key = segmentKey(name, suffix);
            const pick = select([['', `Same as ${name} (${own()})`], ...voices], settings.characterVoices[key] || '', value => {
                if (value) settings.characterVoices[key] = value; else delete settings.characterVoices[key];
                save();
                applyVoice({quiet: true});
            });
            return el('div', {class: 'rad-row rad-subrow'}, el('span', {class: 'rad-name', text: label}), pick,
                testButton(() => settings.characterVoices[key] || own()));
        });
        return el('div', {class: 'rad-voicegroup'}, row, ...parts);
    });
    const speed = el('input', {type: 'range', min: '0.5', max: '2', step: '0.05', value: String(settings.speed),
        oninput: event => { settings.speed = Number(event.target.value); speedOut.textContent = `${settings.speed.toFixed(2)}×`; },
        onchange: () => { save(); applyVoice({quiet: true}); }});
    const speedOut = el('span', {class: 'rad-value', text: `${Number(settings.speed).toFixed(2)}×`});
    const rate = Number(ttsValue('playback_rate')) || 1;
    const rateOut = el('span', {class: 'rad-value', text: `${rate.toFixed(2)}×`});
    const playback = el('input', {type: 'range', min: '0.25', max: '3', step: '0.05', value: String(rate),
        oninput: event => { rateOut.textContent = `${Number(event.target.value).toFixed(2)}×`; },
        onchange: event => setTts('playback_rate', Number(event.target.value))});
    const checks = TTS_CHECKS.map(([key, , label, hint]) => {
        const box = el('input', {type: 'checkbox', onchange: event => {
            setTts(key, event.target.checked);
            if (key === 'multi_voice_enabled') applyVoice({quiet: true}).then(render);
            else if (key === 'apply_regex') render();
        }});
        box.checked = !!ttsValue(key);
        return el('label', {class: 'checkbox_label rad-check', title: hint || null}, box,
            el('span', {}, el('span', {text: label}), hint ? el('small', {text: hint}) : null));
    });
    let regexError = null;
    const checkRegex = value => {
        let message = '';
        const match = value.match(/^\/(.*)\/([a-z]*)$/s);
        try { if (value) new RegExp(match ? match[1] : value, match ? match[2] : 'g'); } catch (error) { message = `Invalid regex: ${error.message}`; }
        regexError.textContent = message;
        regexError.hidden = !message;
    };
    const regex = ttsValue('apply_regex') ? field('Regex (matching text is removed)', el('input', {type: 'text', class: 'text_pole',
        value: ttsValue('regex_pattern') || '', placeholder: String.raw`/[^a-zA-Z0-9\s.,!?;:'"()]+/g`,
        oninput: event => { checkRegex(event.target.value); setTts('regex_pattern', event.target.value); }})) : null;
    if (regex) { regexError = el('small', {class: 'rad-error', hidden: true}); regex.append(regexError); checkRegex(ttsValue('regex_pattern') || ''); }
    body.replaceChildren(
        el('label', {class: 'checkbox_label rad-toggle'}, enabled, el('span', {text: 'Read replies aloud with Rocsy Voice'})),
        field('Voice engine', select(engines, settings.engine, value => { settings.engine = value; save(); applyVoice({quiet: true}); }),
            voiceInfo.engines.find(e => e.id === settings.engine)?.detail || ''),
        field('Default voice', el('div', {class: 'rad-inline'}, defaultVoice, testButton(() => settings.defaultVoice)),
            'Used by every character without a voice of their own. Voices are made on Rocsy’s Voice page.'),
        el('div', {class: 'rad-section'}, el('span', {class: 'rad-label', text: 'Voices in this chat'}),
            ...(rows.length ? rows : [el('small', {text: 'Open a chat to give its characters their own voices.'})])),
        field('Voice speed', el('div', {class: 'rad-inline'}, speed, speedOut), 'How fast the engine speaks (re-generates the audio).'),
        field('Playback speed', el('div', {class: 'rad-inline'}, playback, rateOut), 'Speeds up or slows down playback in the browser.'),
        el('div', {class: 'rad-section'}, el('span', {class: 'rad-label', text: 'What gets read'}), ...checks),
        regex,
        el('label', {class: 'checkbox_label'}, Object.assign(el('input', {type: 'checkbox', onchange: event => { settings.autoMapVoices = event.target.checked; save(); }}), {checked: settings.autoMapVoices}),
            el('span', {text: 'Give new characters the default voice automatically'})),
        el('div', {class: 'rad-actions'},
            el('button', {class: 'menu_button', type: 'button', onclick: () => applyVoice()}, icon('fa-check'), el('span', {text: 'Apply to SillyTavern TTS'})),
            el('button', {class: 'menu_button', type: 'button', onclick: async () => { await loadVoice(); render(); }}, icon('fa-rotate'), el('span', {text: 'Refresh voices'}))));
}

function renderConnectionTab(body) {
    const profile = selectedProfile();
    const active = profile?.name === PROFILE;
    const models = llamaInfo?.models || [];
    const auto = el('input', {type: 'checkbox', onchange: event => { settings.autoConnect = event.target.checked; save(); }});
    auto.checked = settings.autoConnect;
    const status = !llamaInfo ? 'Checking llama-swap…'
        : llamaInfo.error && !models.length ? `llama-swap isn’t reachable: ${llamaInfo.error}`
        : `llama-swap at ${llamaInfo.url} · ${models.length} model${models.length === 1 ? '' : 's'}`;
    body.replaceChildren(...[
        el('div', {class: `rad-status ${active ? 'ok' : ''}`}, icon(active ? 'fa-circle-check' : 'fa-circle-info'),
            el('span', {text: active ? `Using the “${PROFILE}” profile` : profile ? `Using “${profile.name}”` : 'No connection profile selected'})),
        el('p', {class: 'rad-muted', text: status}),
        models.length ? field('Model', select(models.map(m => [m.id, m.name]), ourProfile()?.model || settings.model || models[0].id, setModel),
            'llama-swap loads it on the first message.') : null,
        modelLoadSection(),
        el('label', {class: 'checkbox_label'}, auto, el('span', {text: `Use “${PROFILE}” as the default connection when SillyTavern starts`})),
        el('label', {class: 'checkbox_label'}, Object.assign(el('input', {type: 'checkbox', onchange: event => { settings.showSpeed = event.target.checked; save(); annotateAll(); }}), {checked: settings.showSpeed}),
            el('span', {text: 'Show tokens per second on each reply'})),
        el('div', {class: 'rad-actions'},
            el('button', {class: 'menu_button', type: 'button', onclick: async () => { if (await ensureConnection({interactive: true})) toastr.success(`Connected through ${PROFILE}`); render(); }},
                icon('fa-plug'), el('span', {text: active ? 'Reconnect' : `Switch to ${PROFILE}`})))].filter(Boolean));
    if (!llamaInfo) api('/llama').then(data => { llamaInfo = data; render(); }).catch(error => { llamaInfo = {url: '', models: [], error: error.message}; render(); });
    if (!body.dataset.presenceAsked) {
        body.dataset.presenceAsked = '1';
        api('/presence').then(data => { presenceState.info = data; if (settings.tab === 'connection' || connectionHost) render(); })
            .catch(() => {}).finally(() => setTimeout(() => { delete body.dataset.presenceAsked; }, 3000));
    }
}

// ----------------------------------------------------------------- library --
// Bot Booru and Chub are searched through the server plugin (/library/search) and shown as a grid; datacat.run is
// embedded as is and talks to us through its SillyTavern bridge (postMessage), building each PNG card itself.
// Imported cards are remembered per site id (settings.library.imported), so a second click opens the character.

const SITES = [
    // Chub (chub.ai) first: the default source, marked with a star.
    {id: 'chub', label: 'Chub', icon: 'fa-star', title: 'Chub (chub.ai), the biggest card library: the default source', home: 'https://chub.ai/', sorts: [['default', 'Default'], ['trending_downloads', 'Trending'], ['n_favorites', '# Favorites'], ['star_count', 'Popularity'], ['download_count', 'Downloads'], ['rating', 'Rating'], ['rating_count', '# Ratings'], ['last_activity_at', 'Updated'], ['created_at', 'Created'], ['name', 'Name'], ['n_tokens', '# Tokens'], ['random', 'Random']]},
    {id: 'booru', label: 'Bot Booru', home: 'https://botbooru.com/', sorts: [['latest', 'Latest'], ['downloads', 'Most downloaded'], ['favorites', 'Most favorited'], ['views', 'Most viewed'], ['curated', 'Curated'], ['random', 'Random']]},
    {id: 'datacat', label: 'datacat.run', title: 'datacat.run: JanitorAI’s characters with their full cards', home: 'https://datacat.run/'},
    // Lorebooks (SillyTavern's World Info): imported into World Info, not as characters.
    {id: 'chub-lore', kind: 'lore', label: 'Chub', full: 'Chub lorebooks', icon: 'fa-star', title: 'Lorebooks on Chub (chub.ai)', home: 'https://chub.ai/lorebooks',
        sorts: [['default', 'Default'], ['download_count', 'Downloads'], ['star_count', 'Popularity'], ['n_favorites', '# Favorites'], ['trending_downloads', 'Trending'], ['last_activity_at', 'Updated'], ['created_at', 'Created'], ['name', 'Name'], ['n_tokens', '# Tokens'], ['random', 'Random']]},
    {id: 'booru-lore', kind: 'lore', label: 'Bot Booru', full: 'Bot Booru lorebooks', title: 'Lorebooks on Bot Booru', home: 'https://botbooru.com/',
        sorts: [['latest', 'Latest'], ['downloads', 'Most downloaded'], ['favorites', 'Most favorited'], ['views', 'Most viewed']]},
];
const KINDS = [['cards', 'Characters', 'fa-address-card'], ['lore', 'Lorebooks', 'fa-book-atlas']];
const isLore = site => /-lore$/.test(String(site || ''));
const kindOf = site => (isLore(site) ? 'lore' : 'cards');
const accountOf = site => String(site || '').replace(/-lore$/, '');
const siteLabel = id => { const s = SITES.find(x => x.id === id); return s ? s.full || s.label : id; };
const JANITOR_ID = /janitorai\.com\/characters\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
const DATACAT_ORIGIN = /^https:\/\/([a-z0-9-]+\.)*datacat\.run$/;
const DC = {
    init: 'datacat:st-bridge:init', ready: 'datacat:st-bridge:ready', prepare: 'datacat:st-character-card:prepare',
    card: 'datacat:st-character-card', ack: 'datacat:st-character-card:ack', source: 'sillytavern-datacat-browser',
};

const library = {root: null, results: {}, datacat: null};   // results[site] = {q, items, page, more, total, loading, error}
const lib = () => settings.library;
const importedKey = (site, id) => `${site}:${id}`;

/** The avatar file of an imported card, if that character is still in SillyTavern. */
function importedAvatar(site, id) {
    const avatar = lib().imported[importedKey(site === 'janitor' ? 'datacat' : site, id)];
    return avatar && ctx().characters.some(c => c.avatar === avatar) ? avatar : '';
}

// SillyTavern's own World Info module (same URL as SillyTavern's import, so the same live module).
let wiModule = null;
const worldInfo = async () => (wiModule ||= await import('../../../world-info.js'));
const worldNames = () => wiModule?.world_names || [];

/** The World Info name of an imported lorebook, if it's still in SillyTavern. */
function importedBook(site, id) {
    const name = lib().imported[importedKey(site, id)];
    return name && worldNames().includes(name) ? name : '';
}

/** Show a lorebook: the Rocsy Lorebooks page when the Rocsy layout is on, else SillyTavern's World Info panel. */
async function openLorebook(name) {
    const wi = await worldInfo();
    dialog?.close();
    if (typeof globalThis.RocsyVisualOverhaul?.openLorebook === 'function' && document.body.classList.contains('rvo')) return globalThis.RocsyVisualOverhaul.openLorebook(name);
    if (!document.getElementById('WorldInfo')?.classList.contains('openDrawer')) $('#WIDrawerIcon').trigger('click');
    const index = wi.world_names.indexOf(name);
    if (index >= 0) $('#world_editor_select').val(index).trigger('change');
}

/** Put a lorebook (World Info JSON) into SillyTavern under the given name; returns its World Info name, or '' if cancelled. */
async function importLorebook(book, title, site, id) {
    const wi = await worldInfo();
    const before = new Set(wi.world_names);
    const name = String(title || 'Lorebook').replace(/[\\/:*?"<>|\u0000-\u001f]+/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 100) || 'Lorebook';
    const result = await wi.importWorldInfo(new File([JSON.stringify(book)], `${name}.json`, {type: 'application/json'}));
    if (result === false) return '';
    const added = wi.world_names.find(n => !before.has(n)) || wi.world_names.find(n => n.toLowerCase() === name.toLowerCase());
    if (!added) throw new Error('SillyTavern didn’t take the lorebook.');
    lib().imported[importedKey(site, id)] = added;
    save();
    document.dispatchEvent(new CustomEvent('rocsy-lorebooks-changed'));
    return added;
}

/** A lorebook file through SillyTavern's URL importer (Chub). */
async function fetchLoreViaSillyTavern(url) {
    const response = await fetch('/api/content/importURL', {method: 'POST', headers: ctx().getRequestHeaders(), body: JSON.stringify({url})});
    if (!response.ok) {
        const text = (await response.text().catch(() => '')).slice(0, 200);
        throw new Error(`SillyTavern couldn’t download the lorebook (${response.status}${text ? `: ${text}` : ''})`);
    }
    if (response.headers.get('X-Custom-Content-Type') !== 'lorebook') throw new Error('That link isn’t a lorebook.');
    return response.json();
}

async function openCharacter(avatar, {close = true} = {}) {
    const index = ctx().characters.findIndex(c => c.avatar === avatar);
    if (index < 0) throw new Error('That character isn’t in SillyTavern any more.');
    if (close) {
        dialog?.close();
        document.dispatchEvent(new CustomEvent('rocsy-library-close'));  // the Rocsy layout's Library page
    }
    await ctx().selectCharacterById(index);
}

/** Put a card file into SillyTavern; returns the new character's avatar file name. */
async function importCard(blob, fileName, site, id) {
    if ($('#mes_stop').is(':visible')) throw new Error('Stop the reply that’s being written first, then import.');
    const name = /\.(png|json|charx|yaml|yml)$/i.test(fileName) ? fileName : `${fileName || 'card'}.png`;
    const form = new FormData();
    form.append('avatar', new File([blob], name, {type: blob.type || 'image/png'}));
    form.append('file_type', name.split('.').pop().toLowerCase());
    form.append('user_name', ctx().name1 || 'User');
    const headers = ctx().getRequestHeaders();
    delete headers['Content-Type'];
    const response = await fetch('/api/characters/import', {method: 'POST', body: form, headers, cache: 'no-cache'});
    const data = response.ok ? await response.json().catch(() => ({})) : {};
    if (!response.ok || data.error || data.file_name === undefined) throw new Error(data.error || `SillyTavern couldn’t import the card (${response.status} ${response.statusText})`);
    const avatar = `${data.file_name}.png`;
    await ctx().getCharacters();
    lib().imported[importedKey(site, id)] = avatar;
    save();
    return avatar;
}

/** A card file through SillyTavern's own URL importer (Chub, JanitorAI, Pygmalion, RisuRealm …). */
async function fetchViaSillyTavern(url) {
    const response = await fetch('/api/content/importURL', {method: 'POST', headers: ctx().getRequestHeaders(), body: JSON.stringify({url})});
    if (!response.ok) {
        const text = (await response.text().catch(() => '')).slice(0, 200);
        throw new Error(response.status === 500 && /chub/i.test(url)
            ? `Chub didn’t hand the card over (${response.status}${text ? `: ${text}` : ''})`
            : `SillyTavern couldn’t download it (${response.status}${text ? `: ${text}` : ''})`);
    }
    if (response.headers.get('X-Custom-Content-Type') && response.headers.get('X-Custom-Content-Type') !== 'character') throw new Error('That link is a lorebook, not a character.');
    const fileName = (response.headers.get('Content-Disposition') || '').split('filename=')[1]?.replace(/"/g, '') || 'card.png';
    return {blob: await response.blob(), fileName};
}

/** Open one character in the datacat tab (a pasted janitorai.com link: JanitorAI ids are datacat's); its ST Chat button imports the full card. */
function openInDatacat(id) {
    const host = library.root?.parentElement;
    lib().site = 'datacat';
    save();
    library.datacatPath = `/characters/recent/${encodeURIComponent(id)}`;
    if (library.root) library.root.dataset.site = '';   // reload the datacat page even when it's showing
    if (host) renderLibraryTab(host); else open('library');
    toastr.info('Press the character’s “ST Chat” button on datacat to import it.', 'JanitorAI', {timeOut: 6000});
}

async function importLoreItem(site, item, button) {
    const existing = importedBook(site, item.id);
    if (existing) return openLorebook(existing).catch(error => toastr.error(error.message));
    button.disabled = true;
    button.replaceChildren(icon('fa-spinner fa-spin'), el('span', {text: 'Importing…'}));
    try {
        const book = site === 'booru-lore' ? await api(`/library/lorebook?site=booru-lore&id=${encodeURIComponent(item.id)}`) : await fetchLoreViaSillyTavern(item.url);
        await importLorebook(book, item.name, site, item.id);
    } catch (error) {
        toastr.error(error.message, `Couldn’t import ${item.name}`);
    }
    if (button.isConnected) button.replaceWith(cardButton(site, item));
}

async function importItem(site, item, button) {
    if (isLore(site)) return importLoreItem(site, item, button);
    const existing = importedAvatar(site, item.id);
    if (existing) return openCharacter(existing).catch(error => toastr.error(error.message));
    button.disabled = true;
    button.replaceChildren(icon('fa-spinner fa-spin'), el('span', {text: 'Importing…'}));
    try {
        const {blob, fileName} = site === 'booru'
            ? {blob: await api(`/library/card?site=booru&id=${encodeURIComponent(item.id)}`), fileName: `${item.name}.png`}
            : await fetchViaSillyTavern(item.url);
        await importCard(blob, fileName, site, item.id);
        toastr.success(`${item.name} is in your characters`, 'Library');
    } catch (error) {
        toastr.error(error.message, `Couldn’t import ${item.name}`);
    }
    if (button.isConnected) button.replaceWith(cardButton(site, item));
}

function cardButton(site, item) {
    const lore = isLore(site);
    const have = lore ? !!importedBook(site, item.id) : !!importedAvatar(site, item.id);
    const label = have ? (lore ? 'Open lorebook' : 'Open chat') : 'Import';
    const button = el('button', {class: `menu_button rad-card-action ${have ? 'rad-have' : ''}`, type: 'button',
},
        icon(have ? (lore ? 'fa-book-open' : 'fa-comments') : 'fa-download'), el('span', {text: label}));
    if (lore && !have) button.title = 'Add it to SillyTavern’s World Info (Lorebooks)';
    button.addEventListener('click', () => importItem(site, item, button));
    return button;
}

const compact = n => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k` : String(n);

function cardTile(site, item, view = site) {
    const facts = [view === 'bookmarks' && siteLabel(site), item.stats?.entries && `${compact(item.stats.entries)} entries`, item.tokens && `${compact(item.tokens)} tokens`,
        item.stats?.downloads && `${compact(item.stats.downloads)} ↓`, item.stats?.stars && `${compact(item.stats.stars)} ★`].filter(Boolean).join(' · ');
    return el('article', {class: `rad-card${isLore(site) ? ' rad-lore' : ''}${item.blocked ? ' rad-blocked' : ''}`, title: item.blocked ? `Hidden by your settings: ${item.blocked}` : undefined},
        el('div', {class: 'rad-thumbwrap'},
            el('button', {class: `rad-thumb${item.nsfw ? ' rad-nsfw' : ''}`, type: 'button', title: `See ${item.name}’s card`, onclick: () => openDetails(site, item, view)},
                item.thumb ? el('img', {src: item.thumb, alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer'}) : icon(isLore(site) ? 'fa-book-atlas' : 'fa-user'),
                item.nsfw ? el('span', {class: 'rad-badge', text: 'NSFW'}) : null,
                item.blocked ? el('span', {class: 'rad-badge rad-blockbadge', text: 'Hidden'}) : null),
            bookmarkButton(site, item)),
        el('div', {class: 'rad-card-body'},
            el('button', {class: 'rad-card-name', type: 'button', text: item.name, title: `See ${item.name}’s card`, onclick: () => openDetails(site, item, view)}),
            facts || item.creator ? el('small', {class: 'rad-muted rad-cardfacts'},
                ...(item.creator ? creatorLinks(site, item.creator, {prefix: 'by '}) : []),
                facts ? el('span', {text: `${item.creator ? ' · ' : ''}${facts}`}) : null) : null,
            item.tagline ? el('p', {class: 'rad-card-text', text: item.tagline}) : null,
            item.tags?.length ? el('div', {class: 'rad-tags'}, ...item.tags.slice(0, 5).map(tag => tagButton(site, tag))) : null,
            cardButton(site, item)));
}

// ---------------------------------------------------------------- bookmarks --
// Any Chub / Bot Booru card can be bookmarked (from the results or its details); the Bookmarks view lists them all
// across sites. Each keeps a copy of what the card showed (name, picture, tags …) in settings.library.bookmarks.

const bookmarkKey = (site, id) => `${site}:${id}`;
const isBookmarked = (site, id) => !!lib().bookmarks[bookmarkKey(site, id)];
const bookmarkCount = () => Object.keys(lib().bookmarks).length;

function toggleBookmark(site, item) {
    const key = bookmarkKey(site, item.id);
    if (lib().bookmarks[key]) delete lib().bookmarks[key];
    else {
        const {id, name, creator, tagline, tags, tokens, stats, thumb, url, nsfw} = item;
        lib().bookmarks[key] = {site, at: Date.now(), item: {id, name, creator, tagline, tags: (tags || []).slice(0, 12), tokens, stats, thumb, url, nsfw}};
    }
    save();
    const on = !!lib().bookmarks[key];
    document.querySelectorAll(`[data-bookmark="${CSS.escape(key)}"]`).forEach(button => paintBookmark(button, on));
    document.querySelectorAll('.rad-bookmarks-btn .rad-count').forEach(badge => { badge.textContent = bookmarkCount() ? String(bookmarkCount()) : ''; });
    if (lib().site === 'bookmarks' && !library.results.bookmarks?.detail) drawResults('bookmarks');
    toastr.info(on ? `Bookmarked ${item.name}` : `Removed ${item.name} from bookmarks`, 'Library', {timeOut: 1800});
}

function paintBookmark(button, on) {
    button.classList.toggle('on', on);
    button.setAttribute('aria-pressed', String(on));
    button.title = on ? 'Remove the bookmark' : 'Bookmark it';
    button.querySelector('i').className = `${on ? 'fa-solid' : 'fa-regular'} fa-bookmark fa-fw`;
    const label = button.querySelector('span');
    if (label) label.textContent = on ? 'Bookmarked' : 'Bookmark';
}

function bookmarkButton(site, item, {wide = false} = {}) {
    const button = el('button', {class: wide ? 'menu_button rad-bm-wide' : 'rad-bm', type: 'button', 'data-bookmark': bookmarkKey(site, item.id),
        onclick: event => { event.stopPropagation(); toggleBookmark(site, item); }},
        el('i', {class: 'fa-regular fa-bookmark fa-fw', 'aria-hidden': 'true'}), wide ? el('span', {text: 'Bookmark'}) : null);
    paintBookmark(button, isBookmarked(site, item.id));
    return button;
}

const bmView = () => (library.bmView ||= {q: '', site: '', sort: 'new'});

function bookmarkGrid() {
    const view = bmView();
    const q = view.q.toLowerCase();
    let list = Object.values(lib().bookmarks).filter(b => b?.item && (!view.site || b.site === view.site));
    if (q) list = list.filter(b => `${b.item.name} ${b.item.creator || ''} ${b.item.tagline || ''} ${(b.item.tags || []).join(' ')}`.toLowerCase().includes(q));
    list.sort(view.sort === 'old' ? (a, b) => a.at - b.at : view.sort === 'name' ? (a, b) => a.item.name.localeCompare(b.item.name) : (a, b) => b.at - a.at);
    const total = bookmarkCount();
    const screened = screenItems('bookmarks', list, {bookmarks: true});
    list = screened.items;
    if (!total) return [el('div', {class: 'rad-empty'}, el('i', {class: 'fa-regular fa-bookmark', 'aria-hidden': 'true'}),
        el('strong', {text: 'No bookmarks yet'}), el('span', {class: 'rad-muted', text: 'Use the bookmark button on any Chub or Bot Booru card or lorebook to keep it here.'}))];
    return [el('p', {class: 'rad-muted', text: list.length === total ? `${total} bookmarked` : `${list.length} of ${total} bookmarked`}),
        hiddenNote(screened.hidden, library.showBlocked ? list.filter(b => b.blocked).length : 0),
        list.length ? el('div', {class: 'rad-grid'}, ...list.map(b => cardTile(b.site, b.blocked ? {...b.item, blocked: b.blocked} : b.item, 'bookmarks'))) : el('p', {class: 'rad-muted', text: 'No bookmarks match.'})].filter(Boolean);
}

function bookmarksPane() {
    const view = bmView();
    const state = library.results.bookmarks ||= {q: '', items: [], page: 0, more: false, total: 0};
    const redraw = () => drawResults('bookmarks');
    const search = el('input', {class: 'text_pole', type: 'search', value: view.q, placeholder: 'Search bookmarks: name, creator, tags',
        oninput: event => { view.q = event.target.value.trim(); if (state.detail) state.detail = null; redraw(); }});
    const pane = el('div', {class: 'rad-pane'},
        el('div', {class: 'rad-searchbar'}, search,
            select([['', 'All sites'], ...SITES.filter(x => x.sorts).map(x => [x.id, x.full || x.label])], view.site, value => { view.site = value; state.detail = null; redraw(); }),
            select([['new', 'Newest first'], ['old', 'Oldest first'], ['name', 'Name A–Z']], view.sort, value => { view.sort = value; state.detail = null; redraw(); })),
        el('div', {class: 'rad-results', 'data-site': 'bookmarks'}));
    queueMicrotask(redraw);
    return pane;
}

// --------------------------------------------------------------- card details --
// A card opens in the Library itself with everything in it (notes, description, personality, scenario, greetings,
// examples, prompts, lorebook, stats), with Import / Open chat and a link to the site. The results stay behind it.

// Moving inside the Library (another source, a card opened or closed) is announced as 'rocsy-library-nav', so
// the Rocsy layout can put it in the browser history; libraryRestore() puts the Library back to such a step.
let navQuiet = false;
const seenCards = new Map();   // "site:id" -> {site, item}: cards opened this session, so Forward can reopen them
const libraryNav = () => { if (!navQuiet) document.dispatchEvent(new CustomEvent('rocsy-library-nav')); };
function libraryState() {
    const view = lib().site;
    const detail = library.results[view]?.detail;
    const page = !infinite() && view !== 'bookmarks' && view !== 'datacat' ? library.results[view]?.page || 0 : 0;
    const creator = creatorSearchable(view) ? filtersOf(view).creator || '' : '';
    return {site: view, detail: detail ? `${detail.site}:${detail.item.id}` : '', page: page > 1 ? page : 0, creator};
}
function libraryRestore(step) {
    if (!step || !library.root) return;
    navQuiet = true;
    try {
        if (step.site && lib().site !== step.site && (step.site === 'bookmarks' || SITES.some(x => x.id === step.site))) {
            lib().site = step.site; save(); renderLibraryTab(library.root.parentElement);
        }
        const view = lib().site;
        const results = library.results[view];
        const wanted = step.page || 1;
        if (creatorSearchable(view) && (filtersOf(view).creator || '') !== (step.creator || '')) {   // into / out of a creator search
            setCreator(view, step.creator || '');
            renderLibraryTab(library.root.parentElement, true);
            search(view, {page: wanted, quiet: true});
            const card = step.detail && seenCards.get(step.detail);
            if (card) openDetails(card.site, card.item, view);
            return;
        }
        if (!infinite() && results?.page && results.page !== wanted && !step.detail) { results.detail = null; search(view, {page: wanted, quiet: true}); return; }
        const open = library.results[view]?.detail;
        const openKey = open ? `${open.site}:${open.item.id}` : '';
        if (openKey === (step.detail || '')) return;
        const card = step.detail && seenCards.get(step.detail);
        if (card) openDetails(card.site, card.item, view); else closeDetails(view);
    } finally { navQuiet = false; }
}
/** The Library's start: results instead of a card, accounts folded, scrolled to the top. */
function libraryHome() {
    for (const state of Object.values(library.results)) if (state) { state.detail = null; state.scroll = 0; }
    if (!library.root) return;
    library.showAccounts = false;
    navQuiet = true;
    try { renderLibraryTab(library.root.parentElement, true); } finally { navQuiet = false; }
    const pane = library.root?.querySelector('.rad-library > .rad-pane');
    if (pane) pane.scrollTop = 0;
    if (library.root?.parentElement) library.root.parentElement.scrollTop = 0;
}

function openDetails(site, item, view = site) {
    const state = library.results[view] ||= {q: '', items: [], page: 0, more: false, total: 0};
    seenCards.set(`${site}:${item.id}`, {site, item});
    state.detail = {item, site, data: null, error: ''};
    state.scroll = library.root?.querySelector('.rad-library > .rad-pane')?.scrollTop || 0;
    drawResults(view);
    const load = api(`/library/details?site=${site}&id=${encodeURIComponent(item.id)}`);
    load.then(data => { if (state.detail?.item === item) { state.detail.data = data; drawResults(view); } })
        .catch(error => { if (state.detail?.item === item) { state.detail.error = error.message; drawResults(view); } });
    library.root?.parentElement && (library.root.parentElement.scrollTop = 0);
    libraryNav();
}

function closeDetails(view) {
    const state = library.results[view];
    if (!state?.detail) return;
    state.detail = null;
    drawResults(view);
    const pane = library.root?.querySelector('.rad-library > .rad-pane');
    if (pane) pane.scrollTop = state.scroll || 0;
    libraryNav();
}

// Card text is shown formatted: markdown and the HTML creators write, cleaned by a private DOMPurify (no scripts,
// styles, forms, frames or SVG; links and pictures only over http(s); pictures load lazily without a referrer).
let purifier = null;
let purifyImages = true;
function formatCardText(value, {images = true} = {}) {
    const DOMPurify = globalThis.DOMPurify, showdown = globalThis.showdown;
    if (!DOMPurify || !showdown) return null;
    if (!purifier) {
        purifier = DOMPurify(window);
        purifier.addHook('afterSanitizeAttributes', node => {
            if (node.tagName === 'A') {
                const href = node.getAttribute('href') || '';
                if (!/^(https?:|mailto:)/i.test(href)) node.removeAttribute('href');
                node.setAttribute('target', '_blank');
                node.setAttribute('rel', 'noopener noreferrer nofollow');
            }
            if (node.tagName === 'IMG') {
                const src = node.getAttribute('src') || '';
                if (!purifyImages || !/^https?:\/\//i.test(src)) { node.remove(); return; }
                node.removeAttribute('srcset');
                node.setAttribute('loading', 'lazy');
                node.setAttribute('decoding', 'async');
                node.setAttribute('referrerpolicy', 'no-referrer');
                node.setAttribute('class', 'rad-fimg');
            }
        });
    }
    const converter = new showdown.Converter({simpleLineBreaks: true, strikethrough: true, tables: true, simplifiedAutoLink: true,
        openLinksInNewWindow: true, emoji: false, underline: true, ghCodeBlocks: true});
    const html = converter.makeHtml(String(value || ''));
    purifyImages = images;
    return purifier.sanitize(html, {
        FORBID_TAGS: ['style', 'script', 'iframe', 'frame', 'object', 'embed', 'form', 'input', 'button', 'textarea', 'select', 'svg', 'math', 'video', 'audio', 'source', 'link', 'meta', 'base'],
        FORBID_ATTR: ['style', 'class', 'id', 'srcset', 'onerror', 'onload'],
        ALLOW_DATA_ATTR: false,
    });
}

function cardText(value, fill, formatted) {
    const box = el('div', {class: `rad-dtext${formatted ? ' rad-formatted' : ''}`});
    const html = formatted ? formatCardText(fill(value)) : null;
    if (html !== null) box.innerHTML = html; else box.textContent = fill(value);
    return box;
}

/** Long text folds behind "Show all". */
function foldable(box) {
    requestAnimationFrame(() => {
        if (box.scrollHeight <= 520) return;
        box.classList.add('rad-folded');
        const more = el('button', {class: 'rad-showmore', type: 'button', text: 'Show all', onclick: () => {
            const folded = box.classList.toggle('rad-folded');
            more.textContent = folded ? 'Show all' : 'Show less';
        }});
        box.after(more);
    });
    return box;
}

function statTiles(stats) {
    return el('div', {class: 'rad-stats'}, ...stats.map(([label, value, iconName, hint]) => el('div', {class: 'rad-stat', title: hint || label},
        icon(iconName || 'fa-chart-simple'),
        el('strong', {text: typeof value === 'number' ? compact(value) : String(value)}),
        el('small', {text: hint && !/^Lower/.test(hint) ? hint : label}))));
}

/** Comments card for a character imported from Chub or Bot Booru (overhaul character page side column), else null. */
function characterComments(avatar) {
    const imported = lib()?.imported || {};
    const key = Object.keys(imported).find(k => imported[k] === avatar && /^(chub|booru):/.test(k));
    if (!key) return null;
    const site = key.slice(0, key.indexOf(':')), id = key.slice(key.indexOf(':') + 1);
    const note = el('small', {class: 'rad-muted', text: 'Loading comments…'});
    const holder = el('section', {class: 'rad-dcard rvo-dcard'}, el('h4', {}, icon('fa-comments'), el('span', {text: 'Comments'})), note);
    const ready = site === 'booru' ? Promise.resolve({comments: {supported: true, id}}) : api(`/library/details?site=chub&id=${encodeURIComponent(id)}`);
    ready.then(data => { const box = commentsBox(site, {id, url: data?.url || ''}, data); box.classList.add('rvo-dcard'); holder.replaceWith(box); })
        .catch(error => { note.className = 'rad-error'; note.textContent = error.message; });
    return holder;
}

function commentsBox(site, item, data) {
    const box = el('section', {class: 'rad-dcard'});
    if (!data?.comments?.supported) {
        box.append(el('h4', {}, icon('fa-comments'), el('span', {text: 'Comments'})),
            el('a', {class: 'rad-sidelink', href: data?.comments?.url || item.url, target: '_blank', rel: 'noopener noreferrer'},
                el('span', {text: `Read the comments on ${SITES.find(x => x.id === site)?.label || 'the site'}`}), icon('fa-arrow-up-right-from-square')));
        return box;
    }
    const list = el('div', {class: 'rad-comments'}, el('small', {class: 'rad-muted', text: 'Loading comments…'}));
    const title = el('span', {text: 'Comments'});
    box.append(el('h4', {}, icon('fa-comments'), title), list);
    let offset = 0;
    const load = () => api(`/library/comments?site=${site}&id=${encodeURIComponent(data.comments.id || item.id)}&offset=${offset}`).then(res => {
        if (!offset) list.replaceChildren();
        list.querySelector('.rad-showmore')?.remove();
        title.textContent = `Comments (${res.total})`;
        if (!res.total) list.append(el('small', {class: 'rad-muted', text: 'No comments yet.'}));
        const comment = (c, reply) => {
            const when = Date.parse(c.at);
            return el('div', {class: reply ? 'rad-comment rad-reply' : 'rad-comment'},
                el('div', {class: 'rad-comment-head'}, el('strong', {text: c.author}),
                    c.rating ? el('small', {class: 'rad-stars', title: `Rated ${c.rating} of 5`, text: '★'.repeat(c.rating) + '☆'.repeat(Math.max(0, 5 - c.rating))}) : null,
                    when ? el('small', {class: 'rad-muted', text: new Date(when).toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'})}) : null,
                    c.score ? el('small', {class: 'rad-muted', text: `▲ ${c.score}`}) : null),
                c.text ? cardText(c.text, x => x, true) : null,
                c.replies?.length ? el('div', {class: 'rad-replies'}, ...c.replies.map(r => comment(r, true))) : null);
        };
        for (const c of res.comments) list.append(comment(c));
        offset += res.comments.length;
        if (offset < res.total) list.append(el('button', {class: 'rad-showmore', type: 'button', text: 'More comments', onclick: load}));
    }).catch(error => list.replaceChildren(el('small', {class: 'rad-error', text: error.message})));
    load();
    return box;
}

function detailsView(site, detail, view = site) {
    const {item, data, error} = detail;
    const name = data?.name || item.name;
    const formatted = lib().formatted !== false;
    const fill = value => String(value || '').replace(/\{\{char\}\}/gi, name).replace(/\{\{user\}\}/gi, ctx().name1 || 'you');
    const when = value => { const time = Date.parse(value); return time ? new Date(time).toLocaleDateString(undefined, {year: 'numeric', month: 'short', day: 'numeric'}) : ''; };
    const section = (title, value, iconName, open = true) => value ? el('details', {class: 'rad-dsection', open},
        el('summary', {}, icon(iconName), el('span', {text: title})), foldable(cardText(value, fill, formatted))) : null;
    const site_ = SITES.find(s => s.id === site);
    const lore = isLore(site);
    const image = data?.image || item.thumb;
    const toggle = el('button', {class: 'menu_button rad-fmt', type: 'button', title: formatted ? 'Show the raw text' : 'Show formatted text',
        onclick: () => { lib().formatted = !formatted; save(); drawResults(view); }}, icon(formatted ? 'fa-code' : 'fa-wand-magic-sparkles'), el('span', {text: formatted ? 'Raw text' : 'Formatted'}));
    const hero = el('div', {class: 'rad-dhero2'},
        image ? el('div', {class: 'rad-dhero-bg', style: `background-image: url("${String(image).replace(/["\\]/g, '')}")`}) : null,
        el('div', {class: 'rad-dpic'}, image ? el('img', {src: image, alt: '', referrerpolicy: 'no-referrer'}) : icon(lore ? 'fa-book-atlas' : 'fa-user'),
            (data?.nsfw ?? item.nsfw) ? el('span', {class: 'rad-badge', text: 'NSFW'}) : null),
        el('div', {class: 'rad-dhead'},
            el('small', {class: 'rad-dsite', text: siteLabel(site)}),
            el('h2', {text: name}),
            data?.creator || item.creator ? el('div', {class: 'rad-dcreator'}, icon('fa-pen-nib'), el('span', {}, ...creatorLinks(site, data?.creator || item.creator, {prefix: 'by '}))) : null,
            (data?.tagline || item.tagline) ? el('p', {class: 'rad-dtagline', text: data?.tagline || item.tagline}) : null,
            (data?.tags || item.tags)?.length ? el('div', {class: 'rad-tags'}, ...(data?.tags || item.tags).map(tag => tagButton(site, tag))) : null,
            el('div', {class: 'rad-actions'}, cardButton(site, item), bookmarkButton(site, data ? {...item, tagline: item.tagline || data.tagline} : item, {wide: true}),
                el('a', {class: 'menu_button rad-linkbtn', href: item.url, target: '_blank', rel: 'noopener noreferrer'}, icon('fa-arrow-up-right-from-square'), el('span', {text: `Open on ${site_?.label || 'site'}`}))),
            data?.note ? el('p', {class: 'rad-muted', text: data.note}) : null));
    const main = el('div', {class: 'rad-dmain'},
        error ? el('p', {class: 'rad-error', text: error}) : null,
        !data && !error ? el('p', {class: 'rad-muted'}, icon('fa-spinner fa-spin'), el('span', {text: lore ? ' Loading the lorebook…' : ' Loading the card…'})) : null,
        ...(data ? [
            section(lore ? 'About this lorebook' : 'Creator’s notes', data.creatorNotes, 'fa-note-sticky'),
            section('Description', data.description, 'fa-user'),
            section('Personality', data.personality, 'fa-masks-theater'),
            section('Scenario', data.scenario, 'fa-map'),
            section('First message', data.firstMessage, 'fa-comment-dots'),
            data.alternateGreetings?.length ? el('details', {class: 'rad-dsection'},
                el('summary', {}, icon('fa-comments'), el('span', {text: `Alternate greetings (${data.alternateGreetings.length})`})),
                ...data.alternateGreetings.map((greeting, i) => el('div', {class: 'rad-dgreeting'}, el('strong', {text: `#${i + 2}`}), foldable(cardText(greeting, fill, formatted))))) : null,
            section('Example dialogue', data.exampleDialogs, 'fa-quote-left', false),
            section('System prompt', data.systemPrompt, 'fa-terminal', false),
            section('Post-history instructions', data.postHistory, 'fa-terminal', false),
            data.lorebook ? el('details', {class: 'rad-dsection', open: lore || undefined},
                el('summary', {}, icon('fa-book'), el('span', {text: lore ? `Entries (${data.lorebook.entries.length}${data.lorebook.entries.length >= 200 ? '+, first 200 shown' : ''})` : `Lorebook${data.lorebook.name ? `: ${data.lorebook.name}` : ''} (${data.lorebook.entries.length} entries)`})),
                ...data.lorebook.entries.map(entry => el('div', {class: 'rad-dgreeting'},
                    el('strong', {text: entry.comment || entry.keys.slice(0, 4).join(', ') || 'Entry'}),
                    entry.keys.length ? el('small', {class: 'rad-muted', text: `Keys: ${entry.keys.join(', ')}`}) : null,
                    foldable(cardText(entry.content, fill, formatted))))) : null,
        ] : []));
    const side = el('aside', {class: 'rad-dside'},
        data ? el('section', {class: 'rad-dcard'}, el('h4', {}, icon('fa-chart-simple'), el('span', {text: 'Stats'})),
            statTiles([...(data.tokens ? [['Tokens', data.tokens, 'fa-coins']] : []), ...data.stats])) : null,
        data ? el('section', {class: 'rad-dcard'}, el('h4', {}, icon('fa-circle-info'), el('span', {text: 'About'})),
            el('dl', {class: 'rad-facts'},
                ...[['Source', site_?.label], ['Creator', data.creator], ['Created', when(data.created)], ['Updated', when(data.updated)],
                    ...(lore ? [['Imports into', 'World Info (Lorebooks)'], ['Original', data.source]]
                        : [['Greetings', 1 + (data.alternateGreetings || []).length], ['Lorebook', data.lorebook ? `${data.lorebook.entries.length} entries` : 'none']])]
                    .filter(([, v]) => v).flatMap(([k, v]) => [el('dt', {text: k}), k === 'Creator' ? el('dd', {}, ...creatorLinks(site, v)) : el('dd', {text: String(v)})]))) : null,
        data ? commentsBox(site, item, data) : null);
    return el('div', {class: 'rad-details2'},
        el('div', {class: 'rad-dtop'},
            el('button', {class: 'menu_button', type: 'button', onclick: () => closeDetails(view)}, icon('fa-arrow-left'), el('span', {text: view === 'bookmarks' ? 'Back to bookmarks' : 'Back to results'})),
            toggle),
        hero,
        el('div', {class: 'rad-dcols'}, main, side));
}

// Results come as numbered pages (default) or as one endless list that loads the next batch when you scroll near
// its end (settings.library.paging = 'pages' | 'infinite'). The server serves PAGE_SIZE results per page.
const PAGE_SIZE = 24;
const infinite = () => lib().paging === 'infinite';
const lastPage = state => state.total ? Math.max(1, Math.ceil(state.total / PAGE_SIZE)) : 0;

/** search(site): a new search (page 1); {page: n}: that page; {more: true}: append the next page (endless list). */
async function search(site, {more = false, page = 0, quiet = false} = {}) {
    const state = library.results[site] ||= {q: '', items: [], page: 0, more: false, total: 0};
    if (state.loading) return;
    const target = page || (more ? state.page + 1 : 1);
    state.loading = true;
    state.loadingPage = target;
    state.error = '';
    if (!more && !page) { state.items = []; state.page = 0; state.total = 0; }
    state.detail = null;
    drawResults(site);
    try {
        const params = new URLSearchParams({site, q: state.q, sort: lib().sort[site] || '', page: String(target), nsfw: lib().nsfw ? '1' : '0',
            f: JSON.stringify(withBlacklist(site, filtersOf(site)))});
        const data = await api(`/library/search?${params}`);
        if (more) {
            const seen = new Set(state.items.map(i => i.id));
            state.items.push(...data.items.filter(i => !seen.has(i.id)));
        } else {
            state.items = data.items;
            state.first = target;          // an endless list switched on later continues from this page
        }
        state.page = target;
        state.more = data.more && data.items.length > 0;
        state.total = data.total;
        state.signedIn = data.signedIn;
        state.accountError = data.accountError || '';
        if (data.accountError) { library.accounts[site] = {connected: true, user: library.accounts[site]?.user || '', error: data.accountError}; refreshAccountViews(); }
    } catch (error) {
        state.error = error.message;
    }
    state.loading = false;
    drawResults(site);
    if (page && !more) {   // a new page starts at the top of the results
        library.root?.querySelector('.rad-results')?.scrollIntoView({block: 'start'});
        if (!quiet) libraryNav();
    }
}

/** Page numbers: 1 … 4 5 [6] 7 8 … 40, with Previous / Next and a box to jump to a page. */
function pager(site, state) {
    const last = lastPage(state);
    const current = state.loading ? state.loadingPage : state.page;
    if (!current || (last <= 1 && !state.more && current <= 1)) return null;
    const go = n => { if (n >= 1 && n !== state.page && (!last || n <= last)) search(site, {page: n}); };
    const top = last || current + (state.more ? 1 : 0);
    const numbers = new Set([1, top, current - 2, current - 1, current, current + 1, current + 2].filter(n => n >= 1 && n <= top));
    const list = [...numbers].sort((a, b) => a - b);
    const parts = [];
    list.forEach((n, i) => {
        if (i && n - list[i - 1] > 1) parts.push(el('span', {class: 'rad-pgap', text: '…'}));
        parts.push(el('button', {class: `rad-pnum${n === current ? ' on' : ''}`, type: 'button', 'aria-current': n === current ? 'page' : undefined,
            disabled: state.loading || undefined, onclick: () => go(n), text: String(n)}));
    });
    const jump = last > 7 ? el('input', {class: 'text_pole rad-pjump', type: 'number', min: '1', max: String(last), placeholder: 'Page', title: `Go to page (1–${last})`,
        enterkeyhint: 'go', onkeydown: event => { if (event.key === 'Enter') go(Math.min(last, Math.max(1, parseInt(event.target.value, 10) || 1))); }}) : null;
    return el('nav', {class: 'rad-pager', 'aria-label': 'Pages'},
        el('button', {class: 'rad-pnum rad-pstep', type: 'button', title: 'Previous page', disabled: state.loading || current <= 1 || undefined, onclick: () => go(current - 1)},
            icon('fa-chevron-left'), el('span', {text: 'Previous'})),
        ...parts,
        el('button', {class: 'rad-pnum rad-pstep', type: 'button', title: 'Next page', disabled: state.loading || !(state.more || current < last) || undefined, onclick: () => go(current + 1)},
            el('span', {text: 'Next'}), icon('fa-chevron-right')),
        jump);
}

/** Pages ⇄ endless list. */
function pagingSwitch(site) {
    const button = (mode, iconName, label, title) => el('button', {class: `rad-pmode${(mode === 'infinite') === infinite() ? ' on' : ''}`, type: 'button', title,
        'aria-pressed': String((mode === 'infinite') === infinite()),
        onclick: () => {
            if ((mode === 'infinite') === infinite()) return;
            lib().paging = mode; save();
            const state = library.results[site];
            if (mode === 'pages' && state?.items.length > PAGE_SIZE) search(site, {page: 1}); else drawResults(site);   // a long endless list starts over at page 1
        }}, icon(iconName), el('span', {text: label}));
    return el('div', {class: 'rad-pmodes', role: 'group', 'aria-label': 'How results load'},
        button('pages', 'fa-list-ol', 'Pages', 'Numbered pages'),
        button('infinite', 'fa-infinity', 'Endless', 'Keep loading more as you scroll'));
}

// The endless list loads the next page when its end comes within a screen of view.
let scrollWatch = null;
function watchEnd(site, sentinel) {
    scrollWatch?.disconnect();
    scrollWatch = new IntersectionObserver(entries => {
        const state = library.results[site];
        if (entries.some(e => e.isIntersecting) && infinite() && state?.more && !state.loading && !state.error && !state.detail) search(site, {more: true});
    }, {rootMargin: '0px 0px 900px 0px'});
    scrollWatch.observe(sentinel);
}

function drawResults(site) {
    const box = library.root?.querySelector('.rad-results');
    if (!box || box.dataset.site !== site) return;
    const state = library.results[site] || {items: []};
    if (state.detail) { box.replaceChildren(detailsView(state.detail.site || site, state.detail, site)); return; }
    if (site === 'bookmarks') { box.replaceChildren(...bookmarkGrid()); return; }
    const accountNote = state.accountError ? el('p', {class: 'rad-error', text: state.accountError}) : null;
    const hint = accountOf(site) === 'booru' && lib().nsfw && state.signedIn === false && !state.accountError && state.items.length
        ? el('p', {class: 'rad-muted', text: `Bot Booru only shows NSFW ${isLore(site) ? 'lorebooks' : 'cards'} to signed-in people: sign in under Bot Booru account (above) to see them.`}) : null;
    const endless = infinite();
    const noun = isLore(site) ? 'lorebooks' : 'cards';
    const last = lastPage(state);
    const where = endless
        ? (state.items.length ? `${state.items.length.toLocaleString()}${state.total ? ` of ${state.total.toLocaleString()}` : ''} ${noun}` : '')
        : state.page ? `${state.total ? `${state.total.toLocaleString()} ${noun} · ` : ''}page ${state.page.toLocaleString()}${last ? ` of ${last.toLocaleString()}` : ''}` : '';
    const paging = !endless && state.items.length ? pager(site, state) : null;
    const sentinel = endless && state.more ? el('div', {class: 'rad-sentinel', 'aria-hidden': 'true'}) : null;
    const turning = state.loading && !endless && state.items.length;   // keep the old page (dimmed) while the next one loads
    const screened = screenItems(site, state.items);
    box.replaceChildren(...[
        accountNote,
        el('div', {class: 'rad-resultbar'}, el('span', {class: 'rad-muted', text: where}), pagingSwitch(site)),
        hint,
        paging && last > 1 ? paging : null,
        hiddenNote(screened.hidden, library.showBlocked ? screened.items.filter(i => i.blocked).length : 0),
        screened.items.length ? el('div', {class: `rad-grid${turning ? ' rad-busy' : ''}`}, ...screened.items.map(item => cardTile(site, item))) : null,
        state.error ? el('p', {class: 'rad-error', text: state.error}) : null,
        !state.loading && !state.error && state.page && !screened.items.length ? el('p', {class: 'rad-muted', text: state.items.length ? 'Everything on this page is hidden by your blacklist.' : 'Nothing found.'}) : null,
        state.loading ? el('p', {class: 'rad-muted rad-loading'}, icon('fa-spinner fa-spin'), el('span', {text: turning ? ` Loading page ${state.loadingPage}…` : endless && state.items.length ? ' Loading more…' : ' Searching…'})) : null,
        paging ? pager(site, state) : null,
        sentinel,
        endless && state.more && !state.loading && state.error ? el('button', {class: 'menu_button rad-more', type: 'button', onclick: () => search(site, {more: true})}, icon('fa-rotate-right'), el('span', {text: 'Try again'})) : null,
        endless && !state.more && state.page > 1 && !state.loading ? el('p', {class: 'rad-muted rad-end', text: `That’s all ${state.items.length.toLocaleString()} ${noun}.`}) : null,
    ].filter(Boolean));
    if (sentinel) watchEnd(site, sentinel); else scrollWatch?.disconnect();
}

// Accounts. Rocsy never asks for a site's password: Sign in / Create account open the site's own pages in a
// popup, and for Chub and Bot Booru the sign-in is then handed to Rocsy by the "Connect to Rocsy" bookmark (it
// reads the token the site's page keeps and posts it to this Rocsy window only; Rocsy takes it only from that
// site's address) or pasted by hand. The server checks it with the site and keeps it in SillyTavern's secrets.
const ACCOUNT_SITES = {
    // chub.ai's own /login and /register routes crash when opened directly ("_M().active is undefined"): open the
    // home page, whose Log in / Sign up buttons work.
    chub: {name: 'Chub', signIn: 'https://chub.ai/', register: 'https://chub.ai/', siteHint: 'Use Log in / Sign up at the top right of chub.ai.', storage: 'URQL_TOKEN',
        origins: /^https:\/\/(www\.)?(chub\.ai|characterhub\.org)$/, why: 'Search as you: your Chub content settings and cards only signed-in people see.'},
    booru: {name: 'Bot Booru', signIn: 'https://botbooru.com/account', register: 'https://botbooru.com/account', storage: 'token',
        origins: /^https:\/\/(www\.)?botbooru\.com$/, why: 'Bot Booru shows NSFW cards only to signed-in people.'},
    datacat: {name: 'datacat.run', inFrame: true,
        note: 'datacat signs in inside its own page here and stays signed in, in this browser. It has JanitorAI’s characters with their full cards.'},
};
library.accounts = {};   // site -> {connected, user, error}

async function loadAccount(site) {
    try { library.accounts[site] = await api(`/library/account?site=${site}`); }
    catch { library.accounts[site] = {connected: false}; }
    refreshAccountViews();
}

function refreshAccountViews() {
    document.querySelectorAll('.rad-site').forEach(button => {
        const account = library.accounts[button.dataset.site];
        button.classList.toggle('rad-signed', !!account?.connected);
        button.title = account?.connected ? `${button.textContent.trim()}: signed in as ${account.user}` : (SITES.find(s => s.id === button.dataset.site)?.title || '');
    });
    document.querySelectorAll('[data-account-card]').forEach(card => card.replaceWith(accountCard(card.dataset.accountCard, {open: card.open})));
    const count = Object.values(library.accounts).filter(a => a?.connected).length;
    document.querySelectorAll('.rad-accounts-btn .rad-count').forEach(badge => { badge.textContent = count ? String(count) : ''; });
}

async function connectAccount(site, token, {confirmFirst = false} = {}) {
    const info = ACCOUNT_SITES[site];
    try {
        if (confirmFirst) {
            const look = await api('/library/account', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({site, token, check: true})});
            const question = `Connect the ${info.name} account “${look.user}” to Rocsy?`;
            const ok = ctx().callGenericPopup ? await ctx().callGenericPopup(question, ctx().POPUP_TYPE?.CONFIRM ?? 2) : confirm(question);
            if (!ok) return;
        }
        const data = await api('/library/account', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({site, token})});
        library.accounts[site] = data;
        toastr.success(`Signed in to ${info.name} as ${data.user}`, 'Library');
        refreshAccountViews();
        if (library.results[site]) search(site);
    } catch (error) {
        toastr.error(error.message, `${info.name} account`);
    }
}

async function disconnectAccount(site) {
    await api('/library/account', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({site, token: ''})}).catch(() => {});
    library.accounts[site] = {connected: false};
    refreshAccountViews();
    if (library.results[site]) search(site);
}

/** The "Connect to Rocsy" bookmarklet: run on a signed-in Chub or Bot Booru tab, it hands the sign-in to this
 *  Rocsy (postMessage to the window that opened the tab, only to this origin; else it opens Rocsy with it). */
function bookmarklet() {
    const origin = location.origin, page = location.origin + location.pathname;
    const code = `(()=>{const h=location.hostname;let s='',t='';`
        + `if(/(^|\\.)(chub\\.ai|characterhub\\.org)$/.test(h)){s='chub';t=localStorage.getItem('URQL_TOKEN')||''}`
        + `else if(/(^|\\.)botbooru\\.com$/.test(h)){s='booru';t=localStorage.getItem('token')||''}`
        + `if(!s){alert('Use this on chub.ai or botbooru.com after signing in.');return}`
        + `if(t.length<8){alert('Sign in on this site first, then click Connect to Rocsy again.');return}`
        + `const m={type:'rocsy-connect',site:s,token:t};`
        + `if(window.opener&&!window.opener.closed){try{window.opener.postMessage(m,${JSON.stringify(origin)});alert('Sent to Rocsy. You can close this tab.');return}catch(e){}}`
        + `window.open(${JSON.stringify(page)}+'#rocsy-connect='+s+':'+encodeURIComponent(t),'_blank')})()`;
    return `javascript:${encodeURIComponent(code)}`;
}

function onConnectMessage(event) {
    const data = event.data;
    if (!data || data.type !== 'rocsy-connect' || typeof data.token !== 'string') return;
    const info = ACCOUNT_SITES[data.site];
    if (!info?.origins?.test(event.origin)) return;   // only from that site's own pages
    connectAccount(data.site, data.token);
}

/** Opened by the bookmarklet as a new Rocsy tab (#rocsy-connect=site:token): confirm the account, then save. */
function connectFromHash() {
    const match = location.hash.match(/^#rocsy-connect=(chub|booru):(.+)$/);
    if (!match) return;
    history.replaceState(null, '', location.pathname + location.search);
    connectAccount(match[1], decodeURIComponent(match[2]), {confirmFirst: true});
}

/** The site in a normal tab (not a small popup: some sites' pages break in one). The tab keeps its link to this
 *  page, which the Connect to Rocsy bookmark uses. */
function popup(url, name) {
    const win = window.open(url, `rocsy-${name}`);
    if (!win) toastr.warning('The browser blocked the new tab; allow pop-ups for Rocsy and try again.');
}

function accountCard(site, {open = false} = {}) {
    const info = ACCOUNT_SITES[site];
    if (!info) return null;
    const account = library.accounts[site];
    const connected = !!account?.connected;
    const card = el('details', {class: `rad-account${connected ? ' rad-connected' : ''}`, 'data-account-card': site, open: open || undefined});
    const state = !info.storage ? '' : connected ? `Signed in as ${account.user || 'you'}` : account ? 'Not connected' : 'Checking…';
    card.append(el('summary', {}, icon(connected ? 'fa-circle-check' : 'fa-user'), el('strong', {text: info.name}), el('span', {class: 'rad-account-status', text: state})));
    const body = el('div', {class: 'rad-account-body'});
    card.append(body);
    if (info.inFrame) {
        const go = path => { lib().site = 'datacat'; save(); library.datacatPath = path; renderLibraryTab(library.root.parentElement, true); };
        body.append(el('div', {class: 'rad-actions'},
            el('button', {class: 'menu_button', type: 'button', onclick: () => go('/login')}, icon('fa-right-to-bracket'), el('span', {text: 'Sign in'})),
            el('button', {class: 'menu_button', type: 'button', onclick: () => go('/register')}, icon('fa-user-plus'), el('span', {text: 'Create account'}))),
            el('small', {class: 'rad-muted', text: info.note}));
        return card;
    }
    const signButtons = el('div', {class: 'rad-actions'},
        el('button', {class: 'menu_button', type: 'button', onclick: () => popup(info.signIn, `${site}-signin`)}, icon('fa-right-to-bracket'), el('span', {text: 'Sign in'})),
        el('button', {class: 'menu_button', type: 'button', onclick: () => popup(info.register, `${site}-register`)}, icon('fa-user-plus'), el('span', {text: 'Create account'})));
    if (!info.storage) { body.append(signButtons, el('small', {class: 'rad-muted', text: info.note})); return card; }
    if (connected) {
        body.append(el('div', {class: 'rad-actions'},
            el('button', {class: 'menu_button', type: 'button', onclick: () => loadAccount(site)}, icon('fa-rotate'), el('span', {text: 'Check again'})),
            el('button', {class: 'menu_button', type: 'button', onclick: () => disconnectAccount(site)}, icon('fa-right-from-bracket'), el('span', {text: 'Disconnect'}))),
            account.error ? el('small', {class: 'rad-error', text: account.error}) : el('small', {class: 'rad-muted', text: `${info.why} The sign-in is kept in SillyTavern’s secrets on the server.`}));
        return card;
    }
    const mark = el('a', {class: 'menu_button rad-bookmarklet', href: bookmarklet(), draggable: 'true', title: 'Drag me to your bookmarks bar',
        onclick: event => { event.preventDefault(); toastr.info('Drag “Connect to Rocsy” to your bookmarks bar, then click it on the site where you’re signed in.'); }},
        icon('fa-bookmark'), el('span', {text: 'Connect to Rocsy'}));
    const paste = el('input', {class: 'text_pole', type: 'password', autocomplete: 'off', placeholder: `Paste your ${info.name} sign-in token`});
    body.append(
        el('small', {class: 'rad-muted', text: info.why}),
        el('ol', {class: 'rad-steps'},
            el('li', {}, el('span', {text: `Sign in to ${info.name} (or create an account) in the tab that opens.${info.siteHint ? ` ${info.siteHint}` : ''}`}), signButtons),
            el('li', {}, el('span', {text: 'Drag this button to your bookmarks bar (once):'}), el('div', {class: 'rad-actions'}, mark)),
            el('li', {}, el('span', {text: `Signed in, click the “Connect to Rocsy” bookmark on that ${info.name} tab. Rocsy checks the sign-in and shows your name here.`}))),
        el('details', {class: 'rad-paste'},
            el('summary', {text: 'No bookmarks bar (phone)? Paste the sign-in instead'}),
            el('small', {class: 'rad-muted', text: `On ${info.name}, open the browser console (F12 → Console), run localStorage.getItem("${info.storage}") and paste what it prints.`}),
            el('div', {class: 'rad-inline'}, paste,
                el('button', {class: 'menu_button', type: 'button', onclick: () => paste.value.trim() && connectAccount(site, paste.value.trim().replace(/^"|"$/g, ''))}, icon('fa-link'), el('span', {text: 'Connect'})))));
    return card;
}

/** All sites' accounts in one place (the Library's Accounts button). */
function accountsOverview() {
    return el('div', {class: 'rad-accounts'},
        el('div', {class: 'rad-accounts-head'}, el('strong', {text: 'Accounts'}),
            el('small', {class: 'rad-muted', text: 'Sign in to the card sites and connect them, so the Library searches as you. Rocsy never asks for their passwords.'})),
        ...Object.keys(ACCOUNT_SITES).map(site => accountCard(site, {open: !library.accounts[site]?.connected && !!ACCOUNT_SITES[site].storage})));
}

const accountPanel = site => accountCard(site);

// Each site is searched its own way (filters are remembered per site in settings.library.filters):
//   Chub      - text, tags to include / exclude, its sort list (+ ascending), token range, age, has-lorebook & co.
//   Bot Booru - text, tags (space separated, -tag excludes), its sorts with a day/week/month/all window, curated
//               order, token range; popular tags are suggested.
const filtersOf = site => (lib().filters[site] ||= {});
const tagList = value => String(value || '').split(/[,\n]+/).map(t => t.trim()).filter(Boolean);

// ------------------------------------------------------------ search by creator --
// A card's creator ("artist") is a link: it searches that site for everything they made (Chub: username;
// Bot Booru cards: their Writer tag; Bot Booru lorebooks: the uploader, filtered by the server). Bot Booru cards
// can list several writers, each its own link.
const creatorSearchable = site => SITES.some(s => s.id === site && s.sorts);
const creatorNames = (site, value) => (site === 'booru' ? String(value || '').split(/\s*,\s*/) : [String(value || '')]).map(n => n.trim()).filter(Boolean);

function searchByCreator(site, name) {
    name = String(name || '').trim();
    if (!name || !creatorSearchable(site)) return;
    setCreator(site, name);
    const state = library.results[site];
    state.detail = null;
    if (library.results.bookmarks) library.results.bookmarks.detail = null;
    lib().site = site;
    lib().kind = kindOf(site);
    save();
    search(site, {quiet: true});
    if (library.root?.parentElement) renderLibraryTab(library.root.parentElement, true);
    library.root?.parentElement && (library.root.parentElement.scrollTop = 0);
    libraryNav();
}

/** Into a creator search (text and tags put aside, everything they made) or back out of it (restored). */
function setCreator(site, name) {
    const f = filtersOf(site);
    const state = library.results[site] ||= {q: '', items: [], page: 0, more: false, total: 0};
    library.creatorPrev ||= {};
    if (name) {
        if (!f.creator) library.creatorPrev[site] = {q: state.q, tags: f.tags};
        f.creator = name;
        delete f.tags;
        state.q = '';
    } else {
        delete f.creator;
        const prev = library.creatorPrev[site];
        if (prev) { state.q = prev.q || ''; if (prev.tags) f.tags = prev.tags; delete library.creatorPrev[site]; }
    }
    save();
}

function clearCreator(site) {
    setCreator(site, '');
    search(site, {quiet: true});
    if (library.root?.parentElement) renderLibraryTab(library.root.parentElement, true);
    libraryNav();
}

/** The creator(s) as links; plain text where the site can't be searched by creator. */
function creatorLinks(site, value, {prefix = ''} = {}) {
    const names = creatorNames(site, value);
    if (!names.length) return [];
    if (!creatorSearchable(site)) return [el('span', {text: `${prefix}${names.join(', ')}`})];
    const out = prefix ? [el('span', {text: prefix})] : [];
    names.forEach((name, i) => {
        if (i) out.push(el('span', {text: ', '}));
        out.push(el('button', {class: 'rad-creatorlink', type: 'button', text: name, title: `Search ${siteLabel(site)} for everything by ${name}`,
            onclick: event => { event.stopPropagation(); searchByCreator(site, name); }}));
    });
    return out;
}

// -------------------------------------------------------------- library settings --
// The gear in the Library's site row opens its settings: a global blacklist (tags, keywords, creators) that hides
// matching cards and lorebooks on every site, plus how results look. New users start from DEFAULT_BLACKLIST.
// Hidden cards can still be shown for one look ("Show them"). datacat.run is the site's own page, so it isn't filtered.

/** The starting blacklist for new users: content most people never want to see while browsing. */
const DEFAULT_BLACKLIST = {
    tags: ['loli', 'lolicon', 'shota', 'shotacon', 'underage', 'minor', 'child', 'cub', 'nsfl', 'gore', 'guro', 'snuff', 'scat', 'necrophilia', 'bestiality'],
    words: ['loli', 'shota', 'underage'],
    creators: [],
};
const BLACKLIST_OPTIONS = {bookmarks: true, server: true};
const freshBlacklist = () => ({...structuredClone(DEFAULT_BLACKLIST), ...BLACKLIST_OPTIONS});

const blacklist = () => lib().blacklist;
const normTag = value => String(value || '').toLowerCase().replace(/^#/, '').replace(/[_\s]+/g, ' ').trim();
const escapeRe = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

let blockCache = null;
function blockRules() {
    const b = blacklist();
    const key = JSON.stringify([b.tags, b.words, b.creators]);
    if (blockCache?.key === key) return blockCache;
    const words = b.words.map(w => w.trim()).filter(Boolean).map(w => escapeRe(w).replace(/\s+/g, '[\\s_-]+'));
    return (blockCache = {
        key,
        tags: new Set(b.tags.map(normTag).filter(Boolean)),
        // whole words only: "loli" hides "loli" and "Loli-chan", not "lollipop"
        words: words.length ? new RegExp(`(?:^|[^\\p{L}\\p{N}])(${words.join('|')})(?=$|[^\\p{L}\\p{N}])`, 'iu') : null,
        creators: new Set(b.creators.map(normTag).filter(Boolean)),
    });
}

/** Why the blacklist hides this card ('' when it doesn't). */
function blockedReason(item) {
    const rules = blockRules();
    const tag = (item.tags || []).find(t => rules.tags.has(normTag(t)));
    if (tag) return `tag “${tag}”`;
    const creator = String(item.creator || '').split(/\s*,\s*/).find(c => rules.creators.has(normTag(c)));
    if (creator) return `creator “${creator}”`;
    const word = rules.words && `${item.name || ''} \n ${item.tagline || ''} \n ${(item.tags || []).join(' \n ')}`.match(rules.words);
    if (word) return `keyword “${word[1]}”`;
    return '';
}

const alreadyImported = (site, item) => !!(isLore(site) ? importedBook(site, item.id) : importedAvatar(site, item.id));

/** Split a result list into what's shown and how many the settings hide. */
function screenItems(site, items, {bookmarks = false} = {}) {
    if (bookmarks && !blacklist().bookmarks) return {items, hidden: 0};
    const out = [];
    let hidden = 0;
    for (const entry of items) {
        const [itemSite, item] = bookmarks ? [entry.site, entry.item] : [site, entry];
        const reason = blockedReason(item) || (!bookmarks && lib().hideImported && alreadyImported(itemSite, item) ? 'already imported' : '');
        if (!reason) out.push(entry);
        else if (library.showBlocked) out.push(bookmarks ? {...entry, blocked: reason} : {...entry, blocked: reason});
        else hidden++;
    }
    return {items: out, hidden};
}

function hiddenNote(hidden, shownBlocked) {
    if (!hidden && !shownBlocked) return null;
    const toggle = el('button', {class: 'rad-textbtn', type: 'button', text: library.showBlocked ? 'Hide them again' : 'Show them',
        onclick: () => { library.showBlocked = !library.showBlocked; redrawLibraryResults(); }});
    const settingsLink = el('button', {class: 'rad-textbtn', type: 'button', text: 'Library settings', onclick: () => toggleLibrarySettings(true)});
    return el('p', {class: 'rad-muted rad-hiddennote'}, icon('fa-eye-slash'),
        el('span', {text: library.showBlocked ? ` Showing ${shownBlocked} ${shownBlocked === 1 ? 'one' : 'ones'} your settings hide. ` : ` ${hidden} hidden by your blacklist. `}),
        toggle, el('span', {text: ' · '}), settingsLink);
}

/** The blacklisted tags, sent to the sites themselves too (when allowed), so a page isn't half empty. */
function withBlacklist(site, f) {
    if (!blacklist().server || !blacklist().tags.length) return f;
    const account = accountOf(site);
    if (account === 'chub') {
        // the server splits on spaces and commas and keeps 30: one-word tags only, the user's own excludes first
        const own = tagList(f.exclude).flatMap(t => t.split(/\s+/)).filter(Boolean);
        const extra = blacklist().tags.map(t => t.trim()).filter(t => t && !/\s/.test(t));
        return {...f, exclude: [...new Set([...own, ...extra])].slice(0, 30).join(',')};
    }
    if (site === 'booru') {
        const own = String(f.tags || '').split(/\s+/).filter(Boolean);
        const extra = blacklist().tags.map(t => `-${normTag(t).replace(/\s+/g, '_')}`).filter(t => t.length > 1 && !own.includes(t));
        return {...f, tags: [...own, ...extra].slice(0, 30).join(' ')};
    }
    return f;
}

function redrawLibraryResults() {
    library.root?.querySelectorAll('.rad-results').forEach(box => drawResults(box.dataset.site));
}

/** Results fetched under the old settings: the shown site searches again, the others when next opened. */
let researchTimer = null;
function blacklistChanged({refetch = false} = {}) {
    blockCache = null;
    save();
    library.root?.classList.toggle('rad-blurnsfw', !!lib().blurNsfw);
    redrawLibraryResults();
    if (!refetch) return;
    clearTimeout(researchTimer);
    researchTimer = setTimeout(() => {
        for (const id of Object.keys(library.results)) if (id !== 'bookmarks' && id !== lib().site) delete library.results[id];
        const site = lib().site;
        if (library.results[site] && SITES.some(s => s.id === site && s.sorts)) search(site);
    }, 700);
}

/** A list edited as chips: Enter or comma adds, × or Backspace removes. */
function chipEditor(list, placeholder, onchange) {
    const box = el('div', {class: 'rad-chips'});
    const input = el('input', {class: 'rad-chipin', type: 'text', placeholder, enterkeyhint: 'done', autocomplete: 'off', 'aria-label': placeholder});
    const changed = () => {
        const focused = document.activeElement === input;
        draw();
        if (focused) input.focus();
        onchange();
    };
    const add = () => {
        const parts = input.value.split(/[,\n]+/).map(s => s.trim()).filter(Boolean);
        input.value = '';
        let added = false;
        for (const part of parts) if (!list.some(x => normTag(x) === normTag(part))) { list.push(part); added = true; }
        if (added) changed();
    };
    input.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ',') { event.preventDefault(); add(); }
        else if (event.key === 'Backspace' && !input.value && list.length) { list.pop(); changed(); }
    });
    input.addEventListener('blur', add);
    input.addEventListener('paste', () => setTimeout(() => { if (/[,\n]/.test(input.value)) add(); }));
    const draw = () => box.replaceChildren(...list.map((value, i) => el('span', {class: 'rad-chip'},
        el('span', {text: value}),
        el('button', {class: 'rad-chipx', type: 'button', title: `Remove “${value}”`, 'aria-label': `Remove ${value}`,
            onclick: () => { list.splice(i, 1); changed(); input.focus(); }}, icon('fa-xmark')))), input);
    box.addEventListener('click', event => { if (event.target === box) input.focus(); });
    draw();
    return box;
}

function toggleLibrarySettings(on = !library.showSettings) {
    library.showSettings = on;
    if (on) library.showAccounts = false;
    if (library.root?.parentElement) renderLibraryTab(library.root.parentElement, true);
    if (on) library.root?.querySelector('.rad-libsettings')?.scrollIntoView({block: 'nearest'});
}

function librarySettings() {
    const b = blacklist();
    const group = (title, hint, list, placeholder, refetch = false) => el('div', {class: 'rad-setgroup'},
        el('div', {class: 'rad-sethead'}, el('strong', {text: title}), el('small', {class: 'rad-muted', text: hint}),
            list.length ? el('button', {class: 'rad-textbtn rad-listclear', type: 'button', text: 'Clear', title: `Remove every ${title.toLowerCase().replace(/s$/, '')}`,
                onclick: () => { list.length = 0; blacklistChanged({refetch}); rerender(); }}) : null),
        chipEditor(list, placeholder, () => blacklistChanged({refetch})));
    const option = (label, hint, checked, onchange) => el('div', {class: 'rad-setopt'}, checkbox(label, checked, onchange), hint ? el('small', {class: 'rad-muted', text: hint}) : null);
    const rerender = () => { if (library.root?.parentElement) renderLibraryTab(library.root.parentElement, true); };
    const template = el('button', {class: 'menu_button', type: 'button', title: 'Add the default blacklist’s tags and keywords to yours',
        onclick: () => {
            for (const key of ['tags', 'words', 'creators']) for (const v of DEFAULT_BLACKLIST[key]) if (!b[key].some(x => normTag(x) === normTag(v))) b[key].push(v);
            blacklistChanged({refetch: true}); rerender(); toastr.info('Default blacklist added', 'Library');
        }}, icon('fa-wand-magic-sparkles'), el('span', {text: 'Add default blacklist'}));
    // Clear all starts an empty blacklist of your own; the toast's Undo puts the old one back.
    const clear = el('button', {class: 'menu_button rad-clearall', type: 'button', title: 'Empty the tags, keywords and creators lists to make your own blacklist',
        onclick: () => {
            if (!b.tags.length && !b.words.length && !b.creators.length) return;
            const before = {tags: [...b.tags], words: [...b.words], creators: [...b.creators]};
            b.tags.length = 0; b.words.length = 0; b.creators.length = 0;
            blacklistChanged({refetch: true}); rerender();
            const undo = toastr.info('Blacklist cleared. Click here to undo.', 'Library', {timeOut: 8000, extendedTimeOut: 4000, closeButton: true,
                onclick: () => { for (const key of ['tags', 'words', 'creators']) b[key].splice(0, b[key].length, ...before[key]); blacklistChanged({refetch: true}); rerender(); toastr.clear(undo); }});
        }}, icon('fa-eraser'), el('span', {text: 'Clear all'}));
    return el('section', {class: 'rad-libsettings', 'aria-label': 'Library settings'},
        el('div', {class: 'rad-accounts-head rad-setstitle'},
            el('strong', {}, icon('fa-gear'), el('span', {text: ' Library settings'})),
            el('button', {class: 'menu_button rad-icon', type: 'button', title: 'Close settings', 'aria-label': 'Close settings', onclick: () => toggleLibrarySettings(false)}, icon('fa-xmark'))),
        el('h4', {class: 'rad-sethtitle', text: 'Blacklist'}),
        el('small', {class: 'rad-muted', text: 'Cards and lorebooks that match anything here are hidden on every site. Not applied inside datacat.run, which is that site’s own page.'}),
        group('Tags', 'Hides anything with this tag (spaces and underscores count the same).', b.tags, 'Add a tag, press Enter', true),
        group('Keywords', 'Hides anything whose name, tagline or tags contain this whole word or phrase.', b.words, 'Add a word or phrase'),
        group('Creators', 'Hides everything by these creators.', b.creators, 'Add a creator name'),
        el('div', {class: 'rad-inline rad-setbtns'}, template, clear),
        option('Also hide blacklisted bookmarks', 'Off: your bookmarks always show everything you saved.', b.bookmarks, v => { b.bookmarks = v; blacklistChanged(); }),
        option('Ask the sites to leave out blacklisted tags', 'Chub and Bot Booru then fill each page with cards you can see. One-word tags only on Chub.', b.server, v => { b.server = v; blacklistChanged({refetch: true}); }),
        el('h4', {class: 'rad-sethtitle', text: 'Results'}),
        option('Include NSFW cards and lorebooks', '', lib().nsfw, v => { lib().nsfw = v; blacklistChanged({refetch: true}); rerender(); }),
        option('Blur NSFW pictures', 'Hover or focus a picture to see it.', lib().blurNsfw, v => { lib().blurNsfw = !!v; blacklistChanged(); }),
        option('Hide cards and lorebooks I already imported', '', lib().hideImported, v => { lib().hideImported = !!v; blacklistChanged(); }),
        el('div', {class: 'rad-setopt rad-inline'}, el('span', {text: 'Results load as'}),
            select([['pages', 'Numbered pages'], ['infinite', 'An endless list']], infinite() ? 'infinite' : 'pages', value => {
                lib().paging = value; save();
                const site = lib().site, state = library.results[site];
                if (value === 'pages' && state?.items.length > PAGE_SIZE) search(site, {page: 1}); else redrawLibraryResults();
            })));
}

/** Clicking a tag on a card filters by it, the way that site's tags work. */
function tagButton(site, tag) {
    if (site === 'booru-lore') return el('span', {class: 'rad-tagbtn', text: tag});
    return el('button', {class: 'rad-tagbtn', type: 'button', text: tag, title: `Show cards tagged “${tag}”`, onclick: () => addTagFilter(site, tag)});
}

async function addTagFilter(site, tag) {
    if (lib().site !== site) lib().site = site;   // e.g. a tag clicked in Bookmarks
    const f = filtersOf(site);
    if (site === 'chub' || site === 'chub-lore') f.tags = [...new Set([...tagList(f.tags), tag])].join(', ');
    else if (site === 'booru') f.tags = [...new Set([...String(f.tags || '').split(/\s+/).filter(Boolean), tag.replace(/\s+/g, '_').toLowerCase()])].join(' ');
    save();
    const state = library.results[site];
    if (state) state.detail = null;
    if (library.root) { library.root.dataset.site = ''; renderLibraryTab(library.root.parentElement); }
    search(site);
}

function numberBox(placeholder, value, onchange) {
    return el('input', {class: 'text_pole rad-num', type: 'number', min: '0', step: '100', placeholder, value: value ?? '',
        onchange: event => onchange(event.target.value ? String(Math.max(0, parseInt(event.target.value, 10) || 0)) : '')});
}

function checkbox(label, checked, onchange) {
    const box = el('input', {type: 'checkbox', onchange: event => onchange(event.target.checked)});
    box.checked = !!checked;
    return el('label', {class: 'checkbox_label'}, box, el('span', {text: label}));
}

function searchPane(site) {
    const info = SITES.find(s => s.id === site);
    const state = library.results[site] ||= {q: '', items: [], page: 0, more: false, total: 0};
    const f = filtersOf(site);
    const go = () => { state.q = text.value.trim(); save(); search(site); };
    const set = (key, value, now = true) => { if (value === '' || value === false || value === null) delete f[key]; else f[key] = value; save(); if (now) go(); };
    const text = el('input', {class: 'text_pole', type: 'search', value: state.q, enterkeyhint: 'search',
        placeholder: site === 'booru' ? 'Search names and text' : site === 'booru-lore' ? 'Search Bot Booru lorebooks' : site === 'chub-lore' ? 'Search Chub lorebooks' : 'Search Chub', onkeydown: event => { if (event.key === 'Enter') go(); }});
    const sort = select(info.sorts, lib().sort[site] || info.sorts[0][0], value => { lib().sort[site] = value; save(); renderLibraryTab(library.root.parentElement, true); go(); });
    const order = lib().sort[site];
    const searchButton = el('button', {class: 'menu_button', type: 'button', title: 'Search', onclick: go}, icon('fa-magnifying-glass'), el('span', {text: 'Search'}));
    const nsfw = checkbox(isLore(site) ? 'Include NSFW lorebooks' : 'Include NSFW cards', lib().nsfw, value => { lib().nsfw = value; save(); go(); });
    const bar = [], rows = [];

    if (site === 'chub' || site === 'chub-lore') {
        const asc = el('button', {class: 'menu_button rad-icon', type: 'button', title: f.asc ? 'Ascending (click for descending)' : 'Descending (click for ascending)',
            onclick: () => { set('asc', !f.asc); renderLibraryTab(library.root.parentElement, true); }}, icon(f.asc ? 'fa-arrow-up-wide-short' : 'fa-arrow-down-wide-short'));
        bar.push(text, sort, asc, searchButton);
        const include = el('input', {class: 'text_pole', type: 'text', value: f.tags || '', placeholder: 'Tags to include, comma separated',
            onchange: event => set('tags', event.target.value.trim()), onkeydown: event => { if (event.key === 'Enter') event.target.blur(); }});
        const exclude = el('input', {class: 'text_pole', type: 'text', value: f.exclude || '', placeholder: 'Tags to exclude',
            onchange: event => set('exclude', event.target.value.trim()), onkeydown: event => { if (event.key === 'Enter') event.target.blur(); }});
        rows.push(el('div', {class: 'rad-filterrow'}, el('span', {class: 'rad-flabel'}, icon('fa-tags')), include, el('span', {class: 'rad-flabel'}, icon('fa-ban')), exclude));
        const more = el('details', {class: 'rad-more-filters', open: !!(f.minTokens || f.maxTokens || f.days || Object.keys(f).some(k => ['lore', 'greetings', 'examples', 'images', 'expressions'].includes(k)))},
            el('summary', {text: 'More filters'}),
            el('div', {class: 'rad-filterrow'},
                el('span', {class: 'rad-flabel', text: 'Tokens'}), numberBox('min', f.minTokens, v => set('minTokens', v)), numberBox('max', f.maxTokens, v => set('maxTokens', v)),
                el('span', {class: 'rad-flabel', text: 'Age'}),
                select([['', 'Any time'], ['1', 'Last day'], ['7', 'Last week'], ['30', 'Last month'], ['90', 'Last 3 months'], ['365', 'Last year']], f.days || '', v => set('days', v))),
            site === 'chub-lore' ? null : el('div', {class: 'rad-filterrow'},
                checkbox('Lorebook', f.lore, v => set('lore', v)), checkbox('Alternate greetings', f.greetings, v => set('greetings', v)),
                checkbox('Example dialogues', f.examples, v => set('examples', v)), checkbox('Gallery images', f.images, v => set('images', v)),
                checkbox('Expressions', f.expressions, v => set('expressions', v))));
        rows.push(more);
    } else if (site === 'booru-lore') {
        bar.push(text, sort, searchButton);
    } else if (site === 'booru') {
        bar.push(text, sort);
        if (['downloads', 'favorites', 'views'].includes(order)) bar.push(select([['day', 'Today'], ['week', 'This week'], ['month', 'This month'], ['all', 'All time']], f.window || 'all', v => set('window', v)));
        if (order === 'curated') bar.push(select([['recent', 'Recent'], ['random', 'Random']], f.curated || 'recent', v => set('curated', v)));
        bar.push(searchButton);
        const listId = 'rad-booru-tags';
        const tags = el('input', {class: 'text_pole', type: 'text', value: f.tags || '', list: listId, placeholder: 'Tags: dragon_girl fantasy -nsfl (space separated, - excludes)',
            onchange: event => set('tags', event.target.value.trim()), onkeydown: event => { if (event.key === 'Enter') event.target.blur(); }});
        const datalist = el('datalist', {id: listId});
        api('/library/booru-tags').then(data => datalist.append(...data.tags.slice(0, 200).flatMap(t => [el('option', {value: t.name}), el('option', {value: `-${t.name}`})]))).catch(() => {});
        rows.push(el('div', {class: 'rad-filterrow'}, el('span', {class: 'rad-flabel'}, icon('fa-tags')), tags, datalist,
            el('span', {class: 'rad-flabel', text: 'Tokens'}), numberBox('min', f.minTokens, v => set('minTokens', v)), numberBox('max', f.maxTokens, v => set('maxTokens', v))));
    }
    if (f.creator) rows.unshift(el('div', {class: 'rad-creatorfilter'}, icon('fa-pen-nib'),
        el('span', {text: `Showing ${isLore(site) ? 'lorebooks' : 'cards'} by `}), el('strong', {text: f.creator}),
        el('button', {class: 'menu_button rad-icon', type: 'button', title: `Stop showing only ${f.creator}’s work`, 'aria-label': 'Clear the creator filter',
            onclick: () => clearCreator(site)}, icon('fa-xmark'))));
    const pane = el('div', {class: 'rad-pane'},
        el('div', {class: 'rad-searchbar'}, ...bar),
        ...rows,
        el('div', {class: 'rad-optrow'}, nsfw, accountPanel(accountOf(site))),
        el('div', {class: 'rad-results', 'data-site': site}));
    queueMicrotask(() => { if (!state.page && !state.loading && !state.error) search(site); else drawResults(site); });
    return pane;
}

// datacat.run's SillyTavern bridge: the page asks whether we already have a character (prepare), then sends the
// PNG it built (card); every request is answered with an ack that carries the same nonce and requestId.
function datacatPane() {
    const nonce = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const path = library.datacatPath || '/';
    library.datacatPath = '';
    const src = `https://datacat.run${path}?dc_embed=st&dc_bridge_nonce=${encodeURIComponent(nonce)}`;
    const frame = el('iframe', {class: 'rad-frame', src, title: 'datacat.run', referrerpolicy: 'no-referrer',
        allow: 'clipboard-write'});
    library.datacat = {frame, nonce, origin: ''};
    return el('div', {class: 'rad-pane rad-pane-frame'},
        el('p', {class: 'rad-muted'}, el('span', {text: 'Open a character and use its “ST Chat” button to import it. '}),
            el('a', {href: 'https://datacat.run/', target: '_blank', rel: 'noopener noreferrer', text: 'Open datacat.run in a new tab'})),
        accountPanel('datacat'),
        frame);
}

function datacatAck(requestId, status, message, extra = {}) {
    const dc = library.datacat;
    dc?.frame.contentWindow?.postMessage({type: DC.ack, nonce: dc.nonce, requestId, status, message, ...extra}, dc.origin || 'https://datacat.run');
}

async function onDatacatMessage(event) {
    const dc = library.datacat;
    const data = event.data;
    if (!dc || !data || typeof data !== 'object' || event.source !== dc.frame.contentWindow || !DATACAT_ORIGIN.test(event.origin)) return;
    if (data.type === DC.ready) {
        if (data.needsInit || !dc.origin) {
            dc.origin = event.origin;
            dc.frame.contentWindow.postMessage({type: DC.init, nonce: dc.nonce, source: DC.source}, event.origin);
        }
        return;
    }
    if (data.nonce !== dc.nonce) return;
    const meta = data.metadata || {};
    const id = String(meta.datacatCharacterId || meta.characterId || '').trim();
    const name = String(meta.characterName || 'the character');
    if (data.type === DC.prepare) {
        const avatar = id && importedAvatar('datacat', id);
        if (!avatar) return datacatAck(data.requestId, 'need-card', 'Not imported yet');
        try {
            await openCharacter(avatar, {close: false});
            datacatAck(data.requestId, 'existing', `${name} is already in SillyTavern: opened it.`);
        } catch (error) { datacatAck(data.requestId, 'error', error.message); }
        return;
    }
    if (data.type === DC.card) {
        datacatAck(data.requestId, 'processing', 'Importing the card into SillyTavern…', {phase: 'SillyTavern import', step: 'st-import'});
        try {
            if (!(data.png instanceof ArrayBuffer) || !data.png.byteLength) throw new Error('datacat sent no card.');
            const fileName = String(meta.fileName || `${name}.png`).replace(/[\\/:*?"<>|]+/g, '_');
            const avatar = await importCard(new Blob([data.png], {type: 'image/png'}), fileName, 'datacat', id || fileName);
            toastr.success(`${name} is in your characters`, 'Library');
            await openCharacter(avatar, {close: false}).catch(() => {});
            datacatAck(data.requestId, 'ok', `Imported ${name} into SillyTavern and opened it.`);
        } catch (error) {
            datacatAck(data.requestId, 'error', error.message);
        }
    }
}

/** Paste a link: Bot Booru by its id, everything else through SillyTavern's URL importer. */
async function importLink(url, button) {
    let parsed;
    try { parsed = new URL(url); } catch { return toastr.warning('That isn’t a link.'); }
    if (/datacat\.run$/.test(parsed.hostname)) return toastr.info('Open datacat cards in the datacat.run tab and use their ST Chat button.');
    const janitor = url.match(JANITOR_ID);
    if (janitor) {
        const have = importedAvatar('janitor', janitor[1].toLowerCase());
        return have ? openCharacter(have) : openInDatacat(janitor[1].toLowerCase());
    }
    button.disabled = true;
    try {
        const booruLore = parsed.hostname.endsWith('botbooru.com') && parsed.pathname.match(/\/lorebook\/(\d+)/);
        const chubLore = /chub\.ai|characterhub\.org/.test(parsed.hostname) && parsed.pathname.match(/\/lorebooks\/([^/]+\/[^/?#]+)/);
        if (booruLore || chubLore) {
            const site = booruLore ? 'booru-lore' : 'chub-lore';
            const id = booruLore ? booruLore[1] : chubLore[1];
            const existing = importedBook(site, id);
            if (existing) return await openLorebook(existing);
            const book = booruLore ? await api(`/library/lorebook?site=booru-lore&id=${id}`) : await fetchLoreViaSillyTavern(`https://chub.ai/lorebooks/${id}`);
            await importLorebook(book, book.name || id.split('/').pop(), site, id);
            return;
        }
        const booru = parsed.hostname.endsWith('botbooru.com') && parsed.pathname.match(/\/(?:character|post|download\/png)\/(\d+)/);
        const chub = parsed.pathname.match(/\/characters\/([^/]+\/[^/?#]+)/);
        const site = booru ? 'booru' : /chub\.ai|characterhub\.org/.test(parsed.hostname) && chub ? 'chub' : 'link';
        const id = booru ? booru[1] : chub ? chub[1] : url;
        const existing = importedAvatar(site, id);
        if (existing) return await openCharacter(existing);
        const {blob, fileName} = booru ? {blob: await api(`/library/card?site=booru&id=${id}`), fileName: `botbooru-${id}.png`} : await fetchViaSillyTavern(url);
        await importCard(blob, fileName, site, id);
        toastr.success('Imported', 'Library');
    } catch (error) {
        toastr.error(error.message, 'Import from link');
    } finally {
        button.disabled = false;
    }
}

/** Switch the Library between character cards and lorebooks (back to the last source used for that kind). */
function libraryKind(kind) {
    if (!KINDS.some(([id]) => id === kind)) return;
    const current = lib().site === 'bookmarks' ? lib().kind : kindOf(lib().site);
    if (current === kind && lib().site !== 'bookmarks') return;
    const last = lib().lastSite?.[kind];
    lib().site = last && SITES.some(s => s.id === last && kindOf(s.id) === kind) ? last : SITES.find(s => kindOf(s.id) === kind).id;
    lib().kind = kind;
    save();
    if (library.root?.parentElement) { renderLibraryTab(library.root.parentElement, true); libraryNav(); }
}

function renderLibraryTab(body, force = false) {
    if (force && library.root) library.root.dataset.site = '';
    // The tab keeps its DOM between renders (a chat change re-renders the dialog), so datacat's page and the
    // search results aren't reloaded; only the import buttons are refreshed.
    if (library.root && body.firstChild === library.root && library.root.dataset.site === lib().site) {
        library.root.querySelectorAll('.rad-results').forEach(box => drawResults(box.dataset.site));
        return;
    }
    const site = lib().site === 'bookmarks' ? {id: 'bookmarks'} : SITES.find(s => s.id === lib().site) || SITES[0];
    const kind = site.id === 'bookmarks' ? lib().kind || 'cards' : kindOf(site.id);
    if (site.id !== 'bookmarks') { lib().kind = kind; (lib().lastSite ||= {})[kind] = site.id; }
    const link = el('input', {class: 'text_pole', type: 'url', placeholder: 'Or paste a card or lorebook link (Bot Booru, Chub, JanitorAI, RisuRealm, Pygmalion, Perchance …)',
        onkeydown: event => { if (event.key === 'Enter') importLink(link.value.trim(), linkButton); }});
    const linkButton = el('button', {class: 'menu_button', type: 'button', onclick: () => link.value.trim() && importLink(link.value.trim(), linkButton)}, icon('fa-link'), el('span', {text: 'Import'}));
    library.root = el('div', {class: `rad-library${lib().blurNsfw ? ' rad-blurnsfw' : ''}`, 'data-site': site.id, 'data-kind': kind},
        el('div', {class: 'rad-toprow'},
            el('div', {class: 'rad-kinds', role: 'tablist', 'aria-label': 'What to find'}, ...KINDS.map(([id, label, faIcon]) => el('button', {
                class: 'rad-kind', type: 'button', role: 'tab', 'aria-selected': String(id === kind),
                onclick: () => libraryKind(id),
            }, icon(faIcon), el('span', {text: label})))),
            el('button', {class: `rad-site rad-settings-btn${library.showSettings ? ' on' : ''}`, type: 'button', title: 'Library settings: blacklist and results',
                'aria-label': 'Library settings', 'aria-pressed': String(!!library.showSettings), onclick: () => toggleLibrarySettings()},
                icon('fa-gear'), el('span', {class: 'rad-settings-label', text: 'Settings'}))),
        el('nav', {class: 'rad-sites'}, ...SITES.filter(s => kindOf(s.id) === kind).map(s => el('button', {
            class: 'rad-site', type: 'button', 'aria-pressed': String(s.id === site.id), 'data-site': s.id,
            onclick: () => { if (lib().site !== s.id) { lib().site = s.id; save(); renderLibraryTab(body); libraryNav(); } },
            title: s.title,
        }, s.icon ? icon(s.icon) : null, el('span', {text: s.label}))),
            el('button', {class: 'rad-site rad-bookmarks-btn', type: 'button', 'aria-pressed': String(site.id === 'bookmarks'), title: 'All your bookmarked cards',
                onclick: () => { if (lib().site !== 'bookmarks') { lib().site = 'bookmarks'; save(); renderLibraryTab(body); libraryNav(); } }},
                el('i', {class: `${site.id === 'bookmarks' ? 'fa-solid' : 'fa-regular'} fa-bookmark fa-fw`, 'aria-hidden': 'true'}), el('span', {text: 'Bookmarks'}),
                el('span', {class: 'rad-count', text: bookmarkCount() ? String(bookmarkCount()) : ''})),
            el('button', {class: `rad-site rad-accounts-btn${library.showAccounts ? ' on' : ''}`, type: 'button', title: 'Sign in to the card sites',
                onclick: () => { library.showAccounts = !library.showAccounts; if (library.showAccounts) library.showSettings = false; renderLibraryTab(body, true); }},
                icon('fa-user'), el('span', {text: 'Accounts'}), el('span', {class: 'rad-count'}))),
        library.showAccounts ? accountsOverview() : null,
        library.showSettings ? librarySettings() : null,
        site.id === 'datacat' ? datacatPane() : site.id === 'bookmarks' ? bookmarksPane() : searchPane(site.id),
        el('div', {class: 'rad-inline rad-linkbar'}, link, linkButton));
    if (site.id !== 'datacat') library.datacat = null;
    body.replaceChildren(library.root);
    for (const id of ['chub', 'booru']) if (!library.accounts[id]) loadAccount(id); else refreshAccountViews();
}

// External media (pictures, audio and video from other sites inside messages and cards). SillyTavern blocks it by
// default; Rocsy turns it on once (Library cards and datacat/Janitor greetings use it) and the Chats tab keeps a
// switch. It goes through SillyTavern's own checkbox, so its handler saves it and redraws the chat.
const externalMediaAllowed = () => !ctx().powerUserSettings?.forbid_external_media;
function setExternalMedia(allow) {
    const box = document.getElementById('forbid_external_media');
    if (box && globalThis.jQuery) { globalThis.jQuery(box).prop('checked', !allow).trigger('input'); return; }
    const power = ctx().powerUserSettings;
    if (!power) return;
    power.forbid_external_media = !allow;
    ctx().saveSettingsDebounced();
    ctx().reloadCurrentChat?.();
}

function renderChatsTab(body) {
    const n = naming();
    const auto = el('input', {type: 'checkbox', onchange: event => { n.auto = event.target.checked; save(); render(); }});
    auto.checked = n.auto;
    const after = el('input', {class: 'text_pole rad-number', type: 'number', min: '1', max: '50', step: '1', value: String(n.after),
        onchange: event => { n.after = Math.min(50, Math.max(1, parseInt(event.target.value, 10) || 2)); event.target.value = String(n.after); save(); }});
    const models = llamaInfo?.models || [];
    const current = ctx().getCurrentChatId?.();
    const entry = current ? getTitle(currentOwner(), current) : null;
    const media = el('input', {type: 'checkbox', onchange: event => { setExternalMedia(event.target.checked); render(); }});
    media.checked = externalMediaAllowed();
    body.replaceChildren(
        el('label', {class: 'checkbox_label rad-toggle rad-check'}, media, el('span', {},
            el('span', {text: 'Show external media'}),
            el('small', {class: 'rad-muted', text: 'Pictures, audio and video from other sites in messages and character cards (greeting images, status panels). Loading them tells those sites your IP address. Per-character exceptions stay in SillyTavern’s chat menu.'}))),
        el('label', {class: 'checkbox_label rad-toggle'}, auto, el('span', {text: 'Name chats automatically'})),
        field('Name a chat after', el('div', {class: 'rad-inline'}, after, el('span', {text: 'messages'})),
            'Counts your messages and the character’s; the greeting doesn’t. 2 = right after the first reply. If naming fails it tries once more at the next message.'),
        field('Naming model', select([['', 'Same as the chat (no model swap)'], ...models.map(m => [m.id, m.name])], n.model, value => { n.model = value; save(); }),
            'Another llama-swap model makes llama-swap unload the chat model to load it, and back.'),
        el('p', {class: 'rad-muted', text: 'Names show in the Rocsy sidebar and header. Rename a chat there (pencil, F2 or double-click); a name you give it is kept, and an empty name hands naming back. The chat file itself keeps its name.'}),
        el('div', {class: 'rad-status'}, icon('fa-tag'), el('span', {text: !current ? 'No chat open.' : entry?.title ? `This chat: “${entry.title}” (${entry.by === 'user' ? 'named by you' : 'named automatically'})` : 'This chat has no name yet.'})),
        el('div', {class: 'rad-actions'},
            el('button', {class: 'menu_button', type: 'button', disabled: !current, onclick: async event => {
                const button = event.currentTarget; button.disabled = true;
                await nameChat({force: true}); button.disabled = false; render();
            }}, icon('fa-wand-magic-sparkles'), el('span', {text: 'Name this chat now'}))));
    if (!llamaInfo) api('/llama').then(data => { llamaInfo = data; render(); }).catch(() => { llamaInfo = {url: '', models: []}; });
}

const TABS = [
    {id: 'voice', label: 'Voice', icon: 'fa-microphone-lines', render: renderVoiceTab},
    {id: 'connection', label: 'Connection', icon: 'fa-plug', render: renderConnectionTab},
    {id: 'chats', label: 'Chats', icon: 'fa-tag', render: renderChatsTab},
    {id: 'image', label: 'Image', icon: 'fa-image', render: body => body.replaceChildren(globalThis.RocsyImage?.imageSettings() || el('p', {text: 'Rocsy Image tools didn’t load.'}))},
    {id: 'library', label: 'Library', icon: 'fa-book-open', render: renderLibraryTab},
];

// The Connection tab can also live on the Rocsy layout's "Model & connection" page.
let connectionHost = null;
function mountConnection(container) {
    connectionHost = container;
    container.classList.add('rad-body', 'rad-inline-tab');
    container.dataset.tab = 'connection';
    renderConnectionTab(container);
}

function render() {
    if (connectionHost?.isConnected) renderConnectionTab(connectionHost);
    else connectionHost = null;
    if (!dialog?.open) return;
    const tab = TABS.find(t => t.id === settings.tab) || TABS[0];
    dialog.classList.toggle('rad-wide', tab.id === 'library');
    dialog.querySelectorAll('.rad-tab').forEach(button => button.setAttribute('aria-selected', String(button.dataset.tab === tab.id)));
    const body = dialog.querySelector('.rad-body');
    body.dataset.tab = tab.id;
    tab.render(body);
}

// In the Rocsy layout the Library is a page of the site (the overhaul hosts it); elsewhere it's a dialog tab.
const libraryInSite = () => document.body.classList.contains('rvo') && typeof globalThis.RocsyVisualOverhaul?.showLibrary === 'function';

/** Draw the Library into a page container (used by the Rocsy layout). */
function mountLibrary(container) {
    container.classList.add('rad-body', 'rad-page');
    container.dataset.tab = 'library';
    renderLibraryTab(container);
}

function open(tab) {
    if (tab === 'library' && libraryInSite()) {
        dialog?.close();
        globalThis.RocsyVisualOverhaul.showLibrary();
        return;
    }
    if (!tab && settings.tab === 'library' && libraryInSite()) tab = 'voice';
    if (!dialog) {
        dialog = el('dialog', {id: 'rocsy-addons', class: 'rad-dialog', 'aria-label': 'Rocsy Addons'},
            el('header', {class: 'rad-head'},
                el('strong', {text: 'Rocsy Addons'}),
                el('nav', {class: 'rad-tabs', role: 'tablist'}, ...TABS.map(t => el('button', {
                    class: 'rad-tab', type: 'button', role: 'tab', 'data-tab': t.id,
                    onclick: () => {
                        if (t.id === 'library' && libraryInSite()) return open('library');
                        settings.tab = t.id; save(); render();
                    },
                }, icon(t.icon), el('span', {text: t.label})))),
                el('button', {class: 'rad-close', type: 'button', title: 'Close', onclick: () => dialog.close()}, icon('fa-xmark'))),
            el('div', {class: 'rad-body'}));
        dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
        document.body.append(dialog);
    }
    if (tab) settings.tab = tab;
    if (!dialog.open) dialog.showModal();
    render();
    if (!voiceInfo && settings.tab === 'voice') loadVoice().then(render);
}

function buildPanel() {
    const target = document.getElementById('extensions_settings2') || document.getElementById('extensions_settings');
    if (!target || document.getElementById('rad-panel')) return;
    target.append(el('div', {id: 'rad-panel', class: 'inline-drawer'},
        el('div', {class: 'inline-drawer-toggle inline-drawer-header'},
            el('b', {text: 'Rocsy Addons'}), el('div', {class: 'inline-drawer-icon fa-solid fa-circle-chevron-down down'})),
        el('div', {class: 'inline-drawer-content'},
            el('p', {text: 'Rocsy Voice for TTS, Rocsy llama-swap as the default connection, and the card Library.'}),
            el('div', {class: 'flex-container'},
                ...TABS.map(t => el('button', {class: 'menu_button', type: 'button', onclick: () => open(t.id)}, icon(t.icon), el('span', {text: t.label})))))));
}

/** A Library button in SillyTavern's Characters panel, next to Create / Import. */
function buildCharactersButton() {
    if (document.getElementById('rad-library-button')) return;
    const anchor = document.getElementById('external_import_button') || document.getElementById('character_import_button');
    if (!anchor) return;
    anchor.after(el('div', {id: 'rad-library-button', class: 'menu_button fa-solid fa-book-open', title: 'Library: find and import character cards (Bot Booru, Chub, datacat.run)',
        onclick: () => open('library')}));
}

function registerCommand() {
    try {
        const c = ctx();
        c.SlashCommandParser.addCommandObject(c.SlashCommand.fromProps({
            name: 'rocsy',
            callback: (_args, value) => { open(String(value || '').trim().toLowerCase() || undefined); return ''; },
            helpString: 'Open Rocsy Addons: /rocsy, /rocsy voice, /rocsy connection, /rocsy chats or /rocsy library.',
        }));
    } catch (error) {
        console.warn('[Rocsy Addons] slash command', error);
    }
}

// Start-up is split in two so SillyTavern isn't held up: the UI (Library, panel, buttons, /rocsy) is set up at
// APP_INITIALIZED, while SillyTavern's splash is still up, and the Voice/connection work, which talks to the
// server, runs at APP_READY without being awaited (SillyTavern awaits each event listener in turn).
let stored = null;

function initUi() {
    stored = ctx().extensionSettings[MODULE];
    settings = {...DEFAULTS, ...(stored && typeof stored === 'object' ? stored : {})};
    if (!settings.characterVoices || typeof settings.characterVoices !== 'object') settings.characterVoices = {};
    settings.library = {...DEFAULTS.library, ...(settings.library && typeof settings.library === 'object' ? settings.library : {})};
    settings.library.sort = {...DEFAULTS.library.sort, ...(settings.library.sort || {})};
    for (const site of SITES) if (site.sorts && !site.sorts.some(([v]) => v === settings.library.sort[site.id])) settings.library.sort[site.id] = site.sorts[0][0];
    settings.library.filters = {...(settings.library.filters && typeof settings.library.filters === 'object' ? settings.library.filters : {})};
    settings.library.bookmarks = {...(settings.library.bookmarks && typeof settings.library.bookmarks === 'object' ? settings.library.bookmarks : {})};
    if (settings.library.site === 'janitor') settings.library.site = 'datacat';   // JanitorAI tab removed: datacat has its characters
    delete settings.library.sort.janitor;
    if (settings.library.filters?.janitor) delete settings.library.filters.janitor;
    if (!settings.library.chubDefault) { settings.library.site = 'chub'; settings.library.chubDefault = true; }   // once: Chub became the default
    const bl = settings.library.blacklist;
    if (!bl || typeof bl !== 'object') settings.library.blacklist = freshBlacklist();   // new users (and this update): the default template
    else for (const key of ['tags', 'words', 'creators']) bl[key] = Array.isArray(bl[key]) ? bl[key].map(String) : [];
    settings.library.blacklist = {...BLACKLIST_OPTIONS, ...settings.library.blacklist};
    settings.library.imported = {...(settings.library.imported && typeof settings.library.imported === 'object' ? settings.library.imported : {})};
    window.addEventListener('message', event => { onDatacatMessage(event).catch(error => console.error('[Rocsy Addons] datacat', error)); });
    window.addEventListener('message', onConnectMessage);
    window.addEventListener('hashchange', connectFromHash);
    setTimeout(connectFromHash, 0);
    settings.naming = {...DEFAULTS.naming, ...(settings.naming && typeof settings.naming === 'object' ? settings.naming : {})};
    settings.naming.titles = {...(settings.naming.titles && typeof settings.naming.titles === 'object' ? settings.naming.titles : {})};
    settings.modelLoad = {...DEFAULTS.modelLoad, ...(settings.modelLoad && typeof settings.modelLoad === 'object' ? settings.modelLoad : {})};
    if (!LOAD_MODES.some(([mode]) => mode === settings.modelLoad.mode)) settings.modelLoad.mode = 'site';
    globalThis.RocsyAddons = {open, mountLibrary, characterComments, mountConnection, libraryState, libraryRestore, libraryHome, libraryKind, importedLorebookSource: name => Object.entries(lib().imported).find(([k, v]) => v === name && isLore(k.split(':')[0]))?.[0] || '', voiceControl, setCharacterVoice, chatModel, formatCardText, externalMedia: {get: externalMediaAllowed, set: setExternalMedia}, titles: {get: getTitle, rename: renameChat, owner: ownerOf, clean: cleanTitle}, notify};
    buildPanel();
    buildCharactersButton();
    registerCommand();
    const {eventSource, eventTypes} = ctx();
    if (eventTypes.CHAT_CHANGED) eventSource.on(eventTypes.CHAT_CHANGED, () => { mapNewCharacters(); render(); setTimeout(annotateAll, 300); setTimeout(warnMissingVoices, 1500); });
    for (const type of ['CHARACTER_MESSAGE_RENDERED', 'MESSAGE_UPDATED', 'MESSAGE_SWIPED']) {
        if (eventTypes[type]) eventSource.on(eventTypes[type], id => setTimeout(() => annotateSpeed(Number(id)), 50));
    }
    if (eventTypes.MORE_MESSAGES_LOADED) eventSource.on(eventTypes.MORE_MESSAGES_LOADED, () => setTimeout(annotateAll, 50));
    for (const type of ['GENERATION_ENDED', 'MESSAGE_SENT']) if (eventTypes[type]) eventSource.on(eventTypes[type], () => setTimeout(maybeName, 800));
    if (eventTypes.CHAT_RENAMED) eventSource.on(eventTypes.CHAT_RENAMED, onChatRenamed);
    document.dispatchEvent(new CustomEvent('rocsy-addons-ready'));
}

async function initServices() {
    setTimeout(annotateAll, 300);
    // First start: an existing Rocsy Voice setup (OpenAI Compatible → Voice) is adopted as it is; otherwise
    // Rocsy Voice becomes the TTS provider unless another one is already set up.
    const tts = ctx().extensionSettings.tts;
    const existing = tts?.[PROVIDER];
    if (!stored && existing && String(existing.provider_endpoint || '').includes('/Voice/')) {
        settings.engine = String(existing.model || settings.engine).toLowerCase().replace(/-/g, '_');
        settings.speed = Number(existing.speed) || 1;
        const map = typeof existing.voiceMap === 'object' && existing.voiceMap ? existing.voiceMap : {};
        settings.defaultVoice = map[DEFAULT_VOICE] || settings.defaultVoice;
        for (const [name, voice] of Object.entries(map)) if (name !== DEFAULT_VOICE && voice && voice !== DEFAULT_VOICE) settings.characterVoices[name] = voice;
        settings.ttsEnabled = tts.enabled !== false;
    } else if (!stored && (!tts?.currentProvider || tts.currentProvider === PROVIDER || tts.currentProvider === 'System')) {
        await loadVoice();
        if (voiceInfo) await applyVoice({quiet: true});
    } else {
        mapNewCharacters();
    }
    if (!settings.externalMediaDefault) {   // once: external media on (the switch is in the Chats tab)
        settings.externalMediaDefault = true;
        if (!externalMediaAllowed()) setExternalMedia(true);
        save();
    }
    if (!stored) save();
    // Default connection: created and selected when it's missing, selected when no profile is chosen. A different
    // profile the user picked is left alone.
    if (settings.autoConnect && (!ourProfile() || !selectedProfile())) await ensureConnection();
    strictOnce();
    globalThis.RocsyImage?.autoSetup();
}

/** Once: the Rocsy llama-swap profile gets Strict prompt post-processing (see connectLlama). */
function strictOnce() {
    if (settings.strictPostProcessing) return;
    const profile = ourProfile();
    if (!profile) return;
    settings.strictPostProcessing = true;
    const current = profile['prompt-post-processing'] || '';
    if (!current || current === 'none' || current === 'merge' || current === 'merge_tools') {
        profile['prompt-post-processing'] = 'strict';
        if (selectedProfile()?.id === profile.id) $('#custom_prompt_post_processing').val('strict').trigger('change');
    }
    save();
}

let uiStarted = false;
const startUi = () => {
    if (uiStarted) return;
    uiStarted = true;
    try { initUi(); } catch (error) { console.error('[Rocsy Addons] failed to start', error); }
    worldInfo().catch(error => console.warn('[Rocsy Addons] World Info module not loaded', error));
};
const startServices = () => {
    startUi();
    try { initPhoneHelpers(); } catch (error) { console.error('[Rocsy Addons] notifications / keep awake', error); }
    try { startPresence(); } catch (error) { console.error('[Rocsy Addons] model loading', error); }
    setTimeout(() => initServices().catch(error => console.error('[Rocsy Addons] start-up', error)), 0);
};
// Replies survive a dropped connection (phone locked mid-reply): set up right away, before any reply starts.
try { initResilientReplies(); } catch (error) { console.error('[Rocsy Addons] resilient replies', error); }
const {eventSource, eventTypes} = ctx();
// Both events re-fire for late listeners, so this works when the add-on is loaded after start-up too.
if (eventTypes.APP_INITIALIZED) eventSource.once(eventTypes.APP_INITIALIZED, startUi);
if (eventTypes.APP_READY) eventSource.once(eventTypes.APP_READY, startServices);
else startServices();
