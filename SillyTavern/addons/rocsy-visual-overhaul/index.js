// Rocsy Visual Overhaul for SillyTavern.
// A chat-platform layout over SillyTavern's own UI: a sidebar (new chat, search, recent chats, characters,
// grouped settings), a conversation header, a centred chat column and one consistent settings sheet.
// Everything hangs off body.rvo, so switching it off restores SillyTavern exactly; SillyTavern's own
// panels, buttons and settings are reused (opened by clicking their own icons), never copied.

import * as SP from './settings-pages.js';
import * as CP from './chats-page.js';
import * as CT from './chat-tools.js';

const MODULE = 'rocsy-visual-overhaul';
const DEFAULTS = {
    enabled: true,
    theme: 'rocsy',          // rocsy | midnight | light | system | sillytavern
    sidebarCollapsed: false,
    showDetails: false,      // message number, timer, token count
    showBackground: false,   // the chat background picture
    wideChat: false,
    recentLimit: 200,
    formatted: true,         // card text on character pages: formatted or raw
    autotag: {},             // Autotag: model, options and which characters it tagged (see AUTOTAG_DEFAULTS)
    desktopExpression: false, // Rocsy's expression portrait / background on wide screens too (not only ≤1000px)
    chatPins: {},            // Chats page: pinned chats ("c:<avatar>/<file>" | "g:<group>/<file>" -> time)
    chatArchive: {},         // Chats page: archived chats (hidden from the sidebar)
};
const THEMES = {
    rocsy: 'Rocsy Dark', midnight: 'Midnight', light: 'Light', system: 'Follow system', sillytavern: 'SillyTavern theme',
};
// Sidebar settings entries → SillyTavern's own drawer icons (clicked to open their panel).
const SETTINGS = [
    {key: 'connection', label: 'Model & connection', icon: 'fa-plug', page: 'connection'},
    {key: 'response', label: 'Response settings', icon: 'fa-sliders', page: 'response'},
    {key: 'formatting', label: 'Prompt formatting', icon: 'fa-font', page: 'formatting'},
    {key: 'lore', label: 'World Info', icon: 'fa-book-atlas', page: 'lorebooks'},   // in-site: Lorebooks page + its activation settings
    {key: 'persona', label: 'Persona', icon: 'fa-face-smile', page: 'persona', options: {persona: ''}},
    {key: 'quickreplies', label: 'Quick Replies', icon: 'fa-bolt', page: 'quickreplies'},
    {key: 'regex', label: 'Regex', icon: 'fa-code', page: 'regex'},
    {key: 'memory', label: 'Memory', icon: 'fa-brain', page: 'memory', title: 'Summarize and Vector Storage'},
    {key: 'extras', label: 'Expressions & images', icon: 'fa-image', page: 'extras'},
    {key: 'extensions', label: 'Extensions', icon: 'fa-cubes', page: 'extensions'},
    {key: 'backgrounds', label: 'Backgrounds', icon: 'fa-panorama', page: 'backgrounds'},
    {key: 'interface', label: 'Interface', icon: 'fa-user-gear', page: 'interface', title: 'Layout, phone, notifications, themes and chat behaviour'},
];

// Titles for SillyTavern's panels when they open as Rocsy sheets (keyed by their drawer's id).
const SHEET_TITLES = {
    'ai-config-button': 'Response settings', 'sys-settings-button': 'Model & connection',
    'advanced-formatting-button': 'Prompt formatting', 'WI-SP-button': 'World Info', 'user-settings-button': 'Interface',
    'backgrounds-button': 'Backgrounds', 'extensions-settings-button': 'Extensions',
    'persona-management-button': 'Persona', 'rightNavHolder': 'Characters',
};

const ctx = () => globalThis.SillyTavern.getContext();
const $q = (selector, root = document) => root.querySelector(selector);
let settings = {...DEFAULTS};
let recents = [];
let recentsTimer = null;
const expanded = new Set();     // characters / groups whose chat history is unfolded in the sidebar
const histories = new Map();    // key -> that character's full chat list (loaded when unfolded)
let built = false;

function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
        else if (value !== false && value !== undefined && value !== null) node.setAttribute(key, value === true ? '' : value);
    }
    node.append(...children.filter(Boolean));
    return node;
}

const icon = name => el('i', {class: `fa-solid ${name} fa-fw`, 'aria-hidden': 'true'});

function save() {
    ctx().extensionSettings[MODULE] = settings;
    ctx().saveSettingsDebounced();
}

function loadSettings() {
    const stored = ctx().extensionSettings[MODULE];
    settings = {...DEFAULTS, ...(stored && typeof stored === 'object' ? stored : {})};
    if (!THEMES[settings.theme]) settings.theme = DEFAULTS.theme;
}

// ------------------------------------------------------------------ drawers --

function openDrawers() {
    return [...document.querySelectorAll('#top-settings-holder .drawer-content.openDrawer, #movingDivs .drawer-content.openDrawer')]
        .filter(panel => !panel.classList.contains('pinnedOpen'));
}

/** Open or close a SillyTavern drawer the way its own top-bar icon does. */
const toggleDrawer = drawer => drawer?.querySelector(':scope > .drawer-toggle')?.click();

function closeSheets() {
    for (const panel of openDrawers()) toggleDrawer(panel.closest('.drawer'));
    $q('#character_popup.open') && $q('#character_cross')?.click();
}

/** Show one SillyTavern panel as the sheet (others close); its sidebar entry again closes it. */
function openSetting(selector) {
    const target = $q(selector);
    if (!target) return toastr.warning('That SillyTavern panel isn’t available here.');
    const drawer = target.closest('.drawer');
    for (const other of openDrawers()) if (other.closest('.drawer') !== drawer) toggleDrawer(other.closest('.drawer'));
    const wasOpen = openDrawers().length > 0;
    target.click();
    closeSidebarOnMobile();
    if (openDrawers().length && !wasOpen) navPush({overlay: 'sheet'});
}

const isPhone = () => window.matchMedia('(max-width: 900px)').matches;

// ------------------------------------------------------------------- pages --
// Library and Characters are pages of the site: they take the chat area's place (the chat stays loaded under
// them). Hiding a page keeps its DOM, so search results, scroll and the datacat page survive.

let page = '';   // '', 'library', 'characters' or 'lorebooks'
const PAGES = {
    library: {label: 'Library', sub: 'Find character cards and lorebooks on Chub, Bot Booru and datacat.run'},
    characters: {label: 'Characters', sub: ''},
    lorebooks: {label: 'Lorebooks', sub: ''},
    chats: CP.PAGE,
    ...SP.PAGES,
};

function pageNode(id) {
    let node = $q(`#rvo-${id}`);
    if (!node) {
        node = el('main', {id: `rvo-${id}`, class: 'rvo-page', 'aria-label': PAGES[id].label});
        document.body.append(node);
    }
    return node;
}

function showPage(id, options = {}) {
    if (!built) return;
    if (id === 'library' && !globalThis.RocsyAddons?.mountLibrary) return toastr.warning('Rocsy Addons isn’t loaded, so the Library isn’t available.');
    closeSheets();
    SP.leave();
    const node = pageNode(id);
    if (id === 'library') globalThis.RocsyAddons.mountLibrary(node);
    else if (id === 'lorebooks') renderLorebooksPage(node, options);
    else if (id === 'chats') CP.render(node, options);
    else if (SP.isSettingsPage(id)) SP.render(id, node, options);
    else renderCharactersPage(node, options);
    page = id;
    document.querySelectorAll('.rvo-page').forEach(n => n.toggleAttribute('data-active', n === node));
    document.body.classList.add('rvo-page-open');
    document.body.dataset.rvoPage = id;
    document.querySelectorAll('#rvo-sidebar .rvo-nav[data-key]').forEach(b => (b.dataset.key === id ? b.setAttribute('aria-current', 'page') : b.removeAttribute('aria-current')));
    renderHeader();
    closeSidebarOnMobile();
    if (id === 'library' && options.home) globalThis.RocsyAddons?.libraryHome?.();
    navPush();
}

function hidePage() {
    if (!page) return;
    flushEdits();
    flushLorebook();
    SP.leave();
    page = '';
    document.body.classList.remove('rvo-page-open');
    delete document.body.dataset.rvoPage;
    document.querySelectorAll('.rvo-page[data-active]').forEach(n => n.removeAttribute('data-active'));
    document.querySelectorAll('#rvo-sidebar .rvo-nav[aria-current]').forEach(b => b.removeAttribute('aria-current'));
    renderHeader();
    navPush();
}

// ----------------------------------------------------------------- history --
// Every step between the chat, Library (source, card) and Characters (gallery, character, editor) is a browser
// history entry, so Back / Forward (and a phone's back gesture) walk through them. A menu sheet or the phone
// sidebar gets an entry too, so Back closes it first instead of leaving the page. An in-page Back button whose
// target is the previous entry goes back in history instead of adding one, so both stay in step.
const nav = {i: 0, entries: [], restoring: false, selfBack: false};
const navKey = state => JSON.stringify([state.page, state.view, state.avatar, state.lib?.site || '', state.lib?.detail || '', state.lib?.page || 0, state.lib?.creator || '']);
function navState(extra = {}) {
    const sp = SP.isSettingsPage(page) ? SP.navInfo(page) : null;
    const cp = page === 'chats' ? CP.navInfo() : null;
    return {rvo: 1, page, view: sp ? sp.view : cp ? cp.view : page === 'characters' ? chars.view : page === 'lorebooks' && lore.book ? 'book' : '',
        avatar: sp ? sp.avatar : cp ? cp.avatar : page === 'characters' ? chars.avatar : page === 'lorebooks' ? lore.book : '',
        lib: page === 'library' ? globalThis.RocsyAddons?.libraryState?.() || null : null, ...extra};
}
function navReset() {
    const state = {...navState(), i: 0};
    nav.i = 0; nav.entries = [state];
    try { history.replaceState({...(history.state && typeof history.state === 'object' ? history.state : {}), ...state}, ''); } catch { /* fine */ }
}
function navPush(extra = {}) {
    if (nav.restoring || !built) return;
    const state = navState(extra);
    const current = nav.entries[nav.i];
    if (!extra.overlay && current && !current.overlay && navKey(current) === navKey(state)) return;
    if (current?.overlay && !extra.overlay) {   // the sheet / sidebar closed by itself: replace its entry
        nav.entries[nav.i] = {...state, i: nav.i};
        history.replaceState(nav.entries[nav.i], '');
        return;
    }
    const previous = nav.entries[nav.i - 1];
    if (!extra.overlay && previous && !previous.overlay && navKey(previous) === navKey(state)) {   // "back" by button
        nav.selfBack = true; nav.i -= 1; history.back();
        return;
    }
    nav.i += 1;
    nav.entries.length = nav.i;
    nav.entries.push({...state, i: nav.i});
    history.pushState(nav.entries[nav.i], '');
}
const overlayOpen = () => document.body.classList.contains('rvo-side-open') || openDrawers().length > 0 || !!$q('#character_popup.open') || !!$q('#rvo-gallery');
function closeOverlays() {
    closeSidebarOnMobile();
    closeSheets();
    closeGallery();
}

// --------------------------------------------------------- expression gallery --
// A character's expression sprites, full size, one at a time: arrows / keys / swipe step through every sprite in
// the folder (several per expression are fine: joy.png, joy-2.png), a filmstrip below jumps to any of them.

/** SillyTavern's sprite folder for a character: the character's name, unless an override path is set. */
function spriteFolder(ch) {
    const override = (ctx().extensionSettings.expressionOverrides || []).find(o => o.name === charFile(ch.avatar));
    return override?.path || ch.name;
}

async function loadSprites(ch) {
    const response = await fetch(`/api/sprites/get?name=${encodeURIComponent(spriteFolder(ch))}`, {headers: ctx().getRequestHeaders()});
    const list = response.ok ? await response.json() : [];
    return (Array.isArray(list) ? list : []).sort((a, b) => a.label.localeCompare(b.label) || a.path.localeCompare(b.path));
}

const gallery = {items: [], i: 0, name: '', touchX: null};
function closeGallery() {
    const node = $q('#rvo-gallery');
    if (!node) return;
    node.remove();
    document.removeEventListener('keydown', galleryKey, true);
}
function galleryKey(event) {
    if (!$q('#rvo-gallery')) return;
    if (event.key === 'ArrowRight') { event.preventDefault(); event.stopPropagation(); galleryGo(gallery.i + 1); }
    else if (event.key === 'ArrowLeft') { event.preventDefault(); event.stopPropagation(); galleryGo(gallery.i - 1); }
    else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); history.state?.overlay === 'gallery' ? history.back() : closeGallery(); }
}
function galleryGo(index) {
    const node = $q('#rvo-gallery');
    if (!node || !gallery.items.length) return;
    gallery.i = (index + gallery.items.length) % gallery.items.length;
    const item = gallery.items[gallery.i];
    const same = gallery.items.filter(x => x.label === item.label);
    const nth = same.length > 1 ? ` (${same.indexOf(item) + 1} of ${same.length})` : '';
    node.querySelector('.rvo-gal-img').src = item.path;
    node.querySelector('.rvo-gal-label').textContent = item.label + nth;
    node.querySelector('.rvo-gal-count').textContent = `${gallery.i + 1} / ${gallery.items.length}`;
    node.querySelector('.rvo-gal-open').href = item.path;
    node.querySelectorAll('.rvo-gal-thumb').forEach((t, i) => { t.classList.toggle('on', i === gallery.i); if (i === gallery.i) t.scrollIntoView({block: 'nearest', inline: 'center'}); });
}

/** Open the gallery for a character, at an expression label (or the first sprite). */
async function showExpressionGallery(ch, label = '') {
    if (typeof ch === 'string') ch = ctx().characters.find(x => x.avatar === ch);
    if (!ch) return;
    let items;
    try { items = await loadSprites(ch); } catch (error) { return toastr.error(error.message, 'Expressions'); }
    if (!items.length) return toastr.info(`${ch.name} has no expression pictures yet.`, 'Expressions');
    closeGallery();
    gallery.items = items;
    gallery.name = ch.name;
    const start = Math.max(0, items.findIndex(x => x.label === String(label).toLowerCase()));
    const node = el('div', {id: 'rvo-gallery', role: 'dialog', 'aria-modal': 'true', 'aria-label': `${ch.name}’s expressions`,
        onclick: event => { if (event.target === node || event.target.classList.contains('rvo-gal-stage')) history.state?.overlay === 'gallery' ? history.back() : closeGallery(); }},
        el('header', {class: 'rvo-gal-bar'},
            el('img', {class: 'rvo-gal-avatar', src: ctx().getThumbnailUrl('avatar', ch.avatar), alt: ''}),
            el('div', {class: 'rvo-gal-title'}, el('strong', {class: 'rvo-gal-label'}), el('small', {}, el('span', {text: `${ch.name} · `}), el('span', {class: 'rvo-gal-count'}))),
            el('a', {class: 'rvo-icon-button rvo-gal-open', href: '#', target: '_blank', rel: 'noopener', title: 'Open the full picture'}, icon('fa-up-right-from-square')),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Close (Esc)', onclick: () => (history.state?.overlay === 'gallery' ? history.back() : closeGallery())}, icon('fa-xmark'))),
        el('div', {class: 'rvo-gal-stage',
            ontouchstart: event => { gallery.touchX = event.touches[0].clientX; },
            ontouchend: event => {
                if (gallery.touchX === null) return;
                const dx = event.changedTouches[0].clientX - gallery.touchX; gallery.touchX = null;
                if (Math.abs(dx) > 40) galleryGo(gallery.i + (dx < 0 ? 1 : -1));
            }},
            el('button', {class: 'rvo-gal-nav prev', type: 'button', title: 'Previous (←)', onclick: () => galleryGo(gallery.i - 1)}, icon('fa-chevron-left')),
            el('img', {class: 'rvo-gal-img', alt: ''}),
            el('button', {class: 'rvo-gal-nav next', type: 'button', title: 'Next (→)', onclick: () => galleryGo(gallery.i + 1)}, icon('fa-chevron-right'))),
        el('nav', {class: 'rvo-gal-strip', 'aria-label': 'All expressions'}, ...items.map((item, i) => el('button', {class: 'rvo-gal-thumb', type: 'button', title: item.label, onclick: () => galleryGo(i)},
            el('img', {src: item.path, alt: '', loading: 'lazy'}), el('small', {text: item.label})))));
    document.body.append(node);
    document.addEventListener('keydown', galleryKey, true);
    galleryGo(start);
    if (built) navPush({overlay: 'gallery'});
}

/** Character page side card: the expression pictures as a small grid (hidden when there are none). */
function expressionsCard(ch) {
    const grid = el('div', {class: 'rvo-dexpr'});
    const card = el('section', {class: 'rvo-dcard', hidden: true}, el('h4', {}, icon('fa-masks-theater'), el('span', {text: 'Expressions'}), el('small', {class: 'rvo-dexpr-n'})), grid);
    loadSprites(ch).then(items => {
        if (!items.length) return;
        const labels = [...new Set(items.map(x => x.label))];
        card.querySelector('.rvo-dexpr-n').textContent = String(labels.length);
        const shown = labels.slice(0, 11);
        grid.replaceChildren(...shown.map(label => el('button', {class: 'rvo-dexpr-cell', type: 'button', title: label, onclick: () => showExpressionGallery(ch, label)},
            el('img', {src: items.find(x => x.label === label).path, alt: '', loading: 'lazy'}), el('small', {text: label}))),
            labels.length > shown.length ? el('button', {class: 'rvo-dexpr-cell more', type: 'button', title: 'All expressions', onclick: () => showExpressionGallery(ch, labels[shown.length])},
                el('strong', {text: `+${labels.length - shown.length}`}), el('small', {text: 'more'})) : null);
        card.hidden = false;
    }).catch(() => {});
    return card;
}
function applyNav(state) {
    nav.restoring = true;
    try {
        if (!state.page) hidePage();
        else if (state.page === 'characters') {
            if (page !== 'characters' || chars.view !== state.view || chars.avatar !== state.avatar)
                showPage('characters', state.view === 'autotag' ? {autotag: true} : state.view === 'create' ? {create: true} : {avatar: state.view === 'grid' ? '' : state.avatar || '', edit: state.view === 'edit'});
        } else if (state.page === 'lorebooks') {
            if (page !== 'lorebooks' || lore.book !== (state.avatar || '')) showPage('lorebooks', {book: state.avatar || ''});
        } else if (state.page === 'chats') {
            const here = CP.navInfo();
            if (page !== 'chats' || here.view !== (state.view || '') || here.avatar !== (state.avatar || '')) showPage('chats', {tab: state.view || 'all', who: state.avatar || ''});
        } else if (SP.isSettingsPage(state.page)) {
            if (page !== state.page || !SP.samePlace(state.page, state)) showPage(state.page, SP.optionsFromNav(state));
        } else if (state.page === 'library') {
            if (page !== 'library') showPage('library');
            globalThis.RocsyAddons?.libraryRestore?.(state.lib);
        }
    } finally { nav.restoring = false; }
}
function onPopState(event) {
    const state = event.state;
    if (!state?.rvo || !built) return;
    if (nav.selfBack) { nav.selfBack = false; return; }
    const from = nav.i;
    nav.i = Number.isInteger(state.i) ? state.i : 0;
    if (!nav.entries[nav.i]) nav.entries[nav.i] = state;
    if (overlayOpen()) {
        closeOverlays();
        // Back closed a sheet that had no entry of its own: stay where we are.
        if (nav.i < from && !nav.entries[from]?.overlay && navKey(nav.entries[from] || {}) !== navKey(state)) { nav.i = from; history.forward(); nav.selfBack = true; }
        return;
    }
    // An overlay entry (closed by a tap meanwhile) or a step equal to where we are: skip over it.
    const here = nav.entries[from];
    if (state.overlay || (here && navKey(here) === navKey(state) && nav.i !== from)) {
        if (nav.i > from) history.forward(); else if (nav.i > 0) history.back();
        return;
    }
    applyNav(state);
}

const showLibrary = () => showPage('library');
const hideLibrary = hidePage;
const openCharacters = () => showPage('characters');

// --------------------------------------------------------------- lorebooks --
// Lorebooks (SillyTavern's World Info), all in the site: the list (search, entry counts, where each book is used,
// "All chats" switch), a book's page (where it's used: all chats / this chat / characters; every entry with all
// its fields; add, duplicate, move, delete; rename, duplicate, export, delete the book) and the activation
// settings that apply to every lorebook. SillyTavern's World Info module does the loading and saving; its own
// panel is never shown. Rename / duplicate / export press SillyTavern's own (hidden) buttons, so its link
// updates happen too.

let wiModule = null;
const worldInfo = async () => (wiModule ||= await import('../../../world-info.js'));
const lore = {q: '', book: '', data: null, open: new Set(), eq: '', esort: 'order', showSettings: false, status: ''};
const loreEntries = new Map();   // name -> entry count

function rvoButton(label, faIcon, onclick, title = label, extra = '') {
    return el('button', {class: `rvo-cbtn ${extra}`, type: 'button', title, onclick}, icon(faIcon), el('span', {text: label}));
}
const entriesText = n => `${n} ${n === 1 ? 'entry' : 'entries'}`;
const charFile = avatar => String(avatar || '').replace(/\.[^.]+$/, '');

/** Turn a lorebook on or off for every chat, through SillyTavern's own global World Info list. */
function setGlobalLorebook(name, on) {
    const index = (wiModule?.world_names || []).indexOf(name);
    if (index < 0) return;
    const select = $('#world_info');
    const values = new Set((select.val() || []).map(String));
    if (on) values.add(String(index)); else values.delete(String(index));
    select.val([...values]).trigger('change');
}

/** The open chat's own lorebook (SillyTavern's chat lore). */
const chatLorebook = () => ctx().chatMetadata?.world_info || '';
async function setChatLorebook(name, on) {
    const meta = ctx().chatMetadata;
    if (!meta) return;
    if (on) meta.world_info = name; else if (meta.world_info === name) delete meta.world_info;
    await ctx().saveMetadata();
    $('.chat_lorebook_button').toggleClass('world_set', !!meta.world_info);
}

/** A character's primary lorebook (character card extensions.world), saved on the card. */
async function setCharacterLorebook(avatar, name) {
    const index = ctx().characters.findIndex(c => c.avatar === avatar);
    if (index < 0) return;
    const response = await fetch('/api/characters/merge-attributes', {method: 'POST', headers: ctx().getRequestHeaders(),
        body: JSON.stringify({avatar, data: {extensions: {world: name}}})});
    if (!response.ok) throw new Error(`SillyTavern didn’t save the character (${response.status})`);
    const character = ctx().characters[index];
    character.data ||= {}; character.data.extensions ||= {};
    character.data.extensions.world = name;
    if (String(ctx().characterId) === String(index)) $('#character_world').val(name);
}

function lorebookUsers(name) {
    const lores = wiModule?.world_info?.charLore || [];
    return ctx().characters.filter(c => c?.data?.extensions?.world === name
        || lores.some(l => l.name === charFile(c.avatar) && l.extraBooks?.includes(name)));
}

/** Point SillyTavern's hidden World Info editor at a book (its rename / duplicate / export buttons act on it). */
async function selectInHiddenEditor(name) {
    const wi = await worldInfo();
    const index = wi.world_names.indexOf(name);
    if (index < 0) throw new Error('That lorebook isn’t there any more.');
    if (String($('#world_editor_select').val()) !== String(index)) {
        $('#world_editor_select').val(index).trigger('change');
        await new Promise(resolve => setTimeout(resolve, 300));
    }
}

async function openLorebook(name) {
    showPage('lorebooks', {book: name});
}

async function createLorebook() {
    const wi = await worldInfo();
    const name = await ctx().callGenericPopup('Name of the new lorebook', ctx().POPUP_TYPE.INPUT, wi.getFreeWorldName?.('New lorebook') || 'New lorebook');
    if (!name || typeof name !== 'string' || !name.trim()) return;
    if (await wi.createNewWorldInfo(name.trim(), {interactive: true}) === false) return;
    await wi.updateWorldInfoList?.();
    showPage('lorebooks', {book: name.trim()});
}

async function deleteLorebook(name) {
    const wi = await worldInfo();
    const ok = await ctx().callGenericPopup(`Delete the lorebook “${name}”? This can’t be undone.`, ctx().POPUP_TYPE.CONFIRM);
    if (!ok) return;
    if (await wi.deleteWorldInfo(name)) { loreEntries.delete(name); toastr.info(`Deleted ${name}`, 'Lorebooks'); }
    if (lore.book === name) { lore.book = ''; lore.data = null; navPush(); }
    refreshLorebooksPage(true);
}

/** Rename / duplicate / export through SillyTavern's own buttons (popups and link updates included). */
async function bookAction(name, button, after) {
    const wi = await worldInfo();
    await flushLorebook();
    await selectInHiddenEditor(name);
    const before = new Set(wi.world_names);
    $(button).trigger('click');
    if (!after) return;
    // Wait for the popup to be answered and the list to change.
    for (let i = 0; i < 600; i++) {
        await new Promise(resolve => setTimeout(resolve, 250));
        if (document.querySelector('dialog.popup[open]')) continue;
        const added = wi.world_names.find(n => !before.has(n));
        if (added || i > 8) { after(added, wi); return; }
    }
}
const renameLorebook = name => bookAction(name, '#world_popup_name_button', added => {
    if (!added) return;
    loreEntries.delete(name);
    lore.book = added; lore.data = null;
    showPage('lorebooks', {book: added});
});
const duplicateLorebook = name => bookAction(name, '#world_duplicate', added => { if (added) showPage('lorebooks', {book: added}); });
const exportLorebook = name => bookAction(name, '#world_popup_export');

// ------------------------------------------------------------------ list --

// Each lorebook gets a colour of its own (from its name), used for its icon tile.
const nameHue = name => { let h = 0; for (const ch of String(name)) h = (h * 31 + ch.codePointAt(0)) % 360; return h; };
const bookIcon = (name, big = false) => el('span', {class: `rvo-lbook-icon${big ? ' big' : ''}`, style: `--rvo-hue: ${nameHue(name)}`}, icon('fa-book-atlas'));

/** Header of a page: icon, title, a line of text, stat tiles. */
function pageBanner(iconName, title, text, stats = []) {
    return el('section', {class: 'rvo-sbanner'},
        el('span', {class: 'rvo-sbanner-icon'}, icon(iconName)),
        el('div', {class: 'rvo-sbanner-text'}, el('h2', {text: title}), text ? el('p', {text}) : null),
        stats.length ? statTiles(stats) : null);
}

/** A row of filter chips: options = [[value, label, count]]. */
function filterChips(options, value, onchange) {
    return el('div', {class: 'rvo-chips', role: 'tablist'}, ...options.map(([v, label, count]) => el('button', {
        class: `rvo-chip${v === value ? ' on' : ''}`, type: 'button', role: 'tab', 'aria-selected': String(v === value),
        onclick: () => onchange(v)}, el('span', {text: label}), count !== undefined ? el('small', {text: String(count)}) : null)));
}

const loreUse = name => {
    const global = (wiModule?.selected_world_info || []).includes(name);
    const chat = chatLorebook() === name;
    const users = lorebookUsers(name);
    return {global, chat, users, kind: global ? 'global' : chat ? 'chat' : users.length ? 'chars' : 'unused'};
};

async function renderLorebooksPage(root, options = {}) {
    // Synchronous part first: showPage() records the history step right after this returns its promise.
    if ('book' in options && options.book !== lore.book) { flushLorebook(); lore.book = options.book || ''; lore.data = null; lore.open.clear(); lore.eq = ''; lore.limit = 0; }
    if (options.settings) lore.showSettings = true;
    let wi;
    try { wi = await worldInfo(); } catch (error) {
        root.replaceChildren(el('div', {class: 'rvo-cwrap'}, el('p', {class: 'rvo-empty', text: `SillyTavern’s World Info isn’t available (${error.message}).`})));
        return;
    }
    if (!wi.world_names && wi.updateWorldInfoList) await wi.updateWorldInfoList();
    if (lore.book && !wi.world_names.includes(lore.book)) { lore.book = ''; lore.data = null; }
    if (lore.book) return renderLorebook(root, lore.book);
    const list = el('div', {class: 'rvo-llist'});
    const search = el('input', {type: 'search', class: 'rvo-cinput', placeholder: 'Search lorebooks', value: lore.q,
        oninput: event => { lore.q = event.target.value.trim(); drawLorebooks(list); }});
    const names = wi.world_names || [];
    const uses = names.map(loreUse);
    const totalEntries = names.reduce((n, name) => n + (loreEntries.get(name) || 0), 0);
    const settingsBox = el('details', {class: 'rvo-lsettings', open: lore.showSettings || undefined,
        ontoggle: event => { lore.showSettings = event.target.open; if (event.target.open && !event.target.dataset.built) { event.target.dataset.built = '1'; event.target.append(activationSettings()); } }},
        el('summary', {}, icon('fa-sliders'), el('span', {text: 'Activation settings'}), el('small', {text: 'for every lorebook'})));
    if (lore.showSettings) { settingsBox.dataset.built = '1'; settingsBox.append(activationSettings()); }
    root.replaceChildren(el('div', {class: 'rvo-cwrap rvo-lwrap'},
        pageBanner('fa-book-atlas', 'Lorebooks', 'Background facts that join the prompt when their keywords come up in the chat. Use one in every chat, in one chat, or with a character.', [
            ['Lorebooks', names.length, 'fa-book'], ['Every chat', uses.filter(u => u.global).length, 'fa-globe'],
            ['With characters', uses.filter(u => u.users.length).length, 'fa-user-tag'], ['Entries', totalEntries || '…', 'fa-list', 'rvo-lstat-entries']]),
        el('div', {class: 'rvo-ctools'}, search,
            rvoButton('Find online', 'fa-book-open', () => { showPage('library'); globalThis.RocsyAddons?.libraryKind?.('lore'); }, 'Browse lorebooks on Chub and Bot Booru'),
            rvoButton('Create', 'fa-plus', () => createLorebook().catch(error => toastr.error(error.message))),
            rvoButton('Import', 'fa-file-import', () => $q('#world_import_file')?.click(), 'Import a lorebook file (JSON or PNG)')),
        el('div', {class: 'rvo-lfilter'}),
        settingsBox,
        list));
    if (options.settings) settingsBox.scrollIntoView({block: 'start'});
    drawLorebooks(list);
    renderHeader();
}

const LORE_GROUPS = [['global', 'In every chat', 'fa-globe'], ['chat', 'In this chat', 'fa-message'], ['chars', 'With characters', 'fa-user-tag'], ['unused', 'Not in use', 'fa-box-archive']];
function drawLorebooks(list) {
    const names = wiModule?.world_names || [];
    const q = lore.q.toLowerCase();
    const all = names.map(name => ({name, ...loreUse(name)}));
    const filterBox = $q('#rvo-lorebooks .rvo-lfilter');
    lore.filter ||= 'all';
    if (filterBox) filterBox.replaceChildren(filterChips([['all', 'All', all.length], ...LORE_GROUPS.map(([k, label]) => [k, label, all.filter(b => b.kind === k).length])],
        lore.filter, value => { lore.filter = value; drawLorebooks(list); }));
    if (!names.length) {
        list.replaceChildren(el('div', {class: 'rvo-lempty'}, icon('fa-book-atlas'), el('strong', {text: 'No lorebooks yet'}),
            el('span', {text: 'Find one online, import a file, or create your own.'}),
            rvoButton('Find lorebooks online', 'fa-book-open', () => { showPage('library'); globalThis.RocsyAddons?.libraryKind?.('lore'); })));
        return;
    }
    const shown = all.filter(b => (!q || b.name.toLowerCase().includes(q)) && (lore.filter === 'all' || b.kind === lore.filter));
    if (!shown.length) { list.replaceChildren(el('p', {class: 'rvo-empty', text: 'No lorebooks match.'})); return; }
    const updateTotal = () => {
        const node = $q('#rvo-lstat-entries');
        if (node && names.every(n => loreEntries.has(n))) node.textContent = compactNumber(names.reduce((n, name) => n + loreEntries.get(name), 0));
    };
    const card = book => {
        const {name, users} = book;
        const source = globalThis.RocsyAddons?.importedLorebookSource?.(name) || '';
        const count = el('span', {class: 'rvo-lcount', text: loreEntries.has(name) ? entriesText(loreEntries.get(name)) : '…'});
        if (!loreEntries.has(name)) wiModule.loadWorldInfo(name).then(data => {
            loreEntries.set(name, data?.entries ? Object.keys(data.entries).length : 0);
            count.textContent = entriesText(loreEntries.get(name));
            updateTotal();
        }).catch(() => { count.textContent = ''; });
        const pills = [book.global ? ['fa-globe', 'Every chat'] : null, book.chat ? ['fa-message', 'This chat'] : null,
            source ? ['fa-cloud-arrow-down', source.startsWith('chub') ? 'Chub' : 'Bot Booru'] : null].filter(Boolean);
        const toggle = el('input', {type: 'checkbox', checked: book.global || undefined, onchange: event => setGlobalLorebook(name, event.target.checked)});
        return el('article', {class: `rvo-lbook${book.global ? ' on' : ''}`},
            el('button', {class: 'rvo-lbook-main', type: 'button', title: `Open ${name}`, onclick: () => openLorebook(name)},
                bookIcon(name),
                el('span', {class: 'rvo-lbook-text'}, el('strong', {text: name}),
                    el('small', {class: 'rvo-lmeta'}, count),
                    pills.length ? el('span', {class: 'rvo-lpills'}, ...pills.map(([i, t]) => el('em', {}, icon(i), el('span', {text: t})))) : null)),
            users.length ? el('div', {class: 'rvo-lavatars', title: `Used by ${users.map(c => c.name).join(', ')}`},
                ...users.slice(0, 5).map(c => el('img', {src: ctx().getThumbnailUrl('avatar', c.avatar), alt: c.name, loading: 'lazy'})),
                users.length > 5 ? el('span', {text: `+${users.length - 5}`}) : null) : null,
            el('div', {class: 'rvo-lbook-actions'},
                switchControl(toggle, 'Every chat', 'Use this lorebook in every chat'),
                el('button', {class: 'rvo-icon-button', type: 'button', title: `Open ${name}`, onclick: () => openLorebook(name)}, icon('fa-pen-to-square')),
                el('button', {class: 'rvo-icon-button rvo-ldelete', type: 'button', title: `Delete ${name}`, onclick: () => deleteLorebook(name)}, icon('fa-trash-can'))));
    };
    const groups = lore.filter === 'all' ? LORE_GROUPS : LORE_GROUPS.filter(([k]) => k === lore.filter);
    list.replaceChildren(...groups.map(([kind, label, iconName]) => {
        const books = shown.filter(b => b.kind === kind).sort((a, b) => a.name.localeCompare(b.name));
        if (!books.length) return null;
        return el('section', {class: 'rvo-lgroup'},
            el('h3', {class: 'rvo-lgroup-title'}, icon(iconName), el('span', {text: label}), el('small', {text: String(books.length)})),
            el('div', {class: 'rvo-lgrid'}, ...books.map(card)));
    }).filter(Boolean));
    updateTotal();
}

function switchControl(input, label, title = label) {
    return el('label', {class: 'rvo-lswitch', title}, input, el('span', {class: 'rvo-lslider'}), label ? el('span', {class: 'rvo-lswitch-text', text: label}) : null);
}

/** Redraw what's on screen after World Info changed elsewhere (never while an entry is being typed in). */
function refreshLorebooksPage(force = false) {
    if (page !== 'lorebooks') return;
    if (lore.book) {
        if (force || !wiModule?.world_names?.includes(lore.book)) renderLorebooksPage(pageNode('lorebooks'));
        else drawUsage();
        return;
    }
    const list = $q('#rvo-lorebooks .rvo-llist');
    if (list) drawLorebooks(list); else renderLorebooksPage(pageNode('lorebooks'));
    renderHeader();
}

// ---------------------------------------------------- activation settings --
// SillyTavern's global World Info settings, shown here and written through its own inputs (so its handlers save).
const WI_NUMBERS = [
    ['world_info_depth', 'Scan depth', 'How many recent messages are searched for keywords.', 0, 1000],
    ['world_info_budget', 'Context %', 'Share of the context lorebooks may use.', 1, 100],
    ['world_info_budget_cap', 'Budget cap (tokens)', '0 = no cap.', 0, 65536],
    ['world_info_min_activations', 'Min activations', 'Keep scanning older messages until this many entries fire (0 = off).', 0, 100],
    ['world_info_min_activations_depth_max', 'Max depth for min activations', '0 = unlimited.', 0, 100],
    ['world_info_max_recursion_steps', 'Max recursion steps', '0 = unlimited. Can’t be used with min activations.', 0, 10],
];
const WI_CHECKS = [
    ['world_info_include_names', 'Include names', 'Search message text with the speaker’s name in front.', 'input'],
    ['world_info_recursive', 'Recursive scan', 'Entries can trigger other entries.', 'input'],
    ['world_info_case_sensitive', 'Case-sensitive keys', '', 'input'],
    ['world_info_match_whole_words', 'Match whole words', '', 'input'],
    ['world_info_use_group_scoring', 'Use group scoring', 'In inclusion groups, the entry with the most matching keys wins.', 'change'],
    ['world_info_overflow_alert', 'Alert on overflow', 'Warn when entries don’t fit the budget.', 'change'],
];
function activationSettings() {
    const box = el('div', {class: 'rvo-lset-grid'});
    for (const [id, label, hint, min, max] of WI_NUMBERS) {
        const source = $q(`#${id}`);
        if (!source) continue;
        const input = el('input', {type: 'number', class: 'rvo-cinput', min, max, step: 1, value: source.value,
            onchange: event => {
                const value = Math.min(max, Math.max(min, Number(event.target.value) || 0));
                event.target.value = value;
                $(source).val(value).trigger('input');
                setTimeout(() => box.querySelectorAll('input[data-for]').forEach(i => { const s = $q(`#${i.dataset.for}`); if (s && s.type !== 'checkbox') i.value = s.value; }), 50);
            }, 'data-for': id});
        box.append(el('label', {class: 'rvo-lfield'}, el('span', {text: label}), input, hint ? el('small', {text: hint}) : null));
    }
    const strategy = $q('#world_info_character_strategy');
    if (strategy) box.append(el('label', {class: 'rvo-lfield'}, el('span', {text: 'Insertion order'}),
        el('select', {class: 'rvo-cinput', onchange: event => $(strategy).val(event.target.value).trigger('change')},
            ...[...strategy.options].map(o => el('option', {value: o.value, text: o.textContent.trim(), selected: o.selected || undefined}))),
        el('small', {text: 'Which comes first: global lorebooks or the character’s.'})));
    const checks = el('div', {class: 'rvo-lset-checks'});
    for (const [id, label, hint, event] of WI_CHECKS) {
        const source = $q(`#${id}`);
        if (!source) continue;
        checks.append(el('label', {class: 'rvo-lcheck', title: hint},
            el('input', {type: 'checkbox', checked: source.checked || undefined, onchange: e => { $(source).prop('checked', e.target.checked).trigger(event); }}),
            el('span', {text: label}), hint ? el('small', {text: hint}) : null));
    }
    box.append(checks);
    return box;
}

// ------------------------------------------------------------- one book --

const POSITIONS = [[0, '↑ Before character'], [1, '↓ After character'], [5, '↑ Before examples'], [6, '↓ After examples'],
    [2, '↑ Top of Author’s Note'], [3, '↓ Bottom of Author’s Note'], [4, '@ At depth'], [7, 'Outlet']];
const ROLES = [[0, 'System'], [1, 'User'], [2, 'Assistant']];
const LOGIC = [[0, 'AND ANY'], [3, 'AND ALL'], [1, 'NOT ALL'], [2, 'NOT ANY']];
const TRIGGERS = [['normal', 'Normal'], ['continue', 'Continue'], ['impersonate', 'Impersonate'], ['swipe', 'Swipe'], ['regenerate', 'Regenerate'], ['quiet', 'Quiet']];
const MATCHES = [['matchPersonaDescription', 'Persona description'], ['matchCharacterDescription', 'Character description'], ['matchCharacterPersonality', 'Character personality'],
    ['matchCharacterDepthPrompt', 'Character’s note'], ['matchScenario', 'Scenario'], ['matchCreatorNotes', 'Creator’s notes']];
const strategyOf = entry => entry.constant ? 'constant' : entry.vectorized ? 'vectorized' : 'normal';
const STRATEGIES = [['normal', '🟢 Keywords'], ['constant', '🔵 Always on'], ['vectorized', '🔗 Vectorized']];
const splitKeys = value => String(value || '').split(',').map(k => k.trim()).filter(Boolean);
const triState = value => (value === true ? 'yes' : value === false ? 'no' : '');
const fromTri = value => (value === 'yes' ? true : value === 'no' ? false : null);
const numberOrNull = value => (value === '' || value === null || value === undefined || Number.isNaN(Number(value)) ? null : Number(value));

let loreSaveTimer = null;
function setLoreStatus(text) {
    lore.status = text;
    const node = $q('#rvo-lorebooks .rvo-lstatus');
    if (node) node.textContent = text;
}
/** Save the open book (SillyTavern's own debounced save). */
function saveLorebook() {
    if (!lore.book || !lore.data || !wiModule) return;
    wiModule.saveWorldInfo(lore.book, lore.data);
    loreEntries.set(lore.book, Object.keys(lore.data.entries || {}).length);
    drawLoreStats();
    setLoreStatus('Saving…');
    clearTimeout(loreSaveTimer);
    loreSaveTimer = setTimeout(() => setLoreStatus('All changes saved'), 1500);
}
async function flushLorebook() {
    if (!lore.book || !lore.data || !wiModule || !loreSaveTimer) return;
    clearTimeout(loreSaveTimer); loreSaveTimer = null;
    await wiModule.saveWorldInfo(lore.book, lore.data, true);
    setLoreStatus('All changes saved');
}

async function renderLorebook(root, name) {
    const wi = await worldInfo();
    if (!lore.data) {
        root.replaceChildren(el('div', {class: 'rvo-cwrap rvo-lwrap'}, el('p', {class: 'rvo-empty'}, icon('fa-spinner fa-spin'), el('span', {text: ' Loading…'}))));
        const data = await wi.loadWorldInfo(name);
        if (lore.book !== name) return;
        lore.data = data && typeof data === 'object' ? data : {entries: {}};
        lore.data.entries ||= {};
    }
    const source = globalThis.RocsyAddons?.importedLorebookSource?.(name) || '';
    const entriesBox = el('div', {class: 'rvo-lentries'});
    const search = el('input', {type: 'search', class: 'rvo-cinput', placeholder: 'Search entries: title, keywords, text', value: lore.eq,
        oninput: event => { lore.eq = event.target.value.trim(); drawEntries(entriesBox); }});
    const sort = el('select', {class: 'rvo-cinput rvo-lsort', 'aria-label': 'Sort entries', onchange: event => { lore.esort = event.target.value; drawEntries(entriesBox); }},
        ...[['order', 'Order'], ['title', 'Title A–Z'], ['uid', 'Newest'], ['tokens', 'Longest']].map(([v, t]) => el('option', {value: v, text: t, selected: lore.esort === v || undefined})));
    root.replaceChildren(el('div', {class: 'rvo-cwrap rvo-lwrap rvo-lbookpage'},
        el('div', {class: 'rvo-lbar'},
            el('button', {class: 'rvo-cbtn', type: 'button', onclick: () => { renderLorebooksPage(pageNode('lorebooks'), {book: ''}); navPush(); }},
                icon('fa-arrow-left'), el('span', {text: 'All lorebooks'})),
            el('span', {class: 'rvo-lstatus', text: lore.status}),
            el('div', {class: 'rvo-lbar-actions'},
                el('button', {class: 'rvo-icon-button', type: 'button', title: 'Rename', onclick: () => renameLorebook(name).catch(e => toastr.error(e.message))}, icon('fa-i-cursor')),
                el('button', {class: 'rvo-icon-button', type: 'button', title: 'Duplicate', onclick: () => duplicateLorebook(name).catch(e => toastr.error(e.message))}, icon('fa-clone')),
                el('button', {class: 'rvo-icon-button', type: 'button', title: 'Export (download the file)', onclick: () => exportLorebook(name).catch(e => toastr.error(e.message))}, icon('fa-file-export')),
                el('button', {class: 'rvo-icon-button rvo-ldelete', type: 'button', title: 'Delete the lorebook', onclick: () => deleteLorebook(name)}, icon('fa-trash-can')))),
        el('div', {class: 'rvo-lhead'},
            el('div', {class: 'rvo-lhero'},
                bookIcon(name, true),
                el('div', {class: 'rvo-lhero-text'}, el('h2', {text: name}),
                    el('small', {class: 'rvo-lmeta'}, el('span', {class: 'rvo-lhero-count', text: entriesText(Object.keys(lore.data.entries).length)}),
                        source ? el('span', {text: `from ${source.startsWith('chub') ? 'Chub' : 'Bot Booru'}`}) : null),
                    el('div', {class: 'rvo-lstats'}))),
            el('section', {class: 'rvo-lusage'})),
        el('div', {class: 'rvo-ctools rvo-ltools'}, search, sort,
            rvoButton('Add entry', 'fa-plus', () => addEntry(entriesBox), 'Add an entry', 'primary'),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Fold all', onclick: () => { lore.open.clear(); drawEntries(entriesBox); }}, icon('fa-compress')),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Unfold all', onclick: () => { for (const uid of Object.keys(lore.data.entries)) lore.open.add(String(uid)); drawEntries(entriesBox); }}, icon('fa-expand'))),
        entriesBox));
    drawUsage();
    drawEntries(entriesBox);
    drawLoreStats();
    renderHeader();
}

/** Entry counts on a book's page: all, on, always on, by keywords, vectorized. */
function drawLoreStats() {
    const box = $q('#rvo-lorebooks .rvo-lstats');
    if (!box || !lore.data) return;
    const entries = Object.values(lore.data.entries || {});
    const on = entries.filter(e => !e.disable);
    box.replaceChildren(statTiles([['Entries', entries.length, 'fa-list'], ['On', on.length, 'fa-toggle-on'],
        ['Always on', on.filter(e => e.constant).length, 'fa-thumbtack'], ['By keywords', on.filter(e => !e.constant && !e.vectorized).length, 'fa-key'],
        ['Vectorized', on.filter(e => e.vectorized).length, 'fa-diagram-project']]));
}

/** "Where it's used": all chats, this chat, characters (primary lorebook). */
function drawUsage() {
    const box = $q('#rvo-lorebooks .rvo-lusage');
    const name = lore.book;
    if (!box || !name) return;
    const global = (wiModule?.selected_world_info || []).includes(name);
    const chatOpen = !!(ctx().chatId || ctx().getCurrentChatId?.());
    const users = lorebookUsers(name);
    const others = ctx().characters.filter(c => c?.avatar && !users.includes(c)).sort((a, b) => a.name.localeCompare(b.name));
    const linker = el('select', {class: 'rvo-cinput', 'aria-label': 'Link to a character', onchange: async event => {
        const avatar = event.target.value;
        if (!avatar) return;
        const character = ctx().characters.find(c => c.avatar === avatar);
        const old = character?.data?.extensions?.world;
        if (old && old !== name && !await ctx().callGenericPopup(`${character.name} already uses “${old}”. Use “${name}” instead?`, ctx().POPUP_TYPE.CONFIRM)) { event.target.value = ''; return; }
        try { await setCharacterLorebook(avatar, name); toastr.success(`${character.name} now uses ${name}`, 'Lorebooks'); } catch (error) { toastr.error(error.message); }
        drawUsage();
    }}, el('option', {value: '', text: '+ Link to a character…'}), ...others.map(c => el('option', {value: c.avatar, text: c.name})));
    const lores = wiModule?.world_info?.charLore || [];
    box.replaceChildren(
        el('h3', {}, icon('fa-link'), el('span', {text: 'Where it’s used'})),
        el('div', {class: 'rvo-lusage-row'},
            switchControl(el('input', {type: 'checkbox', checked: global || undefined, onchange: event => { setGlobalLorebook(name, event.target.checked); }}), 'All chats', 'Use this lorebook in every chat'),
            switchControl(el('input', {type: 'checkbox', checked: chatLorebook() === name || undefined, disabled: !chatOpen || undefined,
                onchange: async event => {
                    const current = chatLorebook();
                    if (event.target.checked && current && current !== name && !await ctx().callGenericPopup(`This chat already uses “${current}”. Use “${name}” instead?`, ctx().POPUP_TYPE.CONFIRM)) { event.target.checked = false; return; }
                    await setChatLorebook(name, event.target.checked); drawUsage();
                }}), 'This chat', chatOpen ? 'Use this lorebook in the open chat only' : 'Open a chat first')),
        el('div', {class: 'rvo-lchars'},
            ...users.map(c => {
                const primary = c.data?.extensions?.world === name;
                return el('span', {class: 'rvo-lchip', title: primary ? `${c.name}’s lorebook` : `One of ${c.name}’s extra lorebooks`},
                    el('img', {src: `/thumbnail?type=avatar&file=${encodeURIComponent(c.avatar)}`, alt: '', loading: 'lazy'}),
                    el('span', {text: primary ? c.name : `${c.name} (extra)`}),
                    el('button', {class: 'rvo-lchip-x', type: 'button', title: `Unlink ${c.name}`, onclick: async () => {
                        try {
                            if (primary) await setCharacterLorebook(c.avatar, '');
                            else {
                                const entry = lores.find(l => l.name === charFile(c.avatar));
                                if (entry) { entry.extraBooks = entry.extraBooks.filter(b => b !== name); ctx().saveSettingsDebounced(); }
                            }
                        } catch (error) { toastr.error(error.message); }
                        drawUsage();
                    }}, icon('fa-xmark')));
            }),
            others.length ? linker : null));
}

function sortedEntries() {
    const entries = Object.values(lore.data.entries || {});
    const q = lore.eq.toLowerCase();
    const shown = q ? entries.filter(e => `${e.comment || ''} ${(e.key || []).join(' ')} ${(e.keysecondary || []).join(' ')} ${e.content || ''}`.toLowerCase().includes(q)) : entries;
    const by = {
        order: (a, b) => (b.order ?? 100) - (a.order ?? 100) || a.uid - b.uid,
        title: (a, b) => entryTitle(a).localeCompare(entryTitle(b)),
        uid: (a, b) => b.uid - a.uid,
        tokens: (a, b) => (b.content || '').length - (a.content || '').length,
    }[lore.esort] || ((a, b) => a.uid - b.uid);
    return shown.sort(by);
}
const entryTitle = entry => entry.comment || (entry.key || []).slice(0, 3).join(', ') || `Entry ${entry.uid}`;

function drawEntries(box) {
    const list = sortedEntries();
    const total = Object.keys(lore.data.entries || {}).length;
    const count = $q('#rvo-lorebooks .rvo-lhero-count');
    if (count) count.textContent = entriesText(total);
    if (!total) {
        box.replaceChildren(el('div', {class: 'rvo-lempty'}, icon('fa-feather'), el('strong', {text: 'No entries yet'}),
            el('span', {text: 'An entry is a piece of lore and the keywords that bring it into the chat.'}),
            rvoButton('Add the first entry', 'fa-plus', () => addEntry(box), 'Add an entry', 'primary')));
        return;
    }
    const shown = list.slice(0, lore.limit || 60);
    box.replaceChildren(...[
        list.length !== total ? el('p', {class: 'rvo-lnote', text: `${list.length} of ${total} entries`}) : null,
        ...(list.length ? shown.map(entry => entryCard(entry, box)) : [el('p', {class: 'rvo-empty', text: 'No entries match.'})]),
        list.length > shown.length ? rvoButton(`Show ${Math.min(60, list.length - shown.length)} more`, 'fa-angles-down', () => { lore.limit = (lore.limit || 60) + 60; drawEntries(box); }) : null,
    ].filter(Boolean));
}

function addEntry(box) {
    if (!wiModule?.createWorldInfoEntry) return;
    const entry = wiModule.createWorldInfoEntry(lore.book, lore.data);
    if (!entry) return toastr.error('Couldn’t add an entry.');
    entry.comment = 'New entry';
    lore.open.add(String(entry.uid));
    lore.eq = '';
    lore.esort = 'uid';
    saveLorebook();
    const sort = $q('#rvo-lorebooks .rvo-lsort');
    if (sort) sort.value = 'uid';
    const search = $q('#rvo-lorebooks .rvo-ltools input[type=search]');
    if (search) search.value = '';
    drawEntries(box);
    box.querySelector(`[data-uid="${entry.uid}"] .rvo-le-title`)?.focus();
}

function entryCard(entry, box) {
    const uid = String(entry.uid);
    const open = lore.open.has(uid);
    const set = (key, value, redrawHead = false) => {
        if (value === null || value === undefined) delete entry[key]; else entry[key] = value;
        saveLorebook();
        if (redrawHead) card.querySelector('.rvo-le-head')?.replaceWith(head());
    };
    const head = () => el('div', {class: 'rvo-le-head'},
        switchControl(el('input', {type: 'checkbox', checked: !entry.disable || undefined, onchange: event => { set('disable', !event.target.checked); card.classList.toggle('off', !!entry.disable); }}), '', entry.disable ? 'Switched off' : 'On'),
        el('button', {class: 'rvo-le-toggle', type: 'button', 'aria-expanded': String(open), onclick: () => { open ? lore.open.delete(uid) : lore.open.add(uid); card.replaceWith(entryCard(entry, box)); }},
            el('span', {class: `rvo-le-strategy ${strategyOf(entry)}`, title: STRATEGIES.find(([v]) => v === strategyOf(entry))[1]}),
            el('span', {class: 'rvo-le-name'}, el('strong', {text: entryTitle(entry)}),
                el('small', {text: entry.constant ? 'Always on' : (entry.key || []).length ? (entry.key || []).join(', ') : 'No keywords yet'})),
            el('span', {class: 'rvo-le-pos', text: `${(POSITIONS.find(([v]) => v === Number(entry.position)) || POSITIONS[0])[1].replace(/^[↑↓@] /, '')}${Number(entry.position) === 4 ? ` ${entry.depth ?? 4}` : ''} · ${entry.order ?? 100}`}),
            icon(open ? 'fa-chevron-up' : 'fa-chevron-down')));
    const card = el('article', {class: `rvo-lentry${entry.disable ? ' off' : ''}${open ? ' open' : ''}`, 'data-uid': uid}, head());
    if (!open) return card;

    const field = (label, control, hint, wide = false) => el('label', {class: `rvo-lfield${wide ? ' wide' : ''}`}, el('span', {text: label}), control, hint ? el('small', {text: hint}) : null);
    const text = (value, onchange, attrs = {}) => el('input', {type: 'text', class: 'rvo-cinput', value: value ?? '', oninput: event => onchange(event.target.value), ...attrs});
    const num = (value, onchange, attrs = {}) => el('input', {type: 'number', class: 'rvo-cinput', value: value ?? '', onchange: event => onchange(event.target.value), ...attrs});
    const choose = (options, value, onchange) => el('select', {class: 'rvo-cinput', onchange: event => onchange(event.target.value)},
        ...options.map(([v, t]) => el('option', {value: String(v), text: t, selected: String(v) === String(value) || undefined})));
    const tri = (value, onchange) => choose([['', 'Use global'], ['yes', 'Yes'], ['no', 'No']], triState(value), v => onchange(fromTri(v)));
    const check = (label, checked, onchange, hint) => el('label', {class: 'rvo-lcheck', title: hint || ''}, el('input', {type: 'checkbox', checked: checked || undefined, onchange: event => onchange(event.target.checked)}), el('span', {text: label}));

    const content = el('textarea', {class: 'rvo-cinput rvo-le-content', rows: 6, placeholder: 'What the AI should know when this entry fires…',
        oninput: event => { set('content', event.target.value); grow(event.target); tokens(); }});
    content.value = entry.content || '';
    const grow = area => { area.style.height = 'auto'; area.style.height = `${Math.min(area.scrollHeight + 2, 600)}px`; };
    requestAnimationFrame(() => grow(content));
    const tokenLabel = el('small', {class: 'rvo-le-tokens'});
    let tokenTimer = null;
    const tokens = () => { clearTimeout(tokenTimer); tokenTimer = setTimeout(async () => {
        try { tokenLabel.textContent = `${await ctx().getTokenCountAsync(entry.content || '')} tokens`; } catch { tokenLabel.textContent = `~${Math.round((entry.content || '').length / 4)} tokens`; }
    }, 400); };
    tokens();

    const positionExtra = el('div', {class: 'rvo-le-posx'});
    const drawPositionExtra = () => positionExtra.replaceChildren(...(Number(entry.position) === 4
        ? [field('Depth', num(entry.depth ?? 4, v => set('depth', Math.max(0, Number(v) || 0)), {min: 0, max: 1000})),
            field('Role', choose(ROLES, entry.role ?? 0, v => set('role', Number(v))))]
        : Number(entry.position) === 7 ? [field('Outlet name', text(entry.outletName, v => set('outletName', v.trim())), 'Shown where {{outlet::name}} is used.')] : []));
    drawPositionExtra();

    const characters = ctx().characters.filter(c => c?.avatar).sort((a, b) => a.name.localeCompare(b.name));
    const filter = entry.characterFilter || {isExclude: false, names: [], tags: []};
    const setFilter = () => set('characterFilter', (filter.names.length || filter.tags.length || filter.isExclude) ? {...filter} : null);

    card.append(el('div', {class: 'rvo-le-body'},
        el('div', {class: 'rvo-le-grid'},
            field('Title / memo', text(entry.comment, v => set('comment', v), {class: 'rvo-cinput rvo-le-title', placeholder: 'A name for this entry'}), '', true),
            field('Keywords', text((entry.key || []).join(', '), v => set('key', splitKeys(v))), 'Comma separated. /regex/ works too.', true),
            field('Optional filter', text((entry.keysecondary || []).join(', '), v => { set('keysecondary', splitKeys(v)); set('selective', true); }), 'Also needs these keywords (or not, by the logic).'),
            field('Filter logic', choose(LOGIC, entry.selectiveLogic ?? 0, v => set('selectiveLogic', Number(v))))),
        el('label', {class: 'rvo-lfield wide'}, el('span', {class: 'rvo-le-contenthead'}, el('span', {text: 'Content'}), tokenLabel), content),
        el('div', {class: 'rvo-le-grid'},
            field('Strategy', choose(STRATEGIES, strategyOf(entry), v => { entry.constant = v === 'constant'; entry.vectorized = v === 'vectorized'; saveLorebook(); card.querySelector('.rvo-le-head')?.replaceWith(head()); }),
                'Keywords: fires on its keywords. Always on: every message. Vectorized: by meaning (Vector Storage).'),
            field('Position', choose(POSITIONS, entry.position ?? 0, v => { set('position', Number(v), true); drawPositionExtra(); })),
            positionExtra,
            field('Order', num(entry.order ?? 100, v => set('order', Number(v) || 0, true)), 'Higher goes later (closer to the end).'),
            field('Trigger %', num(entry.useProbability === false ? 100 : entry.probability ?? 100, v => { set('probability', Math.min(100, Math.max(0, Number(v) || 0))); set('useProbability', true); }, {min: 0, max: 100}), 'Chance it fires when triggered.')),
        el('details', {class: 'rvo-le-more'},
            el('summary', {text: 'More options'}),
            el('div', {class: 'rvo-le-grid'},
                field('Scan depth', num(entry.scanDepth ?? '', v => set('scanDepth', numberOrNull(v)), {min: 0, max: 1000, placeholder: 'global'})),
                field('Case-sensitive', tri(entry.caseSensitive, v => set('caseSensitive', v))),
                field('Whole words', tri(entry.matchWholeWords, v => set('matchWholeWords', v))),
                field('Group scoring', tri(entry.useGroupScoring, v => set('useGroupScoring', v))),
                field('Inclusion group', text(entry.group, v => set('group', v.trim())), 'Entries in a group: only one fires.'),
                field('Group weight', num(entry.groupWeight ?? 100, v => set('groupWeight', Number(v) || 0), {min: 1})),
                field('Sticky', num(entry.sticky ?? '', v => set('sticky', numberOrNull(v)), {min: 0, placeholder: '0'}), 'Stays on for this many messages.'),
                field('Cooldown', num(entry.cooldown ?? '', v => set('cooldown', numberOrNull(v)), {min: 0, placeholder: '0'}), 'Can’t fire again for this many messages.'),
                field('Delay', num(entry.delay ?? '', v => set('delay', numberOrNull(v)), {min: 0, placeholder: '0'}), 'Only after this many messages in the chat.'),
                field('Delay until recursion', num(entry.delayUntilRecursion || '', v => set('delayUntilRecursion', Number(v) || 0), {min: 0, placeholder: 'off'}), 'Only fires on a recursion step (level).'),
                field('Automation ID', text(entry.automationId, v => set('automationId', v.trim())), 'Runs this Quick Reply automation when it fires.')),
            el('div', {class: 'rvo-le-checks'},
                check('Prioritize in group', entry.groupOverride, v => set('groupOverride', v)),
                check('Ignore budget', entry.ignoreBudget, v => set('ignoreBudget', v)),
                check('Non-recursable', entry.excludeRecursion, v => set('excludeRecursion', v), 'Other entries can’t trigger it.'),
                check('Prevent further recursion', entry.preventRecursion, v => set('preventRecursion', v), 'Its content won’t trigger other entries.')),
            el('div', {class: 'rvo-le-sub'}, el('strong', {text: 'Also match in'}),
                el('div', {class: 'rvo-le-checks'}, ...MATCHES.map(([key, label]) => check(label, entry[key], v => set(key, v))))),
            el('div', {class: 'rvo-le-sub'}, el('strong', {text: 'Only on these generation types'}), el('small', {text: 'None ticked = all of them.'}),
                el('div', {class: 'rvo-le-checks'}, ...TRIGGERS.map(([key, label]) => check(label, (entry.triggers || []).includes(key), v => {
                    const list = new Set(entry.triggers || []); v ? list.add(key) : list.delete(key); set('triggers', [...list]);
                })))),
            el('div', {class: 'rvo-le-sub'}, el('strong', {text: 'Characters'}),
                el('div', {class: 'rvo-le-checks'},
                    check('Exclude these instead', filter.isExclude, v => { filter.isExclude = v; setFilter(); })),
                el('div', {class: 'rvo-le-checks rvo-le-charlist'}, ...characters.map(c => check(c.name, filter.names.includes(charFile(c.avatar)), v => {
                    const names = new Set(filter.names); v ? names.add(charFile(c.avatar)) : names.delete(charFile(c.avatar)); filter.names = [...names]; setFilter();
                }))),
                (ctx().tags || []).length ? el('div', {class: 'rvo-le-checks'}, el('small', {text: 'Tags:'}), ...(ctx().tags || []).map(t => check(t.name, (filter.tags || []).includes(t.id), v => {
                    const tags = new Set(filter.tags || []); v ? tags.add(t.id) : tags.delete(t.id); filter.tags = [...tags]; setFilter();
                }))) : null,
                el('small', {text: 'None ticked = every character.'}))),
        el('div', {class: 'rvo-le-actions'},
            moveSelect(entry, box),
            rvoButton('Duplicate', 'fa-clone', () => {
                const copy = wiModule.createWorldInfoEntry(lore.book, lore.data);
                if (!copy) return;
                const {uid: newUid} = copy;
                Object.assign(copy, structuredClone(entry), {uid: newUid, comment: `${entry.comment || entryTitle(entry)} (copy)`});
                lore.open.add(String(newUid));
                saveLorebook(); drawEntries(box);
            }),
            rvoButton('Delete', 'fa-trash-can', async () => {
                if (!await ctx().callGenericPopup(`Delete “${entryTitle(entry)}”?`, ctx().POPUP_TYPE.CONFIRM)) return;
                delete lore.data.entries[uid];
                lore.open.delete(uid);
                saveLorebook(); drawEntries(box);
            }, 'Delete this entry', 'danger'))));
    return card;
}

/** Move an entry to another lorebook (copied there with a new id, then removed here). */
function moveSelect(entry, box) {
    const others = (wiModule?.world_names || []).filter(n => n !== lore.book);
    if (!others.length) return null;
    return el('select', {class: 'rvo-cinput rvo-le-move', 'aria-label': 'Move to another lorebook', onchange: async event => {
        const target = event.target.value;
        if (!target) return;
        try {
            const data = await wiModule.loadWorldInfo(target);
            const copy = wiModule.createWorldInfoEntry(target, data);
            const {uid: newUid} = copy;
            Object.assign(copy, structuredClone(entry), {uid: newUid});
            await wiModule.saveWorldInfo(target, data, true);
            delete lore.data.entries[String(entry.uid)];
            loreEntries.delete(target);
            saveLorebook(); drawEntries(box);
            toastr.success(`Moved to ${target}`, 'Lorebooks');
        } catch (error) { toastr.error(error.message, 'Move'); event.target.value = ''; }
    }}, el('option', {value: '', text: 'Move to…'}), ...others.map(n => el('option', {value: n, text: n})));
}

// -------------------------------------------------------------- characters --
// A gallery of the characters (picture, name, tags, description) with search, tag filters and sorting; a card
// opens its page: the full description, tags, first message, chats, voice, and Chat / New chat / Edit.

const chars = {view: 'grid', avatar: '', q: '', tags: new Set(), exclude: new Set(), sort: 'recent', tagQ: '', more: new Set(), filtersOpen: false};

const characterImage = avatar => `/characters/${encodeURIComponent(avatar)}`;
/** SillyTavern's tags for a character, plus the tags written into its card (imported cards bring those). */
function tagsOf(character) {
    const {tags, tagMap} = ctx();
    const list = (tagMap?.[character.avatar] || []).map(id => tags.find(t => t.id === id)).filter(Boolean);
    const seen = new Set(list.map(t => t.name.toLowerCase()));
    const own = Array.isArray(character.data?.tags) ? character.data.tags : Array.isArray(character.tags) ? character.tags : [];
    for (const name of own) {
        const clean = String(name || '').trim();
        if (clean && !seen.has(clean.toLowerCase())) { seen.add(clean.toLowerCase()); list.push({id: `card:${clean.toLowerCase()}`, name: clean}); }
    }
    return list;
}
const tagChip = (tag, extra = {}) => el('span', {class: 'rvo-tag', text: tag.name, ...extra,
    style: tag.color ? `--tag-bg: ${tag.color}` : undefined});
const blurb = character => String(character.data?.creator_notes || character.creatorcomment || character.description || '')
    .replace(/\{\{char\}\}/gi, character.name).replace(/\{\{user\}\}/gi, ctx().name1 || 'you').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
const lastChatTime = character => {
    const item = recents.find(r => r.avatar === character.avatar);
    return item?.when || Number(character.date_last_chat) || 0;
};

async function chatWith(avatar, {fresh = false} = {}) {
    const c = ctx();
    const index = c.characters.findIndex(x => x.avatar === avatar);
    if (index < 0) return toastr.error('That character no longer exists.');
    hidePage();
    if (c.groupId || String(c.characterId) !== String(index)) await c.selectCharacterById(index);
    if (fresh) $q('#option_start_new_chat')?.click();
    closeSidebarOnMobile();
}

function characterCard(character) {
    const tags = tagsOf(character);
    const text = blurb(character);
    return el('article', {class: 'rvo-ccard'},
        el('button', {class: 'rvo-ccard-open', type: 'button', title: `About ${character.name}`,
            onclick: () => showPage('characters', {avatar: character.avatar})},
            el('span', {class: 'rvo-ccard-pic'}, el('img', {src: characterImage(character.avatar), alt: '', loading: 'lazy', decoding: 'async'})),
            el('span', {class: 'rvo-ccard-body'},
                el('strong', {class: 'rvo-ccard-name', text: character.name}),
                text ? el('span', {class: 'rvo-ccard-text', text}) : null,
                tags.length ? el('span', {class: 'rvo-tags'}, ...tags.slice(0, 4).map(t => tagChip(t))) : null)),
        el('div', {class: 'rvo-ccard-actions'},
            el('button', {class: 'rvo-cbtn primary', type: 'button', onclick: () => chatWith(character.avatar)}, icon('fa-comments'), el('span', {text: 'Chat'})),
            el('button', {class: 'rvo-icon-button rvo-newchat', type: 'button', title: `Start a new chat with ${character.name}`, 'aria-label': `Start a new chat with ${character.name}`, onclick: () => chatWith(character.avatar, {fresh: true})}, icon('fa-comment-medical')),
            el('button', {class: 'rvo-icon-button', type: 'button', title: `${character.name}’s settings`, onclick: () => showPage('characters', {avatar: character.avatar})}, icon('fa-gear'))));
}

// AO3-style tag sidebar: tags in groups (Ratings, Archive Warnings, Categories, Fandoms, Characters, Relationships,
// Additional Tags, then Content, Traits, SillyTavern features and everything else), each tag with how many of the
// characters shown have it; tick to include (a character needs every included tag), the ban button excludes.
// Only tags the current results use are listed, so narrowing down stays easy.
const TAG_GROUPS = [['rating', 'Ratings'], ['warning', 'Archive Warnings'], ['category', 'Categories'], ['fandom', 'Fandoms'],
    ['character', 'Characters'], ['relationship', 'Relationships'], ['freeform', 'Additional Tags'], ['content', 'Content'],
    ['danbooru', 'Traits'], ['st', 'SillyTavern Features'], ['other', 'Other Tags']];
const KIND_GROUP = {Rating: 'rating', Warning: 'warning', Category: 'category', Fandom: 'fandom', Relationship: 'relationship',
    Character: 'character', Tag: 'freeform', Content: 'content', Danbooru: 'danbooru', SillyTavern: 'st'};
const AO3_WARNINGS = new Set(['No Archive Warnings Apply', 'Graphic Depictions Of Violence', 'Major Character Death', 'Rape/Non-Con',
    'Underage', 'Creator Chose Not To Use Archive Warnings'].map(name => name.toLowerCase()));
const AO3_CATEGORIES = new Set(['f/m', 'm/m', 'f/f', 'gen', 'multi', 'other']);
const CONTENT_TAGS = new Set(['sfw', 'nsfw', 'dead dove: do not eat', 'dead dove']);
const FANDOM_NAME = /\((video game|anime|manga|tv|movie|film|comics?|cartoon|books?|novel|light novel|web ?series|webcomic|visual novel|game)[^)]*\)\s*$|^original work$/i;

function tagGroup(tag) {
    const kind = settings.autotag?.kinds?.[tag.id];
    if (KIND_GROUP[kind]) return KIND_GROUP[kind];
    const name = String(tag.name || '').trim(), lower = name.toLowerCase();
    if (/^rating: /i.test(name)) return 'rating';
    if (AO3_WARNINGS.has(lower)) return 'warning';
    if (AO3_CATEGORIES.has(lower)) return 'category';
    if (CONTENT_TAGS.has(lower) || tag.color === TAG_COLORS.content) return 'content';
    if (/^st: /i.test(name) || tag.color === TAG_COLORS.st) return 'st';
    if (tag.color === TAG_COLORS.danbooru) return 'danbooru';
    if (FANDOM_NAME.test(name)) return 'fandom';
    if (tag.color === TAG_COLORS.ao3) return /\/| & /.test(name) && !/^(hurt\/comfort|fluff\/angst)$/i.test(name) ? 'relationship' : 'freeform';
    return 'other';
}
const tagLabel = tag => String(tag.name || '').replace(/^Rating: /i, '');

/** The gallery's characters after search and tag filters (exclude, include = every tag, AO3's way). */
function galleryList() {
    const c = ctx();
    const q = chars.q.toLowerCase();
    let list = c.characters.filter(ch => ch && ch.avatar && !(ch.avatar === 'default_Assistant.png' && !lastChatTime(ch)));
    if (q) list = list.filter(ch => `${ch.name} ${blurb(ch)} ${tagsOf(ch).map(t => t.name).join(' ')}`.toLowerCase().includes(q));
    if (chars.tags.size || chars.exclude.size) list = list.filter(ch => {
        const ids = new Set(tagsOf(ch).map(t => t.id));
        return [...chars.tags].every(id => ids.has(id)) && ![...chars.exclude].some(id => ids.has(id));
    });
    return list;
}

function drawGallery(root) {
    const c = ctx();
    const list = galleryList();
    const by = {
        recent: (a, b) => lastChatTime(b) - lastChatTime(a) || a.name.localeCompare(b.name),
        name: (a, b) => a.name.localeCompare(b.name),
        added: (a, b) => (Number(b.create_date ? Date.parse(b.create_date) : 0) || 0) - (Number(a.create_date ? Date.parse(a.create_date) : 0) || 0),
    };
    list.sort(by[chars.sort] || by.recent);
    const filtering = chars.q || chars.tags.size || chars.exclude.size;
    const grid = root.querySelector('.rvo-cgrid');
    grid.replaceChildren(...(list.length ? list.map(characterCard) : [el('p', {class: 'rvo-empty', text: filtering ? 'No characters match.' : 'No characters yet: import some from the Library.'})]));
    root.querySelector('.rvo-ccount').textContent = `${list.length} of ${c.characters.length}`;
    drawActiveFilters(root);
    drawTagSidebar(root, list);
}

/** Every tag in use, by id (SillyTavern tags + tags written into the cards). */
function allTags() {
    const found = new Map();
    for (const ch of ctx().characters) if (ch?.avatar) for (const tag of tagsOf(ch)) if (!found.has(tag.id)) found.set(tag.id, tag);
    return found;
}

function toggleTagFilter(root, id, mode) {
    const [on, off] = mode === 'exclude' ? [chars.exclude, chars.tags] : [chars.tags, chars.exclude];
    off.delete(id);
    on.has(id) ? on.delete(id) : on.add(id);
    drawGallery(root);
}

function drawActiveFilters(root) {
    const box = root.querySelector('.rvo-cactive');
    const known = allTags();
    const chip = (id, mode) => {
        const tag = known.get(id) || {id, name: id.replace(/^card:/, '')};
        return el('button', {class: `rvo-tag rvo-cactive-tag${mode === 'exclude' ? ' excluded' : ''}`, type: 'button',
            title: `${mode === 'exclude' ? 'Stop excluding' : 'Stop filtering by'} ${tag.name}`, style: tag.color ? `--tag-bg: ${tag.color}` : undefined,
            onclick: () => toggleTagFilter(root, id, mode)}, mode === 'exclude' ? icon('fa-ban') : null, el('span', {text: tagLabel(tag)}), icon('fa-xmark'));
    };
    const items = [...[...chars.tags].map(id => chip(id, 'include')), ...[...chars.exclude].map(id => chip(id, 'exclude'))];
    box.hidden = !items.length;
    box.replaceChildren(...(items.length ? [...items, el('button', {class: 'rvo-macro', type: 'button', text: 'Clear all',
        onclick: () => { chars.tags.clear(); chars.exclude.clear(); drawGallery(root); }})] : []));
    const badge = root.querySelector('.rvo-cfilter-count');
    if (badge) { badge.textContent = items.length ? String(items.length) : ''; badge.hidden = !items.length; }
}

function drawTagSidebar(root, list) {
    const box = root.querySelector('.rvo-cfilter-groups');
    if (!box) return;
    const counts = new Map(), known = allTags();
    for (const ch of list) for (const tag of tagsOf(ch)) counts.set(tag.id, (counts.get(tag.id) || 0) + 1);
    const find = chars.tagQ.toLowerCase();
    const groups = new Map(TAG_GROUPS.map(([id]) => [id, []]));
    for (const [id, tag] of known) {
        const picked = chars.tags.has(id) || chars.exclude.has(id);
        if (!counts.get(id) && !picked) continue;               // not in these results
        if (find && !tag.name.toLowerCase().includes(find) && !picked) continue;
        groups.get(tagGroup(tag)).push(tag);
    }
    const closed = settings.tagGroupsClosed || {};
    const sections = TAG_GROUPS.filter(([id]) => groups.get(id).length).map(([id, title]) => {
        const tags = groups.get(id).sort((a, b) => (counts.get(b.id) || 0) - (counts.get(a.id) || 0) || a.name.localeCompare(b.name));
        const limit = find || chars.more.has(id) ? tags.length : 8;
        // Picked tags always show, even past the limit.
        const shown = tags.filter((tag, i) => i < limit || chars.tags.has(tag.id) || chars.exclude.has(tag.id));
        const rows = shown.map(tag => {
            const included = chars.tags.has(tag.id), excluded = chars.exclude.has(tag.id);
            const box = el('input', {type: 'checkbox', onchange: () => toggleTagFilter(root, tag.id, 'include')});
            box.checked = included;
            return el('li', {class: `rvo-cfilter-tag${excluded ? ' excluded' : ''}`},
                el('label', {title: excluded ? `Excluded: ${tag.name}` : `Only characters tagged ${tag.name}`}, box,
                    el('span', {class: 'rvo-cfilter-dot', style: tag.color ? `--tag-bg: ${tag.color}` : undefined}),
                    el('span', {class: 'rvo-cfilter-name', text: tagLabel(tag)}),
                    el('small', {class: 'rvo-cfilter-n', text: `(${counts.get(tag.id) || 0})`})),
                el('button', {class: `rvo-cfilter-ex${excluded ? ' on' : ''}`, type: 'button', 'aria-pressed': String(excluded),
                    title: excluded ? `Stop excluding ${tag.name}` : `Hide characters tagged ${tag.name}`,
                    onclick: () => toggleTagFilter(root, tag.id, 'exclude')}, icon('fa-ban')));
        });
        const details = el('details', {class: 'rvo-cfilter-group'},
            el('summary', {}, el('span', {text: title}), el('small', {class: 'rvo-cfilter-n', text: String(tags.length)})),
            el('ul', {}, ...rows),
            tags.length > limit ? el('button', {class: 'rvo-cfilter-more', type: 'button', text: `Show all ${tags.length}`,
                onclick: () => { chars.more.add(id); drawGallery(root); }})
                : chars.more.has(id) && tags.length > 8 && !find ? el('button', {class: 'rvo-cfilter-more', type: 'button', text: 'Show fewer',
                    onclick: () => { chars.more.delete(id); drawGallery(root); }}) : null);
        details.open = find ? true : !closed[id];
        details.addEventListener('toggle', () => {
            if (find) return;
            settings.tagGroupsClosed = {...(settings.tagGroupsClosed || {}), [id]: !details.open};
            save();
        });
        return details;
    });
    box.replaceChildren(...(sections.length ? sections : [el('p', {class: 'rvo-muted rvo-cfilter-empty', text: find ? 'No tags match.' : 'No tags on these characters yet. Autotag can add some.'})]));
}

function renderGallery(root) {
    pruneUnusedTags();
    const search = el('input', {type: 'search', class: 'rvo-cinput', placeholder: 'Search characters, descriptions, tags', value: chars.q,
        oninput: event => { chars.q = event.target.value.trim(); drawGallery(root); }});
    const sort = el('select', {class: 'rvo-cinput rvo-csort', 'aria-label': 'Sort', onchange: event => { chars.sort = event.target.value; drawGallery(root); }},
        ...[['recent', 'Recently chatted'], ['name', 'Name A–Z'], ['added', 'Newest added']].map(([value, text]) => el('option', {value, text})));
    sort.value = chars.sort;
    const wrap = el('div', {class: `rvo-cwrap rvo-cwrap-filters${chars.filtersOpen ? ' rvo-filters-open' : ''}`});
    const findTag = el('input', {type: 'search', class: 'rvo-cinput rvo-cfilter-find', placeholder: 'Find a tag', value: chars.tagQ,
        oninput: event => { chars.tagQ = event.target.value.trim(); drawTagSidebar(root, galleryList()); }});
    wrap.append(
        el('div', {class: 'rvo-ctools'}, search, sort,
            el('button', {class: 'rvo-cbtn rvo-cfilter-toggle', type: 'button', title: 'Filter by tags', 'aria-expanded': String(chars.filtersOpen), onclick: event => {
                chars.filtersOpen = wrap.classList.toggle('rvo-filters-open');
                event.currentTarget.setAttribute('aria-expanded', String(chars.filtersOpen));
            }}, icon('fa-filter'), el('span', {text: 'Filters'}), el('b', {class: 'rvo-cfilter-count', hidden: ''})),
            el('button', {class: 'rvo-cbtn', type: 'button', title: 'Create a character', onclick: () => showPage('characters', {create: true})}, icon('fa-user-plus'), el('span', {text: 'Create'})),
            el('button', {class: 'rvo-cbtn', type: 'button', title: 'Import a card file', onclick: () => $q('#character_import_button')?.click()}, icon('fa-file-import'), el('span', {text: 'Import'})),
            el('button', {class: 'rvo-cbtn', type: 'button', title: 'Find cards online', onclick: () => showPage('library')}, icon('fa-book-open'), el('span', {text: 'Library'})),
            el('button', {class: 'rvo-cbtn', type: 'button', title: 'Let a model tag your characters (AO3, Danbooru and SillyTavern feature tags)', onclick: () => showPage('characters', {autotag: true})}, icon('fa-wand-magic-sparkles'), el('span', {text: 'Autotag'}))),
        el('div', {class: 'rvo-cbody'},
            el('div', {class: 'rvo-cmain'},
                el('div', {class: 'rvo-cactive', hidden: ''}),
                el('small', {class: 'rvo-ccount'}),
                el('div', {class: 'rvo-cgrid'})),
            el('aside', {class: 'rvo-cfilter', 'aria-label': 'Filter by tags'},
                el('div', {class: 'rvo-cfilter-head'}, el('h3', {}, icon('fa-filter'), el('span', {text: 'Filter by tags'}))),
                findTag,
                el('div', {class: 'rvo-cfilter-groups'}))));
    root.replaceChildren(wrap);
    drawGallery(root);
}

/** Tags no character or group uses any more are deleted (and tag links of characters that are gone), so the
 * tag lists only hold live tags. Runs when the gallery opens and after tags or characters change. */
function pruneUnusedTags() {
    const c = ctx();
    const {tags, tagMap} = c;
    if (!Array.isArray(tags) || !tagMap || !c.characters?.length || !Array.isArray(c.groups)) return false;   // not loaded yet
    const entities = new Set([...c.characters.filter(Boolean).map(ch => ch.avatar), ...c.groups.map(g => String(g.id))]);
    const tagIds = new Set(tags.map(t => t.id));
    let changed = false;
    for (const key of Object.keys(tagMap)) {
        // Characters are keyed by their .png; anything else is a group, dropped only once the groups have loaded.
        const gone = !entities.has(key) && (/\.png$/i.test(key) || c.groups.length > 0);
        if (gone) { delete tagMap[key]; changed = true; continue; }
        const ids = Array.isArray(tagMap[key]) ? tagMap[key] : [];
        const live = [...new Set(ids.filter(id => tagIds.has(id)))];
        if (live.length !== ids.length) { tagMap[key] = live; changed = true; }
    }
    const used = new Set(Object.values(tagMap).flat());
    const gone = [];
    for (let i = tags.length - 1; i >= 0; i--) if (!used.has(tags[i].id)) gone.push(...tags.splice(i, 1));
    const at = settings.autotag;
    if (at?.kinds) for (const id of Object.keys(at.kinds)) if (!used.has(id)) { delete at.kinds[id]; changed = true; }
    if (at?.done) for (const [avatar, done] of Object.entries(at.done)) {
        if (!entities.has(avatar) && /\.png$/i.test(avatar)) { delete at.done[avatar]; changed = true; }
        else if (Array.isArray(done?.tags) && done.tags.some(id => !used.has(id))) { done.tags = done.tags.filter(id => used.has(id)); changed = true; }
    }
    for (const tag of gone) { chars.tags.delete(tag.id); chars.exclude.delete(tag.id); }
    if (gone.length || changed) {
        if (gone.length) console.debug(`[Rocsy] removed ${gone.length} unused tag(s): ${gone.map(t => t.name).join(', ')}`);
        save();   // SillyTavern's tags + our settings in one save
    }
    return gone.length > 0;
}

// Card text is shown formatted (markdown + the HTML creators write), cleaned by Rocsy Addons' private DOMPurify;
// pictures from other sites follow SillyTavern's "external media" setting for that character.
function mediaAllowed(avatar) {
    const p = ctx().powerUserSettings || {};
    if ((p.external_media_allowed_overrides || []).includes(avatar)) return true;
    if ((p.external_media_forbidden_overrides || []).includes(avatar)) return false;
    return !p.forbid_external_media;
}
function cardText(value, fill, {formatted = settings.formatted !== false, avatar = ''} = {}) {
    const box = el('div', {class: `rvo-dtext${formatted ? ' rvo-formatted' : ''}`});
    const html = formatted ? globalThis.RocsyAddons?.formatCardText?.(fill(value), {images: mediaAllowed(avatar)}) : null;
    if (html !== null && html !== undefined) box.innerHTML = html; else box.textContent = fill(value);
    return box;
}
/** Long text folds behind "Show all". */
function foldable(box) {
    requestAnimationFrame(() => {
        if (box.scrollHeight <= 520) return;
        box.classList.add('rvo-folded');
        const more = el('button', {class: 'rvo-showmore', type: 'button', text: 'Show all', onclick: () => {
            more.textContent = box.classList.toggle('rvo-folded') ? 'Show all' : 'Show less';
        }});
        box.after(more);
    });
    return box;
}
const compactNumber = n => Number(n) >= 10000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k` : Number(n || 0).toLocaleString();
function statTiles(stats) {
    return el('div', {class: 'rvo-stats'}, ...stats.map(([label, value, iconName, id]) => el('div', {class: 'rvo-stat', title: label},
        icon(iconName), el('strong', {id, text: typeof value === 'number' ? compactNumber(value) : String(value)}), el('small', {text: label}))));
}
const sideCard = (title, iconName, ...children) => el('section', {class: 'rvo-dcard'}, el('h4', {}, icon(iconName), el('span', {text: title})), ...children);
function facts(pairs) {
    return el('dl', {class: 'rvo-facts'}, ...pairs.filter(([, v]) => v !== '' && v !== null && v !== undefined)
        .flatMap(([k, v]) => [el('dt', {text: k}), el('dd', {text: String(v)})]));
}
function heroBackdrop(src) {
    return el('div', {class: 'rvo-dhero-bg', style: `background-image: url("${String(src).replace(/["\\]/g, '')}")`});
}
const niceDate = value => {
    const time = typeof value === 'number' ? value : Date.parse(String(value || '').replace(/ @(\d+)h (\d+)m (\d+)s.*$/, 'T$1:$2:$3'));
    return time ? new Date(time).toLocaleDateString(undefined, {year: 'numeric', month: 'short', day: 'numeric'}) : '';
};
// The ⋯ menu lives in the hero header, which clips (overflow: hidden). While open, the menu is moved to
// <body> with fixed positioning so nothing can cut it off; it goes back (and closes) on scroll/resize/outside click.
function moreMenu(ch, items) {
    const summary = el('summary', {class: 'rvo-cbtn', title: 'More'}, icon('fa-ellipsis'));
    const menu = el('div', {class: 'rvo-emenu'}, ...items.map(([label, iconName, onclick, danger]) =>
        el('button', {type: 'button', class: danger ? 'danger' : undefined, onclick: event => { box.open = false; onclick(event); }}, icon(iconName), el('span', {text: label}))));
    const box = el('details', {class: 'rvo-emore'}, summary, menu);
    const close = () => { box.open = false; if (!box.isConnected) unfloat(); };
    const outside = event => { if (!box.contains(event.target) && !menu.contains(event.target)) close(); };
    const onKey = event => { if (event.key === 'Escape') close(); };
    const place = () => {
        const r = summary.getBoundingClientRect();
        menu.style.right = `${Math.max(8, innerWidth - r.right)}px`;
        const below = innerHeight - r.bottom - 14;
        if (below < menu.offsetHeight && r.top > below) { menu.style.top = ''; menu.style.bottom = `${innerHeight - r.top + 6}px`; }
        else { menu.style.bottom = ''; menu.style.top = `${r.bottom + 6}px`; }
    };
    function unfloat() {
        removeEventListener('scroll', close, true); removeEventListener('resize', close);
        document.removeEventListener('pointerdown', outside, true); document.removeEventListener('keydown', onKey);
        menu.classList.remove('rvo-emenu-float'); menu.style.top = menu.style.bottom = menu.style.right = '';
        if (box.isConnected) box.append(menu); else menu.remove();
    }
    box.addEventListener('toggle', () => {
        if (!box.open) return unfloat();
        menu.classList.add('rvo-emenu-float'); document.body.append(menu); place();
        addEventListener('scroll', close, true); addEventListener('resize', close);
        document.addEventListener('pointerdown', outside, true); document.addEventListener('keydown', onKey);
    });
    return box;
}

/** The lorebooks linked to a character: its main one (card's "world") plus any extra books, that still exist. */
async function linkedLorebooks(ch) {
    const wi = await worldInfo();
    const names = wi.world_names || [];
    const main = ch?.data?.extensions?.world || '';
    const extra = (wi.world_info?.charLore || []).find(l => l.name === charFile(ch.avatar))?.extraBooks || [];
    return [...new Set([main, ...extra].filter(Boolean))].map(name => ({name, main: name === main, missing: !names.includes(name)}));
}

const loreKeys = entry => (Array.isArray(entry.key) ? entry.key : Array.isArray(entry.keys) ? entry.keys : []).filter(k => String(k).trim());

/** Character page: the linked lorebooks, as readable sections in the main column and a card in the side column. */
function characterLore(ch, text, embeddedName) {
    const holder = el('div', {class: 'rvo-dlore', hidden: true});
    const list = el('div', {class: 'rvo-dlbooks'});
    const card = el('section', {class: 'rvo-dcard', hidden: true}, el('h4', {}, icon('fa-book-atlas'), el('span', {text: 'Lorebook'}), el('small', {class: 'rvo-dexpr-n'})), list);
    linkedLorebooks(ch).then(async books => {
        if (!books.length) return;
        card.querySelector('h4 span').textContent = books.length > 1 ? 'Lorebooks' : 'Lorebook';
        card.querySelector('.rvo-dexpr-n').textContent = books.length > 1 ? String(books.length) : '';
        const rows = books.map(book => {
            const count = el('small', {text: book.missing ? 'Not found — it may have been deleted or renamed' : '…'});
            const row = el('button', {class: `rvo-dlbook${book.missing ? ' missing' : ''}`, type: 'button', disabled: book.missing || undefined,
                title: book.missing ? `${book.name} isn’t in your lorebooks` : `Open ${book.name}`, onclick: () => openLorebook(book.name)},
                bookIcon(book.name), el('span', {class: 'rvo-lbook-text'}, el('strong', {text: book.name}),
                    el('span', {class: 'rvo-dlbook-meta'}, el('em', {text: book.main ? 'Main' : 'Extra'}), count)),
                book.missing ? null : icon('fa-chevron-right'));
            return {book, row, count};
        });
        list.replaceChildren(...rows.map(r => r.row));
        card.hidden = false;
        const sections = await Promise.all(rows.map(async ({book, count}) => {
            if (book.missing) return null;
            let data;
            try { data = await wiModule.loadWorldInfo(book.name); } catch { count.textContent = 'Couldn’t load it'; return null; }
            const entries = Object.values(data?.entries || {}).sort((a, b) => (b.order ?? 0) - (a.order ?? 0) || (a.uid ?? 0) - (b.uid ?? 0));
            loreEntries.set(book.name, entries.length);
            const off = entries.filter(e => e.disable).length;
            count.textContent = entriesText(entries.length) + (off ? ` · ${off} off` : '');
            const q = el('input', {class: 'rvo-cinput rvo-dlore-q', type: 'search', placeholder: 'Search entries…', 'aria-label': `Search ${book.name}`});
            const body = el('div', {class: 'rvo-dlore-list'});
            const draw = () => {
                const term = q.value.trim().toLowerCase();
                const shown = entries.filter(e => !term || [e.comment, e.content, ...loreKeys(e)].some(v => String(v || '').toLowerCase().includes(term)));
                body.replaceChildren(...shown.map(entry => {
                    const keys = loreKeys(entry);
                    return el('div', {class: `rvo-dsub${entry.disable ? ' rvo-dlore-off' : ''}`},
                        el('div', {class: 'rvo-dlore-title'}, el('strong', {text: entry.comment || keys.slice(0, 4).join(', ') || `Entry ${entry.uid ?? ''}`.trim()}),
                            entry.constant ? el('em', {class: 'rvo-dlore-tag', title: 'Always in the prompt', text: 'Always on'}) : null,
                            entry.disable ? el('em', {class: 'rvo-dlore-tag', title: 'Switched off', text: 'Off'}) : null),
                        keys.length ? el('small', {class: 'rvo-muted', text: `Keys: ${keys.join(', ')}`}) : null,
                        String(entry.content || '').trim() ? text(entry.content) : el('small', {class: 'rvo-muted', text: 'No text.'}));
                }), shown.length ? null : el('p', {class: 'rvo-muted', text: term ? 'No entries match.' : 'This lorebook has no entries yet.'}));
            };
            q.addEventListener('input', draw);
            draw();
            return el('details', {class: 'rvo-dsection'},
                el('summary', {}, icon('fa-book-atlas'), el('span', {text: `${book.main ? 'Lorebook' : 'Extra lorebook'}: ${book.name} (${entriesText(entries.length)})`})),
                el('div', {class: 'rvo-dlore-bar'}, entries.length > 4 ? q : null,
                    rvoButton('Open in Lorebooks', 'fa-pen-to-square', () => openLorebook(book.name), `Edit ${book.name} on the Lorebooks page`)),
                body);
        }));
        const shown = sections.filter(Boolean);
        if (!shown.length) return;
        holder.replaceChildren(...shown);
        holder.hidden = false;
        holder.closest('.rvo-dmain')?.querySelector(':scope > .rvo-empty')?.remove();
        // The card's embedded copy is the same book once it's been imported: don't list it twice.
        if (embeddedName && books.some(b => b.name === embeddedName && !b.missing)) holder.closest('.rvo-dmain')?.querySelector(':scope > .rvo-dembedded')?.remove();
    }).catch(() => {});
    return {holder, card};
}

async function renderCharacterDetail(root, avatar) {
    const c = ctx();
    let index = c.characters.findIndex(x => x.avatar === avatar);
    if (index < 0) { chars.view = 'grid'; return renderGallery(root); }
    if (c.characters[index].shallow && c.unshallowCharacter) { await c.unshallowCharacter(index); index = c.characters.findIndex(x => x.avatar === avatar); }
    if (chars.view !== 'detail' || chars.avatar !== avatar || index < 0) return;   // moved on while it loaded
    const ch = c.characters[index];
    const tags = tagsOf(ch);
    const formatted = settings.formatted !== false;
    const fill = text => String(text || '').replace(/\{\{char\}\}/gi, ch.name).replace(/\{\{user\}\}/gi, c.name1 || 'you');
    const text = value => foldable(cardText(value, fill, {formatted, avatar: ch.avatar}));
    const section = (title, value, iconName, open = true) => String(value || '').trim() ? el('details', {class: 'rvo-dsection', open},
        el('summary', {}, icon(iconName), el('span', {text: title})), text(value)) : null;
    const greetings = (ch.data?.alternate_greetings || []).filter(g => String(g || '').trim());
    const book = ch.data?.character_book;
    const entries = Array.isArray(book?.entries) ? book.entries : [];
    const notes = String(ch.data?.creator_notes ?? ch.creatorcomment ?? '');
    const tagline = blurb({...ch, data: {creator_notes: notes}, description: ''});
    const shortNotes = !!tagline && tagline.length <= 260 && !/<img|!\[/i.test(notes);   // short notes go in the hero, long ones get a section
    const image = characterImage(ch.avatar);
    const fav = !!(ch.fav ?? ext(ch).fav);
    const voice = globalThis.RocsyAddons?.voiceControl?.(ch.name) || null;
    const chatsList = el('div', {class: 'rvo-dchats'}, el('small', {class: 'rvo-muted', text: 'Loading…'}));
    const chatsTitle = el('span', {text: 'Chats'});
    const stats = statTiles([['Tokens', '…', 'fa-coins', 'rvo-dstat-tokens'], ['Chats', '…', 'fa-comments', 'rvo-dstat-chats'],
        ['Messages', '…', 'fa-message', 'rvo-dstat-messages'], ['Greetings', 1 + greetings.length, 'fa-comment-dots'],
        ...(entries.length ? [['Lore entries', entries.length, 'fa-book']] : []), ['Tags', tags.length, 'fa-tags']]);
    const toggle = el('button', {class: 'rvo-cbtn rvo-fmt', type: 'button', title: formatted ? 'Show the raw text' : 'Show formatted text',
        onclick: () => { settings.formatted = !formatted; save(); renderCharacterDetail(root, avatar); }},
        icon(formatted ? 'fa-code' : 'fa-wand-magic-sparkles'), el('span', {text: formatted ? 'Raw text' : 'Formatted'}));
    const hero = el('div', {class: 'rvo-dhero'}, heroBackdrop(image),
        el('div', {class: 'rvo-dpic'}, el('img', {src: image, alt: ''}), fav ? el('span', {class: 'rvo-badge', title: 'Favorite'}, icon('fa-star')) : null),
        el('div', {class: 'rvo-dhead'},
            el('small', {class: 'rvo-dkicker', text: 'Character'}),
            el('h2', {text: ch.name}),
            ch.data?.creator || ch.data?.character_version ? el('div', {class: 'rvo-dcreator'}, icon('fa-pen-nib'),
                el('span', {text: [ch.data?.creator, ch.data?.character_version ? `v${ch.data.character_version}` : ''].filter(Boolean).join(' · ')})) : null,
            shortNotes ? el('p', {class: 'rvo-dtagline', text: tagline}) : null,
            tags.length ? el('div', {class: 'rvo-tags'}, ...tags.map(t => tagChip(t, {role: 'button', tabindex: '0', title: `All characters tagged ${t.name}`,
                onclick: () => { chars.tags = new Set([t.id]); chars.exclude.clear(); showPage('characters', {avatar: ''}); }}))) : null,
            el('div', {class: 'rvo-dactions'},
                el('button', {class: 'rvo-cbtn primary', type: 'button', onclick: () => chatWith(ch.avatar)}, icon('fa-comments'), el('span', {text: 'Chat'})),
                el('button', {class: 'rvo-cbtn rvo-newchat', type: 'button', title: `Start a new chat with ${ch.name}`, onclick: () => chatWith(ch.avatar, {fresh: true})}, icon('fa-comment-medical'), el('span', {text: 'New chat'})),
                el('button', {class: 'rvo-cbtn', type: 'button', title: 'Edit name, picture, description, greetings, tags, lorebook…', onclick: () => showPage('characters', {avatar: ch.avatar, edit: true})}, icon('fa-pen'), el('span', {text: 'Edit'})),
                moreMenu(ch, [['Duplicate', 'fa-clone', () => duplicateCharacter(ch)], ['Export card (PNG)', 'fa-file-image', () => exportCharacter(ch, 'png')],
                    ['Export card (JSON)', 'fa-file-code', () => exportCharacter(ch, 'json')],
                    ['Delete…', 'fa-trash-can', () => deleteCharacter(ch), true]]))));
    const lorePart = characterLore(ch, text, book?.name || '');
    const main = el('div', {class: 'rvo-dmain'},
        shortNotes ? null : section('Creator’s notes', notes, 'fa-note-sticky'),
        section('Description', ch.description, 'fa-user'),
        section('Personality', ch.personality, 'fa-masks-theater'),
        section('Scenario', ch.scenario, 'fa-map'),
        section('First message', ch.first_mes, 'fa-comment-dots'),
        greetings.length ? el('details', {class: 'rvo-dsection'},
            el('summary', {}, icon('fa-comments'), el('span', {text: `Alternate greetings (${greetings.length})`})),
            ...greetings.map((greeting, i) => el('div', {class: 'rvo-dsub'}, el('strong', {text: `#${i + 2}`}), text(greeting)))) : null,
        section('Example dialogue', ch.mes_example, 'fa-quote-left', false),
        section('System prompt', ch.data?.system_prompt, 'fa-terminal', false),
        section('Post-history instructions', ch.data?.post_history_instructions, 'fa-terminal', false),
        section('Character’s note', ext(ch).depth_prompt?.prompt, 'fa-thumbtack', false),
        lorePart.holder,
        entries.length ? el('details', {class: 'rvo-dsection rvo-dembedded'},
            el('summary', {}, icon('fa-book'), el('span', {text: `Embedded lorebook${book.name ? `: ${book.name}` : ''} (${entries.length} entries)`})),
            ...entries.map(entry => {
                const keys = Array.isArray(entry.keys) ? entry.keys : Array.isArray(entry.key) ? entry.key : [];
                return el('div', {class: 'rvo-dsub'}, el('strong', {text: entry.comment || entry.name || keys.slice(0, 4).join(', ') || 'Entry'}),
                    keys.length ? el('small', {class: 'rvo-muted', text: `Keys: ${keys.join(', ')}`}) : null, text(entry.content));
            })) : null);
    if (main.children.length === 1) main.append(el('p', {class: 'rvo-empty', text: 'This card is empty: use Edit to write it.'}));
    const side = el('aside', {class: 'rvo-dside'},
        voice ? sideCard('Voice', 'fa-microphone-lines', voice) : null,
        sideCard('Stats', 'fa-chart-simple', stats),
        CP.chatStatsCard({avatar: ch.avatar}),
        lorePart.card,
        expressionsCard(ch),
        sideCard('About', 'fa-circle-info', facts([['Creator', ch.data?.creator], ['Version', ch.data?.character_version], ['Added', niceDate(ch.create_date)],
            ['Last chat', niceDate(Number(ch.date_last_chat) || 0)], ['Lorebook', ext(ch).world || (entries.length ? 'embedded only' : 'none')],
            ['Talkativeness', `${Math.round((ch.talkativeness ?? ext(ch).talkativeness ?? 0.5) * 100)}%`], ['Card', ch.spec ? `${ch.spec} ${ch.spec_version || ''}`.trim() : ''],
            ['File', ch.avatar], ['External media', mediaAllowed(ch.avatar) ? 'shown' : 'blocked']])),
        el('section', {class: 'rvo-dcard'}, el('h4', {}, icon('fa-clock-rotate-left'), chatsTitle), chatsList),
        globalThis.RocsyAddons?.characterComments?.(ch.avatar) || null);   // comments from Chub / Bot Booru, in-site
    root.replaceChildren(el('div', {class: 'rvo-cwrap rvo-details'},
        el('div', {class: 'rvo-dtop'},
            el('button', {class: 'rvo-cbtn rvo-back', type: 'button', onclick: () => showPage('characters', {avatar: ''})}, icon('fa-arrow-left'), el('span', {text: 'All characters'})),
            toggle),
        hero,
        el('div', {class: 'rvo-dcols'}, main, side)));
    root.scrollTop = 0;
    const permanent = [ch.description, ch.personality, ch.scenario, ch.data?.system_prompt, ch.data?.post_history_instructions, ext(ch).depth_prompt?.prompt].filter(Boolean).join('\n');
    (permanent ? c.getTokenCountAsync(permanent) : Promise.resolve(0)).then(n => { const node = $q('#rvo-dstat-tokens', root); if (node) node.textContent = compactNumber(n); }).catch(() => {});
    try {
        const response = await fetch('/api/characters/chats', {method: 'POST', headers: c.getRequestHeaders(), body: JSON.stringify({avatar_url: ch.avatar})});
        const list = response.ok ? await response.json() : [];
        const items = (Array.isArray(list) ? list : []).map(chat => ({name: String(chat.file_name || '').replace(/\.jsonl$/, ''), avatar: ch.avatar, group: '',
            count: Number(chat.chat_items) || 0, preview: String(chat.mes || '').replace(/\s+/g, ' ').slice(0, 140), when: Number(chat.last_mes) || Date.parse(chat.last_mes) || 0}))
            .filter(x => x.name).sort((a, b) => b.when - a.when);
        const set = (id, value) => { const node = $q(id, root); if (node) node.textContent = compactNumber(value); };
        set('#rvo-dstat-chats', items.length);
        set('#rvo-dstat-messages', items.reduce((sum, item) => sum + item.count, 0));
        chatsTitle.textContent = `Chats (${items.length})`;
        chatsList.replaceChildren(...(items.length ? items.map(item => el('button', {
            class: 'rvo-chat rvo-history', type: 'button', title: item.name, onclick: () => { hidePage(); openRecent(item); },
        }, el('span', {class: 'rvo-chat-text'},
            el('span', {class: 'rvo-history-title'}, el('span', {text: displayTitle(item, ch.name)}), el('small', {class: 'rvo-when', text: shortWhen(item.when)})),
            el('span', {class: 'rvo-chat-preview', text: item.preview || 'No messages yet'})))) : [el('small', {class: 'rvo-muted', text: 'No chats yet.'})]));
    } catch {
        chatsList.replaceChildren(el('small', {class: 'rvo-muted', text: 'Couldn’t load the chats.'}));
    }
}

// ----------------------------------------------------------------- autotag --
// A llama-swap model reads every character card (and looks at its picture, when the model can see) and suggests
// AO3-style tags (rating, warnings, category, fandoms, relationships, characters, tropes) and Danbooru-style tags
// (gender, age, species, setting: roleplay basics, no looks); SillyTavern feature tags (lorebook, greetings, expressions, regex…) are worked out
// from the card itself. Suggestions are reviewed per character (click a chip to leave it out) and become ordinary
// SillyTavern tags, so the gallery's tag filter uses them. Autotag remembers what it added, so a re-run replaces
// its own tags and leaves the ones you added by hand.

const AUTOTAG_DEFAULTS = {model: '', parallel: 'auto', vision: true, ao3: true, danbooru: true, st: true, maxAo3: 12, maxDanbooru: 8, review: true, replace: true, strip: false, done: {}, kinds: {}};
const TAG_COLORS = {content: '#8a5a12', ao3: '#8f1d21', danbooru: '#1d5a99', st: '#5b3f8c'};
const autotag = {running: false, stop: false, results: new Map(), selected: null, models: null, rows: new Map(), status: null};
function atSettings() {
    const current = settings.autotag;
    if (current && current.ready === true && typeof current.done === 'object') return current;
    settings.autotag = {...AUTOTAG_DEFAULTS, ...(current && typeof current === 'object' ? current : {}), ready: true};
    settings.autotag.done = {...(settings.autotag.done && typeof settings.autotag.done === 'object' ? settings.autotag.done : {})};
    settings.autotag.kinds = {...(settings.autotag.kinds && typeof settings.autotag.kinds === 'object' ? settings.autotag.kinds : {})};
    if (!settings.autotag.rpOnly) { settings.autotag.rpOnly = true; settings.autotag.maxDanbooru = Math.min(Number(settings.autotag.maxDanbooru) || 8, 8); }   // once: roleplay tags only
    return settings.autotag;
}

/** SillyTavern features the card uses, read from the card itself (no model needed). */
async function stFeatureTags(ch) {
    const tags = [];
    const x = ext(ch);
    const greetings = [ch.first_mes, ...(ch.data?.alternate_greetings || [])].map(g => String(g || ''));
    const all = [ch.description, ch.personality, ch.scenario, ch.mes_example, ...greetings].join('\n');
    if (ch.data?.character_book?.entries?.length || x.world) tags.push('ST: Lorebook');
    if ((ch.data?.alternate_greetings || []).some(g => String(g || '').trim())) tags.push('ST: Alternate Greetings');
    if (String(ch.mes_example || '').trim()) tags.push('ST: Example Dialogue');
    if (String(ch.data?.system_prompt || '').trim()) tags.push('ST: System Prompt');
    if (String(ch.data?.post_history_instructions || '').trim()) tags.push('ST: Post-History Instructions');
    if (String(x.depth_prompt?.prompt || '').trim()) tags.push('ST: Character Note');
    if (Array.isArray(x.regex_scripts) && x.regex_scripts.length) tags.push('ST: Regex Scripts');
    if (Array.isArray(ch.data?.assets) && ch.data.assets.some(a => a?.type && a.type !== 'icon')) tags.push('ST: Embedded Assets');
    if (/<(div|style|details|table|span\s+style|font|center)\b/i.test(greetings.join('\n'))) tags.push('ST: HTML Formatting');
    if (/<img\b|!\[[^\]]*\]\(https?:/i.test(all)) tags.push('ST: Images in Messages');
    if (/\{\{(random|roll|pick|getvar|setvar|getglobalvar|setglobalvar|time|date|idle_duration|lastMessage)\b/i.test(all)) tags.push('ST: Macros');
    if ((ctx().groups || []).some(g => (g.members || []).includes(ch.avatar))) tags.push('ST: Group Chats');
    try {
        const response = await fetch(`/api/sprites/get?name=${encodeURIComponent(spriteFolder(ch))}`, {headers: ctx().getRequestHeaders()});
        const sprites = response.ok ? await response.json() : [];
        if (Array.isArray(sprites) && sprites.length) tags.push('ST: Expressions');
    } catch { /* no sprites */ }
    return tags;
}

/** The card's picture, scaled to at most 768 px, as a JPEG data URL for the model. */
async function cardPicture(avatar) {
    const img = new Image();
    img.src = characterImage(avatar);
    await img.decode();
    const scale = Math.min(1, 768 / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = el('canvas', {width: String(Math.round(img.naturalWidth * scale)), height: String(Math.round(img.naturalHeight * scale))});
    canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/jpeg', 0.85);
}

function cardForTagging(ch) {
    const entries = ch.data?.character_book?.entries || [];
    return {
        name: ch.name, creator_notes: ch.data?.creator_notes ?? ch.creatorcomment, description: ch.description, personality: ch.personality,
        scenario: ch.scenario, first_mes: ch.first_mes, alternate_greetings: ch.data?.alternate_greetings || [], mes_example: ch.mes_example,
        system_prompt: ch.data?.system_prompt, tags: tagsOf(ch).map(t => t.name),
        lorebook: entries.slice(0, 25).map(e => `${e.comment || (e.keys || []).slice(0, 3).join(', ')}: ${String(e.content || '').slice(0, 200)}`).join('\n'),
    };
}

const tagKey = name => String(name || '').toLowerCase().replace(/[_\s]+/g, ' ').trim();

/** Suggestions for one character: [{name, group: 'ao3'|'danbooru'|'st', kind, on}]. */
async function suggestTags(ch) {
    const o = atSettings();
    const out = [];
    const add = (names, group, kind) => { for (const name of names) if (name && !out.some(t => tagKey(t.name) === tagKey(name))) out.push({name, group, kind, on: true}); };
    let vision = false;
    if (o.ao3 || o.danbooru) {
        let image = '';
        if (o.vision) try { image = await cardPicture(ch.avatar); } catch { /* no picture */ }
        const response = await fetch('/api/plugins/rocsy/autotag', {method: 'POST', headers: ctx().getRequestHeaders(),
            body: JSON.stringify({model: o.model, card: cardForTagging(ch), image, ao3: o.ao3, danbooru: o.danbooru, maxAo3: o.maxAo3, maxDanbooru: o.maxDanbooru})});
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || `Rocsy Addons answered ${response.status}`);
        vision = !!data.vision;
        add(data.content || [], 'content', 'Content');   // SFW / NSFW, and Dead Dove: Do Not Eat
        if (o.ao3) {
            if (data.rating) add([`Rating: ${data.rating}`], 'ao3', 'Rating');
            add(data.warnings, 'ao3', 'Warning'); add(data.category, 'ao3', 'Category'); add(data.fandoms, 'ao3', 'Fandom');
            add(data.relationships, 'ao3', 'Relationship'); add(data.characters, 'ao3', 'Character'); add(data.freeform, 'ao3', 'Tag');
        }
        if (o.danbooru) add(data.danbooru, 'danbooru', 'Danbooru');
    }
    if (o.st) add(await stFeatureTags(ch), 'st', 'SillyTavern');
    // Tags the character already has are shown as such and not added twice.
    const have = new Set(tagsOf(ch).map(t => tagKey(t.name)));
    for (const tag of out) tag.has = have.has(tagKey(tag.name));
    return {tags: out, vision};
}

/** Turn the chosen suggestions into SillyTavern tags on the character (replacing Autotag's earlier ones). With
 * "Strip the card first" every tag it had goes (yours, the card's own and earlier Autotag ones), so only the new tags
 * are left; Remove puts the stripped ones back. */
function applyTags(avatar) {
    const c = ctx(), o = atSettings();
    const result = autotag.results.get(avatar);
    if (!result?.tags) return;
    const ids = [];
    for (const suggestion of result.tags.filter(t => t.on)) {
        let tag = c.tags.find(t => tagKey(t.name) === tagKey(suggestion.name));
        if (!tag) {
            tag = {id: crypto.randomUUID(), name: suggestion.name, folder_type: 'NONE', filter_state: 'UNDEFINED',
                sort_order: Math.max(0, ...c.tags.map(t => t.sort_order || 0)) + 1, is_hidden_on_character_card: false,
                color: TAG_COLORS[suggestion.group] || '', color2: '', create_date: Date.now()};
            c.tags.push(tag);
        }
        if (suggestion.kind) o.kinds[tag.id] = suggestion.kind;   // its AO3 group in the gallery's tag sidebar
        ids.push(tag.id);
    }
    const mine = c.tagMap[avatar] ||= [];
    let stripped = o.done[avatar]?.stripped || null;
    if (o.strip) {
        const character = c.characters.find(ch => ch.avatar === avatar);
        const cardTags = cardOwnTags(character);
        const removed = mine.filter(id => !ids.includes(id) && !(o.done[avatar]?.tags || []).includes(id))
            .map(id => c.tags.find(t => t.id === id)).filter(Boolean).map(t => ({name: t.name, color: t.color || ''}));
        // Keep what an earlier strip took, so Remove can still put back the original tags.
        stripped = {tags: [...(stripped?.tags || []), ...removed.filter(r => !(stripped?.tags || []).some(s => tagKey(s.name) === tagKey(r.name)))],
            card: stripped?.card?.length ? stripped.card : cardTags};
        c.tagMap[avatar] = [...ids];
        if (cardTags.length) stripCardTags(avatar).catch(error => toastr.error(`Couldn’t clear the tags written in ${character?.name || avatar}’s card: ${error.message}`, 'Autotag'));
        o.done[avatar] = {at: Date.now(), model: o.model, tags: [...ids], stripped};
    } else {
        const previous = new Set(o.replace ? o.done[avatar]?.tags || [] : []);
        c.tagMap[avatar] = [...new Set([...mine.filter(id => !previous.has(id) || ids.includes(id)), ...ids])];
        o.done[avatar] = {at: Date.now(), model: o.model, tags: ids.filter(id => !mine.includes(id) || previous.has(id)), ...(stripped ? {stripped} : {})};
    }
    result.status = 'applied';
    save();   // our settings (what Autotag added) and SillyTavern's tags in one save
    refreshTagsElsewhere();
    drawAutotagRow(avatar);
    drawAutotagStatus();
}

/** The tags written into a character's card file (imported cards bring them). */
const cardOwnTags = character => (Array.isArray(character?.data?.tags) ? character.data.tags : Array.isArray(character?.tags) ? character.tags : [])
    .map(name => String(name || '').trim()).filter(Boolean);

async function setCardTags(avatar, names) {
    const response = await fetch('/api/characters/merge-attributes', {method: 'POST', headers: ctx().getRequestHeaders(),
        body: JSON.stringify({avatar, tags: names, data: {tags: names}})});
    if (!response.ok) throw new Error(`SillyTavern answered ${response.status}`);
    const character = ctx().characters.find(ch => ch.avatar === avatar);
    if (character) { character.tags = [...names]; character.data ||= {}; character.data.tags = [...names]; }
    refreshCharactersPage();
}
const stripCardTags = avatar => setCardTags(avatar, []);

/** Take Autotag's tags off a character again (tags you added yourself stay; tags a strip took come back). */
function removeAutotags(avatar) {
    const c = ctx(), o = atSettings();
    const done = o.done[avatar] || {};
    const ids = new Set(done.tags || []);
    c.tagMap[avatar] = (c.tagMap[avatar] || []).filter(id => !ids.has(id));
    for (const old of done.stripped?.tags || []) {
        let tag = c.tags.find(t => tagKey(t.name) === tagKey(old.name));
        if (!tag) {
            tag = {id: crypto.randomUUID(), name: old.name, folder_type: 'NONE', filter_state: 'UNDEFINED',
                sort_order: Math.max(0, ...c.tags.map(t => t.sort_order || 0)) + 1, is_hidden_on_character_card: false,
                color: old.color || '', color2: '', create_date: Date.now()};
            c.tags.push(tag);
        }
        if (!c.tagMap[avatar].includes(tag.id)) c.tagMap[avatar].push(tag.id);
    }
    if (done.stripped?.card?.length) setCardTags(avatar, done.stripped.card).catch(error => toastr.error(`Couldn’t restore the card’s own tags: ${error.message}`, 'Autotag'));
    delete o.done[avatar];
    const result = autotag.results.get(avatar);
    if (result?.status === 'applied') result.status = 'ready';
    save();   // our settings (what Autotag added) and SillyTavern's tags in one save
    refreshTagsElsewhere();
    drawAutotagRow(avatar);
    drawAutotagStatus();
}

const taggable = () => ctx().characters.filter(ch => ch?.avatar && !(ch.avatar === 'default_Assistant.png' && !lastChatTime(ch)));

async function runAutotag() {
    const o = atSettings();
    if (autotag.running) { autotag.stop = true; drawAutotagStatus(); return; }
    if (!o.model) return toastr.warning('Pick a model first.');
    if (!o.ao3 && !o.danbooru && !o.st) return toastr.warning('Pick at least one kind of tag.');
    const queue = taggable().filter(ch => autotag.selected.has(ch.avatar)).map(ch => ch.avatar);
    if (!queue.length) return toastr.info('Tick the characters to tag.');
    autotag.running = true; autotag.stop = false; autotag.workers = 0;
    for (const avatar of queue) autotag.results.set(avatar, {status: 'waiting'});
    queue.forEach(drawAutotagRow);
    drawAutotagStatus();
    // Several cards at once: llama-server serves --parallel requests together, which is far faster than one by
    // one. Auto = the model's slots minus one, so a chat on the same model still gets a slot.
    let workers = Number(o.parallel) || 0;
    if (!workers) {
        try {
            const response = await fetch(`/api/plugins/rocsy/autotag/slots?model=${encodeURIComponent(o.model)}`, {headers: ctx().getRequestHeaders()});
            const data = await response.json();
            workers = Math.max(1, Math.min(4, (Number(data.slots) || 1) - 1));
        } catch { workers = 1; }
    }
    autotag.workers = Math.min(workers, queue.length);
    drawAutotagStatus();
    const pending = [...queue];
    const one = async avatar => {
        if (autotag.stop) { autotag.results.set(avatar, {status: 'stopped'}); drawAutotagRow(avatar); return; }
        const c = ctx();
        let index = c.characters.findIndex(x => x.avatar === avatar);
        if (index < 0) return;
        autotag.results.set(avatar, {status: 'working'});
        drawAutotagRow(avatar); drawAutotagStatus();
        try {
            if (c.characters[index].shallow && c.unshallowCharacter) { await c.unshallowCharacter(index); index = c.characters.findIndex(x => x.avatar === avatar); }
            const {tags, vision} = await suggestTags(c.characters[index]);
            autotag.results.set(avatar, {status: 'ready', tags, vision});
            if (!o.review) applyTags(avatar);
        } catch (error) {
            autotag.results.set(avatar, {status: 'error', error: error.message});
        }
        drawAutotagRow(avatar); drawAutotagStatus();
    };
    await Promise.all(Array.from({length: autotag.workers}, async () => { while (pending.length) await one(pending.shift()); }));
    autotag.running = false; autotag.stop = false;
    drawAutotagStatus();
    const ready = [...autotag.results.values()].filter(r => r.status === 'ready').length;
    toastr.success(ready ? `${ready} character${ready === 1 ? '' : 's'} ready to review.` : 'Autotag finished.', 'Autotag');
}

function drawAutotagStatus() {
    const box = autotag.status;
    if (!box?.isConnected) return;
    const results = [...autotag.results.values()];
    const count = status => results.filter(r => r.status === status).length;
    const done = count('ready') + count('applied') + count('error');
    const total = results.filter(r => r.status !== 'stopped').length;
    const run = box.querySelector('.rvo-at-run');
    run.replaceChildren(icon(autotag.running ? 'fa-stop' : 'fa-wand-magic-sparkles'), el('span', {text: autotag.running ? (autotag.stop ? 'Stopping…' : 'Stop') : `Tag ${autotag.selected.size} character${autotag.selected.size === 1 ? '' : 's'}`}));
    run.classList.toggle('primary', !autotag.running);
    box.querySelector('.rvo-at-progress').textContent = total ? `${done} of ${total} done${autotag.running && autotag.workers > 1 ? ` · ${autotag.workers} at once` : ''}${count('error') ? ` · ${count('error')} failed` : ''}${count('applied') ? ` · ${count('applied')} applied` : ''}` : '';
    const bar = box.querySelector('.rvo-at-bar > span');
    bar.style.width = total ? `${Math.round(done / total * 100)}%` : '0';
    const applyAll = box.querySelector('.rvo-at-applyall');
    applyAll.disabled = !count('ready');
    applyAll.querySelector('span').textContent = count('ready') ? `Apply all (${count('ready')})` : 'Apply all';
}

function drawAutotagRow(avatar) {
    const row = autotag.rows.get(avatar);
    if (!row?.isConnected) return;
    const ch = ctx().characters.find(x => x.avatar === avatar);
    if (!ch) return;
    const result = autotag.results.get(avatar) || {status: atSettings().done[avatar] ? 'tagged' : 'idle'};
    const pick = el('input', {type: 'checkbox', title: 'Tag this character', onchange: event => {
        event.target.checked ? autotag.selected.add(avatar) : autotag.selected.delete(avatar); drawAutotagStatus(); }});
    pick.checked = autotag.selected.has(avatar);
    const label = {idle: '', tagged: `Tagged ${niceDate(atSettings().done[avatar]?.at || 0)}`, waiting: 'Waiting', working: 'Reading the card…', ready: 'Review the tags', applied: 'Tags applied',
        error: result.error || 'Failed', stopped: 'Stopped'}[result.status] || '';
    const groups = result.tags ? [['content', 'Content'], ['ao3', 'AO3'], ['danbooru', 'Danbooru'], ['st', 'SillyTavern']].map(([group, title]) => {
        const tags = result.tags.filter(t => t.group === group);
        if (!tags.length) return null;
        return el('div', {class: 'rvo-at-group'}, el('small', {class: 'rvo-at-gname', text: title}),
            el('div', {class: 'rvo-tags'}, ...tags.map(tag => el('button', {type: 'button', class: `rvo-tag rvo-at-tag${tag.on ? ' on' : ''}${tag.has ? ' has' : ''}`,
                'data-group': group, title: `${tag.kind}${tag.has ? ' · already on the character' : ''} — click to ${tag.on ? 'leave out' : 'keep'}`,
                disabled: result.status === 'applied' || undefined, 'aria-pressed': String(tag.on),
                onclick: () => { tag.on = !tag.on; drawAutotagRow(avatar); }}, el('span', {text: tag.name})))));
    }) : [];
    const done = atSettings().done[avatar];
    row.replaceChildren(
        el('div', {class: 'rvo-at-head'}, pick,
            el('img', {class: 'rvo-at-pic', src: characterImage(avatar), alt: '', loading: 'lazy'}),
            el('div', {class: 'rvo-at-who'},
                el('button', {class: 'rvo-at-name', type: 'button', text: ch.name, title: `Open ${ch.name}’s page`, onclick: () => showPage('characters', {avatar})}),
                el('small', {class: `rvo-at-state ${result.status}`}, result.status === 'working' ? icon('fa-spinner fa-spin') : null,
                    el('span', {text: label}), result.tags && result.vision === false && atSettings().vision && (atSettings().ao3 || atSettings().danbooru) ? el('span', {class: 'rvo-muted', text: ' · text only (the model can’t see pictures)'}) : null)),
            el('div', {class: 'rvo-at-actions'},
                result.status === 'ready' ? el('button', {class: 'rvo-cbtn primary', type: 'button', onclick: () => applyTags(avatar)}, icon('fa-check'), el('span', {text: 'Apply'})) : null,
                done && !autotag.running ? el('button', {class: 'rvo-icon-button', type: 'button', title: 'Remove the tags Autotag added', onclick: () => removeAutotags(avatar)}, icon('fa-eraser')) : null)),
        ...groups);
}

async function renderAutotag(root) {
    const o = atSettings();
    const list = taggable();
    autotag.selected ||= new Set(list.filter(ch => !o.done[ch.avatar]).map(ch => ch.avatar));
    for (const avatar of [...autotag.selected]) if (!list.some(ch => ch.avatar === avatar)) autotag.selected.delete(avatar);
    const persist = () => { save(); };
    const model = el('select', {class: 'rvo-einput', onchange: event => { o.model = event.target.value; persist(); }}, el('option', {value: '', text: 'Loading models…'}));
    const loadModels = async () => {
        try {
            if (!autotag.models) {
                const response = await fetch('/api/plugins/rocsy/llama', {headers: ctx().getRequestHeaders()});
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || response.status);
                autotag.models = data.models || [];
            }
            if (!o.model) { o.model = (autotag.models.find(m => m.id === 'qwen-9b') || autotag.models[0])?.id || ''; persist(); }
            model.replaceChildren(...autotag.models.map(m => el('option', {value: m.id, text: m.name})));
            if (o.model && !autotag.models.some(m => m.id === o.model)) model.append(el('option', {value: o.model, text: `${o.model} (missing)`}));
            model.value = o.model;
        } catch (error) {
            model.replaceChildren(el('option', {value: o.model, text: o.model || 'llama-swap isn’t reachable'}));
            toastr.error(`Couldn’t list the models: ${error.message}`, 'Autotag');
        }
    };
    const check = (key, label, hint) => {
        const box = el('input', {type: 'checkbox', onchange: event => { o[key] = event.target.checked; persist(); }});
        box.checked = !!o[key];
        return el('label', {class: 'rvo-echeck rvo-at-check'}, box, el('span', {}, el('span', {text: label}), hint ? el('small', {class: 'rvo-muted', text: hint}) : null));
    };
    const number = (key, min, max) => el('input', {class: 'rvo-einput rvo-enum', type: 'number', min: String(min), max: String(max), value: String(o[key]),
        onchange: event => { o[key] = Math.min(max, Math.max(min, parseInt(event.target.value, 10) || AUTOTAG_DEFAULTS[key])); event.target.value = String(o[key]); persist(); }});
    const rows = el('div', {class: 'rvo-at-list'});
    autotag.rows.clear();
    for (const ch of list) { const row = el('article', {class: 'rvo-at-row'}); autotag.rows.set(ch.avatar, row); rows.append(row); }
    const select = which => {
        autotag.selected = new Set(which === 'all' ? list.map(ch => ch.avatar) : which === 'new' ? list.filter(ch => !o.done[ch.avatar]).map(ch => ch.avatar) : []);
        list.forEach(ch => drawAutotagRow(ch.avatar)); drawAutotagStatus();
    };
    autotag.status = el('section', {class: 'rvo-dcard rvo-at-runbox'},
        el('h4', {}, icon('fa-play'), el('span', {text: 'Run'})),
        el('button', {class: 'rvo-cbtn primary rvo-at-run', type: 'button', onclick: runAutotag}),
        el('div', {class: 'rvo-at-bar'}, el('span')),
        el('small', {class: 'rvo-muted rvo-at-progress'}),
        el('button', {class: 'rvo-cbtn rvo-at-applyall', type: 'button', onclick: () => {
            for (const [avatar, result] of autotag.results) if (result.status === 'ready') applyTags(avatar);
        }}, icon('fa-check-double'), el('span', {text: 'Apply all'})));
    root.replaceChildren(el('div', {class: 'rvo-cwrap rvo-details rvo-autotag'},
        el('div', {class: 'rvo-dtop'},
            el('button', {class: 'rvo-cbtn rvo-back', type: 'button', onclick: () => showPage('characters', {avatar: ''})}, icon('fa-arrow-left'), el('span', {text: 'All characters'}))),
        el('div', {class: 'rvo-at-intro'},
            el('h2', {}, icon('fa-wand-magic-sparkles'), el('span', {text: 'Autotag'})),
            el('p', {class: 'rvo-muted', text: 'A model reads each card (and looks at its picture when it can) and suggests tags that help you pick a roleplay: AO3-style (rating, warnings, who you play, the dynamic, tone, genre, themes), Danbooru-style basics (gender, age when it matters, species, setting) and SillyTavern tags for the features the card uses. Click a tag to leave it out, then Apply: they become normal tags you can filter the gallery by.'})),
        el('div', {class: 'rvo-dcols'},
            el('div', {class: 'rvo-dmain'},
                el('div', {class: 'rvo-at-select'}, el('small', {class: 'rvo-muted', text: `${list.length} characters · select:`}),
                    el('button', {class: 'rvo-macro', type: 'button', text: 'Not tagged yet', onclick: () => select('new')}),
                    el('button', {class: 'rvo-macro', type: 'button', text: 'All', onclick: () => select('all')}),
                    el('button', {class: 'rvo-macro', type: 'button', text: 'None', onclick: () => select('none')})),
                rows),
            el('aside', {class: 'rvo-dside'},
                autotag.status,
                sideCard('Model', 'fa-microchip', model,
                    el('small', {class: 'rvo-muted', text: 'Any llama-swap model; Qwen3.8 models can also see the picture. Others tag from the text. Changing models makes llama-swap load it.'}),
                    check('vision', 'Look at the picture', 'Helps get gender and species right.'),
                    el('div', {class: 'rvo-erow'}, el('span', {class: 'rvo-muted', text: 'Cards at once'}), (() => {
                        const pick = el('select', {class: 'rvo-einput rvo-enum', onchange: event => { o.parallel = event.target.value; persist(); }},
                            ...[['auto', 'Auto'], ['1', '1'], ['2', '2'], ['3', '3'], ['4', '4']].map(([value, text]) => el('option', {value, text})));
                        pick.value = String(o.parallel || 'auto');
                        return pick;
                    })()),
                    el('small', {class: 'rvo-muted', text: 'Auto uses all but one of the model’s parallel slots (qwen-9b: 3), so a chat can still use it.'})),
                sideCard('Tags', 'fa-tags',
                    check('ao3', 'AO3 style', 'Rating, warnings, fandom, who you play, dynamic, tone, genre, themes.'),
                    el('div', {class: 'rvo-erow'}, el('span', {class: 'rvo-muted', text: 'Up to'}), number('maxAo3', 3, 30), el('span', {class: 'rvo-muted', text: 'trope tags'})),
                    check('danbooru', 'Danbooru style', 'Gender, age when it matters, species, setting. No looks or clothing.'),
                    el('div', {class: 'rvo-erow'}, el('span', {class: 'rvo-muted', text: 'Up to'}), number('maxDanbooru', 2, 12), el('span', {class: 'rvo-muted', text: 'tags'})),
                    check('st', 'SillyTavern features', 'Lorebook, alternate greetings, expressions, regex, HTML, macros…')),
                sideCard('Applying', 'fa-check',
                    check('review', 'Review before applying', 'Off: tags are applied as soon as each card is done.'),
                    check('replace', 'Replace earlier Autotag tags', 'Tags you added yourself stay, unless you strip the card.'),
                    check('strip', 'Strip the card first', 'Every tag the character has (yours, the card’s own and earlier Autotag ones) is removed, so only the new tags are left. Remove puts them back.'))))));
    root.scrollTop = 0;
    list.forEach(ch => drawAutotagRow(ch.avatar));
    drawAutotagStatus();
    loadModels();
}

function renderCharactersPage(root, {avatar, edit = false, autotag = false, create = false} = {}) {
    if (create) { flushEdits(); chars.avatar = ''; chars.view = 'create'; }
    else if (autotag) { flushEdits(); chars.avatar = ''; chars.view = 'autotag'; }
    else if (avatar !== undefined) { flushEdits(); chars.avatar = avatar; chars.view = avatar ? (edit ? 'edit' : 'detail') : 'grid'; }
    if (chars.view === 'create') renderCreateCharacter(root);
    else if (chars.view === 'autotag') renderAutotag(root);
    else if (chars.view === 'edit' && chars.avatar) renderEditor(root, chars.avatar);
    else if (chars.view === 'detail' && chars.avatar) renderCharacterDetail(root, chars.avatar);
    else renderGallery(root);
}

// A new character, in the site: name (required), picture, description and first message, then the editor.
const draftCharacter = {name: '', description: '', first_mes: '', file: null, preview: ''};
function renderCreateCharacter(root) {
    const d = draftCharacter;
    const img = el('img', {src: d.preview || 'img/ai4.png', alt: ''});
    const picker = el('input', {type: 'file', accept: 'image/*', hidden: true, onchange: event => {
        const file = event.target.files?.[0];
        if (!file) return;
        d.file = file;
        if (d.preview) URL.revokeObjectURL(d.preview);
        d.preview = URL.createObjectURL(file);
        img.src = d.preview;
    }});
    const status = el('small', {class: 'rvo-serror'});
    const create = el('button', {class: 'rvo-cbtn primary', type: 'button', onclick: async () => {
        if (!d.name.trim()) { status.textContent = 'Give the character a name first.'; return; }
        create.disabled = true;
        status.textContent = '';
        try {
            const form = new FormData();
            for (const [key, value] of Object.entries({ch_name: d.name.trim(), description: d.description, first_mes: d.first_mes, personality: '', scenario: '',
                mes_example: '', creator_notes: '', system_prompt: '', post_history_instructions: '', creator: '', character_version: '', tags: '', talkativeness: '0.5', fav: 'false'}))
                form.append(key, value);
            if (d.file) form.append('avatar', d.file);
            const headers = ctx().getRequestHeaders({omitContentType: true});
            delete headers['Content-Type'];
            const response = await fetch('/api/characters/create', {method: 'POST', headers, body: form, cache: 'no-cache'});
            if (!response.ok) throw new Error(`SillyTavern answered ${response.status}`);
            const avatar = (await response.text()).trim();
            await ctx().getCharacters();
            toastr.success(`Created ${d.name.trim()}`);
            if (d.preview) URL.revokeObjectURL(d.preview);
            Object.assign(d, {name: '', description: '', first_mes: '', file: null, preview: ''});
            showPage('characters', {avatar, edit: true});
        } catch (error) {
            status.textContent = `Couldn’t create the character: ${error.message}`;
        } finally { create.disabled = false; }
    }}, icon('fa-check'), el('span', {text: 'Create and edit'}));
    const text = (key, rows, placeholder) => {
        const node = el('textarea', {class: 'rvo-cinput rvo-sarea', rows, placeholder});
        node.value = d[key];
        node.addEventListener('input', () => { d[key] = node.value; });
        return node;
    };
    root.replaceChildren(el('div', {class: 'rvo-cwrap rvo-swrap'},
        el('div', {class: 'rvo-ctools'}, rvoButton('All characters', 'fa-arrow-left', () => showPage('characters', {avatar: ''})),
            rvoButton('Import a card', 'fa-file-import', () => $q('#character_import_button')?.click(), 'Import a PNG / JSON / CHARX card'),
            rvoButton('Find online', 'fa-book-open', () => showPage('library', {home: true}), 'Browse character cards in the Library')),
        el('section', {class: 'rvo-phero'},
            el('button', {class: 'rvo-ppic', type: 'button', title: 'Choose a picture', onclick: () => picker.click()}, img, el('span', {}, icon('fa-camera'))), picker,
            el('div', {class: 'rvo-phero-text'},
                Object.assign(el('input', {class: 'rvo-cinput rvo-pname-input', placeholder: 'Name', 'aria-label': 'Name', oninput: event => { d.name = event.target.value; status.textContent = ''; }}), {value: d.name}),
                el('small', {class: 'rvo-lnote', text: 'Everything else (personality, scenario, examples, greetings, tags, lorebook) is on the next page.'}))),
        el('label', {class: 'rvo-lfield wide'}, el('span', {text: 'Description'}), text('description', 8, 'Who they are: looks, personality, background. {{char}} is their name, {{user}} yours.')),
        el('label', {class: 'rvo-lfield wide'}, el('span', {text: 'First message'}), text('first_mes', 5, 'How they open the chat.')),
        el('div', {class: 'rvo-sbtns'}, create, status)));
    setTimeout(() => root.querySelector('.rvo-pname-input')?.focus(), 50);
}

// ------------------------------------------------------------------ editor --
// The character editor as a page of the site: sections with a jump list, every field autosaved (merge-attributes,
// so any character can be edited, not only the open one), token counts, {{char}}/{{user}} buttons, alternate
// greetings as a list, tags, lorebook, picture, and Chat / Duplicate / Export / Delete. If the character is the
// one open in SillyTavern, its own (hidden) form gets the same values so it never saves old ones over them.

const SECTIONS = [
    ['basics', 'Creator’s notes', 'fa-note-sticky'], ['character', 'Character', 'fa-user-pen'], ['greetings', 'Greetings', 'fa-comment-dots'],
    ['examples', 'Examples', 'fa-quote-left'], ['prompts', 'Prompts', 'fa-terminal'],
];
const ext = ch => ch.data?.extensions || {};
const FIELDS = [
    {key: 'name', section: 'hero', label: 'Name', line: true, get: ch => ch.name, put: v => ({name: v, data: {name: v}}), form: '#character_name_pole',
        hint: 'Shown in chats and replaces {{char}}. The file keeps its name.'},
    {key: 'creator_notes', section: 'basics', label: 'Creator’s notes', rows: 4, get: ch => ch.data?.creator_notes ?? ch.creatorcomment ?? '',
        put: v => ({creatorcomment: v, data: {creator_notes: v}}), form: '#creator_notes_textarea', hint: 'Shown to people; not sent to the model.'},
    {key: 'description', section: 'character', label: 'Description', rows: 14, tokens: true, get: ch => ch.description, put: v => ({description: v, data: {description: v}}),
        form: '#description_textarea', hint: 'Who they are: looks, background, personality, how they talk. Always in the prompt.'},
    {key: 'personality', section: 'character', label: 'Personality summary', rows: 3, tokens: true, get: ch => ch.personality, put: v => ({personality: v, data: {personality: v}}),
        form: '#personality_textarea', hint: 'A short summary; optional when the description covers it.'},
    {key: 'scenario', section: 'character', label: 'Scenario', rows: 4, tokens: true, get: ch => ch.scenario, put: v => ({scenario: v, data: {scenario: v}}),
        form: '#scenario_pole', hint: 'Where and when the chat starts.'},
    {key: 'first_mes', section: 'greetings', label: 'First message', rows: 10, tokens: true, get: ch => ch.first_mes, put: v => ({first_mes: v, data: {first_mes: v}}),
        form: '#firstmessage_textarea', hint: 'The character’s opening message.'},
    {key: 'mes_example', section: 'examples', label: 'Example dialogue', rows: 10, tokens: true, get: ch => ch.mes_example, put: v => ({mes_example: v, data: {mes_example: v}}),
        form: '#mes_example_textarea', hint: 'Start each example with <START>, then {{user}}: … / {{char}}: … lines.'},
    {key: 'system_prompt', section: 'prompts', label: 'System prompt', rows: 5, tokens: true, get: ch => ch.data?.system_prompt, put: v => ({data: {system_prompt: v}}),
        form: '#system_prompt_textarea', hint: 'Used instead of the main prompt when “Prefer character card prompt” is on; {{original}} keeps the main one.'},
    {key: 'post_history_instructions', section: 'prompts', label: 'Post-history instructions', rows: 4, tokens: true, get: ch => ch.data?.post_history_instructions,
        put: v => ({data: {post_history_instructions: v}}), form: '#post_history_instructions_textarea', hint: 'Sent after the chat history (jailbreak / reminders).'},
    {key: 'depth_prompt', section: 'prompts', label: 'Character’s note', rows: 4, tokens: true, get: ch => ext(ch).depth_prompt?.prompt,
        put: v => ({data: {extensions: {depth_prompt: {prompt: v}}}}), form: '#depth_prompt_prompt', hint: 'Inserted into the chat at the depth below.'},
    {key: 'creator', section: 'hero', label: 'Creator', line: true, get: ch => ch.data?.creator, put: v => ({data: {creator: v}}), form: '#creator_textarea'},
    {key: 'character_version', section: 'hero', label: 'Version', line: true, get: ch => ch.data?.character_version, put: v => ({data: {character_version: v}}), form: '#character_version_textarea'},
];

const editor = {avatar: '', patch: null, timer: 0, saving: false, status: null, snapshot: null};

function mergeDeep(target, source) {
    for (const [key, value] of Object.entries(source)) {
        if (value && typeof value === 'object' && !Array.isArray(value)) mergeDeep(target[key] && typeof target[key] === 'object' && !Array.isArray(target[key]) ? target[key] : (target[key] = {}), value);
        else target[key] = Array.isArray(value) ? [...value] : value;
    }
    return target;
}

function setStatus(text, kind = '') {
    if (!editor.status) return;
    editor.status.textContent = text;
    editor.status.dataset.kind = kind;
}

/** Queue a change; it's saved after a short pause (or at once with Ctrl+S / leaving the editor). */
function queueEdit(patch, formUpdates = {}) {
    editor.patch = mergeDeep(editor.patch || {}, patch);
    editor.form = {...(editor.form || {}), ...formUpdates};
    setStatus('Unsaved changes…', 'dirty');
    clearTimeout(editor.timer);
    editor.timer = setTimeout(flushEdits, 800);
}

async function flushEdits() {
    clearTimeout(editor.timer);
    if (!editor.patch || !editor.avatar || editor.saving) return;
    const patch = editor.patch, form = editor.form || {}, avatar = editor.avatar;
    editor.patch = null; editor.form = null; editor.saving = true;
    setStatus('Saving…', 'saving');
    try {
        const c = ctx();
        const response = await fetch('/api/characters/merge-attributes', {method: 'POST', headers: c.getRequestHeaders(), body: JSON.stringify({avatar, ...patch})});
        if (!response.ok) throw new Error((await response.json().catch(() => ({})))?.error || `SillyTavern answered ${response.status}`);
        const index = c.characters.findIndex(x => x.avatar === avatar);
        if (index >= 0) {
            mergeDeep(c.characters[index], patch);
            if (!c.groupId && String(c.characterId) === String(index)) {
                for (const [selector, value] of Object.entries(form)) {
                    const node = $(selector);
                    if (node.is(':checkbox')) node.prop('checked', !!value); else node.val(value);
                }
                if (patch.name) $('#rm_button_selected_ch h2').text(patch.name);
            }
        }
        setStatus(`Saved ${new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`, 'saved');
        renderHeader();
        renderRecents();
    } catch (error) {
        editor.patch = mergeDeep(patch, editor.patch || {});
        editor.form = {...form, ...(editor.form || {})};
        setStatus(`Not saved: ${error.message}`, 'error');
        toastr.error(error.message, 'Character not saved');
    } finally {
        editor.saving = false;
        if (editor.patch) editor.timer = setTimeout(flushEdits, 800);
    }
}

function autoGrow(area) {
    area.style.height = 'auto';
    area.style.height = `${Math.min(Math.max(area.scrollHeight + 2, 60), 900)}px`;
}

let lastFocused = null;
function insertAtCursor(text) {
    const area = lastFocused;
    if (!area || !area.isConnected) return toastr.info('Click into a text box first.');
    const start = area.selectionStart ?? area.value.length, end = area.selectionEnd ?? start;
    area.setRangeText(text, start, end, 'end');
    area.dispatchEvent(new Event('input'));
    area.focus();
}

function tokenBadge() {
    return el('small', {class: 'rvo-tokens', text: ''});
}

async function countTokens(text, badge, totals, key) {
    try {
        const n = text ? await ctx().getTokenCountAsync(text) : 0;
        badge.textContent = n ? `${n} tokens` : '';
        totals[key] = n;
        const sum = Object.values(totals).reduce((a, b) => a + b, 0);
        const box = $q('#rvo-edit-total');
        if (box) box.textContent = compactNumber(sum);
    } catch { /* tokenizer busy */ }
}

function fieldControl(ch, field, totals) {
    const value = String(field.get(ch) ?? '');
    const badge = field.tokens ? tokenBadge() : null;
    let timer = 0;
    const onInput = event => {
        const v = event.target.value;
        if (!field.line) autoGrow(event.target);
        queueEdit(field.put(field.key === 'name' ? v.trim() || ch.name : v), field.form ? {[field.form]: v} : {});
        if (badge) { clearTimeout(timer); timer = setTimeout(() => countTokens(v, badge, totals, field.key), 500); }
    };
    const input = field.line
        ? el('input', {class: 'rvo-einput', type: 'text', value, oninput: onInput, onfocus: e => { lastFocused = e.target; }})
        : el('textarea', {class: 'rvo-einput', rows: String(field.rows || 4), spellcheck: 'true', oninput: onInput, onfocus: e => { lastFocused = e.target; }});
    if (!field.line) { input.value = value; requestAnimationFrame(() => autoGrow(input)); }
    if (badge) countTokens(value, badge, totals, field.key);
    return el('label', {class: 'rvo-efield', 'data-field': field.key},
        el('span', {class: 'rvo-elabel'}, el('strong', {text: field.label}), badge),
        input,
        field.hint ? el('small', {class: 'rvo-muted', text: field.hint}) : null);
}

function greetingsEditor(ch, totals) {
    const list = el('div', {class: 'rvo-greetings'});
    let greetings = [...(ch.data?.alternate_greetings || [])];
    const save = () => queueEdit({data: {alternate_greetings: greetings}});
    const draw = () => {
        list.replaceChildren(...greetings.map((text, i) => {
            const badge = tokenBadge();
            const area = el('textarea', {class: 'rvo-einput', rows: '5', onfocus: e => { lastFocused = e.target; },
                oninput: event => { greetings[i] = event.target.value; autoGrow(event.target); save(); countTokens(event.target.value, badge, {}, ''); }});
            area.value = text;
            requestAnimationFrame(() => autoGrow(area));
            countTokens(text, badge, {}, '');
            const move = d => { const j = i + d; if (j < 0 || j >= greetings.length) return; [greetings[i], greetings[j]] = [greetings[j], greetings[i]]; save(); draw(); };
            return el('div', {class: 'rvo-greeting'},
                el('div', {class: 'rvo-elabel'}, el('strong', {text: `Greeting #${i + 2}`}), badge,
                    el('span', {class: 'rvo-gtools'},
                        el('button', {class: 'rvo-icon-button', type: 'button', title: 'Move up', disabled: i === 0, onclick: () => move(-1)}, icon('fa-arrow-up')),
                        el('button', {class: 'rvo-icon-button', type: 'button', title: 'Move down', disabled: i === greetings.length - 1, onclick: () => move(1)}, icon('fa-arrow-down')),
                        el('button', {class: 'rvo-icon-button', type: 'button', title: 'Make this the first message', onclick: () => {
                            const first = String(ch.first_mes || '');
                            greetings[i] = first;
                            queueEdit({first_mes: text, data: {first_mes: text, alternate_greetings: greetings}}, {'#firstmessage_textarea': text});
                            const box = $q('.rvo-efield[data-field="first_mes"] textarea'); if (box) { box.value = text; autoGrow(box); }
                            ch.first_mes = text;
                            draw();
                        }}, icon('fa-arrow-turn-up')),
                        el('button', {class: 'rvo-icon-button', type: 'button', title: 'Delete this greeting', onclick: () => { greetings.splice(i, 1); save(); draw(); }}, icon('fa-trash-can')))),
                area);
        }), el('button', {class: 'rvo-cbtn', type: 'button', onclick: () => { greetings.push(''); save(); draw(); requestAnimationFrame(() => list.querySelectorAll('textarea')[greetings.length - 1]?.focus()); }},
            icon('fa-plus'), el('span', {text: 'Add a greeting'})));
    };
    draw();
    return el('div', {class: 'rvo-efield'}, el('span', {class: 'rvo-elabel'}, el('strong', {text: 'Alternate greetings'}),
        el('small', {class: 'rvo-muted', text: 'Swipe the first message to pick one.'})), list);
}

function tagsEditor(ch) {
    const c = ctx();
    const box = el('div', {class: 'rvo-tags rvo-etags'});
    const listId = 'rvo-tag-options';
    const draw = () => {
        const mine = (c.tagMap[ch.avatar] || []).map(id => c.tags.find(t => t.id === id)).filter(Boolean);
        const input = el('input', {class: 'rvo-einput rvo-taginput', type: 'text', list: listId, placeholder: 'Add a tag…',
            onkeydown: event => { if (event.key === 'Enter' || event.key === ',') { event.preventDefault(); add(event.target.value); } },
            onchange: event => add(event.target.value)});
        box.replaceChildren(...mine.map(tag => el('span', {class: 'rvo-tag', style: tag.color ? `--tag-bg: ${tag.color}` : undefined},
            el('span', {text: tag.name}), el('button', {class: 'rvo-tagx', type: 'button', title: `Remove ${tag.name}`, onclick: () => remove(tag.id)}, icon('fa-xmark')))),
            input, el('datalist', {id: listId}, ...c.tags.filter(t => !mine.includes(t)).map(t => el('option', {value: t.name}))));
    };
    const commit = () => { c.saveSettingsDebounced(); setStatus('Tags saved', 'saved'); refreshTagsElsewhere(); };
    const add = name => {
        const clean = String(name || '').trim().replace(/,$/, '');
        if (!clean) return;
        let tag = c.tags.find(t => t.name.toLowerCase() === clean.toLowerCase());
        if (!tag) {
            tag = {id: crypto.randomUUID(), name: clean, folder_type: 'NONE', filter_state: 'UNDEFINED',
                sort_order: Math.max(0, ...c.tags.map(t => t.sort_order || 0)) + 1, is_hidden_on_character_card: false, color: '', color2: '', create_date: Date.now()};
            c.tags.push(tag);
        }
        const ids = c.tagMap[ch.avatar] ||= [];
        if (!ids.includes(tag.id)) ids.push(tag.id);
        commit(); draw();
        box.querySelector('.rvo-taginput')?.focus();
    };
    const remove = id => { c.tagMap[ch.avatar] = (c.tagMap[ch.avatar] || []).filter(x => x !== id); commit(); draw(); };
    draw();
    const cardTags = Array.isArray(ch.data?.tags) ? ch.data.tags : [];
    return el('div', {class: 'rvo-efield'}, el('span', {class: 'rvo-elabel'}, el('strong', {text: 'Tags'}),
        el('small', {class: 'rvo-muted', text: 'Your SillyTavern tags (filters in the gallery). Enter or comma adds one.'})), box,
        cardTags.length ? el('div', {class: 'rvo-cardtags'}, el('small', {class: 'rvo-muted', text: 'Tags written in the card:'}),
            ...cardTags.map(name => el('button', {class: 'rvo-tag rvo-tagadd', type: 'button', title: 'Add as your tag', onclick: () => add(name)}, icon('fa-plus'), el('span', {text: name})))) : null);
}

function refreshTagsElsewhere() {
    pruneUnusedTags();
    try { ctx().eventSource.emit(ctx().eventTypes.CHARACTER_EDITED ?? 'character_edited', {}); } catch { /* fine */ }
}

function extrasEditor(ch) {
    const c = ctx();
    const worlds = c.getWorldInfoNames ? c.getWorldInfoNames() : [];
    const current = ext(ch).world || '';
    const world = el('select', {class: 'rvo-einput', onchange: event => queueEdit({data: {extensions: {world: event.target.value}}}, {'#character_world': event.target.value})},
        el('option', {value: '', text: 'None'}), ...worlds.map(name => el('option', {value: name, text: name})));
    if (current && !worlds.includes(current)) world.append(el('option', {value: current, text: `${current} (missing)`}));
    world.value = current;
    const depth = el('input', {class: 'rvo-einput rvo-enum', type: 'number', min: '0', max: '999', value: String(ext(ch).depth_prompt?.depth ?? 4),
        oninput: event => queueEdit({data: {extensions: {depth_prompt: {depth: Number(event.target.value) || 0}}}}, {'#depth_prompt_depth': event.target.value})});
    const role = el('select', {class: 'rvo-einput rvo-enum', onchange: event => queueEdit({data: {extensions: {depth_prompt: {role: event.target.value}}}}, {'#depth_prompt_role': event.target.value})},
        ...[['system', 'System'], ['user', 'User'], ['assistant', 'Assistant']].map(([value, text]) => el('option', {value, text})));
    role.value = ext(ch).depth_prompt?.role || 'system';
    const talk = el('input', {type: 'range', min: '0', max: '1', step: '0.05', value: String(ch.talkativeness ?? ext(ch).talkativeness ?? 0.5),
        oninput: event => { talkOut.textContent = `${Math.round(event.target.value * 100)}%`; queueEdit({talkativeness: event.target.value, data: {extensions: {talkativeness: event.target.value}}}, {'#talkativeness_slider': event.target.value}); }});
    const talkOut = el('span', {class: 'rvo-muted', text: `${Math.round((ch.talkativeness ?? ext(ch).talkativeness ?? 0.5) * 100)}%`});
    const fav = el('input', {type: 'checkbox', onchange: event => queueEdit({fav: event.target.checked, data: {extensions: {fav: event.target.checked}}}, {'#fav_checkbox': event.target.checked})});
    fav.checked = !!(ch.fav ?? ext(ch).fav);
    const book = ch.data?.character_book;
    return [
        el('label', {class: 'rvo-efield'}, el('span', {class: 'rvo-elabel'}, el('strong', {text: 'Linked lorebook'})), world,
            el('small', {class: 'rvo-muted', text: book?.entries?.length ? `The card also carries its own lorebook (${book.entries.length} entries); SillyTavern offers to import it as a world.` : 'A world from SillyTavern’s World Info, used whenever this character chats.'})),
        el('div', {class: 'rvo-efield'}, el('span', {class: 'rvo-elabel'}, el('strong', {text: 'Character’s note placement'})),
            el('div', {class: 'rvo-erow'}, el('span', {class: 'rvo-muted', text: 'Depth'}), depth, el('span', {class: 'rvo-muted', text: 'Role'}), role)),
        el('div', {class: 'rvo-efield'}, el('span', {class: 'rvo-elabel'}, el('strong', {text: 'Talkativeness'}), talkOut), talk,
            el('small', {class: 'rvo-muted', text: 'How often they speak up in group chats.'})),
        el('label', {class: 'rvo-echeck'}, fav, el('span', {text: 'Favorite'})),
    ];
}

async function changePicture(ch, img, backdrop = null) {
    const picker = el('input', {type: 'file', accept: 'image/*'});
    picker.addEventListener('change', async () => {
        const file = picker.files?.[0];
        if (!file) return;
        const form = new FormData();
        form.append('avatar', file);
        form.append('avatar_url', ch.avatar);
        const headers = ctx().getRequestHeaders();
        delete headers['Content-Type'];
        setStatus('Uploading picture…', 'saving');
        try {
            const response = await fetch('/api/characters/edit-avatar', {method: 'POST', body: form, headers});
            if (!response.ok) throw new Error(await response.text());
            await fetch(characterImage(ch.avatar), {cache: 'reload'}).catch(() => {});
            await fetch(ctx().getThumbnailUrl('avatar', ch.avatar), {cache: 'reload'}).catch(() => {});
            img.src = `${characterImage(ch.avatar)}?t=${Date.now()}`;
            if (backdrop) backdrop.style.backgroundImage = `url("${img.src}")`;
            setStatus('Picture saved', 'saved');
            renderRecents();
        } catch (error) {
            setStatus('Picture not saved', 'error');
            toastr.error(String(error.message || error).slice(0, 200), 'Picture');
        }
    });
    picker.click();
}

async function exportCharacter(ch, format) {
    await flushEdits();
    try {
        const response = await fetch('/api/characters/export', {method: 'POST', headers: ctx().getRequestHeaders(), body: JSON.stringify({format, avatar_url: ch.avatar})});
        if (!response.ok) throw new Error(`SillyTavern answered ${response.status}`);
        const blob = await response.blob();
        const link = el('a', {href: URL.createObjectURL(blob), download: `${ch.name}.${format}`});
        document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 4000);
    } catch (error) { toastr.error(error.message, 'Export'); }
}

async function duplicateCharacter(ch) {
    await flushEdits();
    try {
        const response = await fetch('/api/characters/duplicate', {method: 'POST', headers: ctx().getRequestHeaders(), body: JSON.stringify({avatar_url: ch.avatar})});
        if (!response.ok) throw new Error(`SillyTavern answered ${response.status}`);
        const data = await response.json().catch(() => ({}));
        await ctx().getCharacters();
        toastr.success(`Duplicated ${ch.name}`);
        if (data?.path) showPage('characters', {avatar: String(data.path).split('/').pop(), edit: true});
    } catch (error) { toastr.error(error.message, 'Duplicate'); }
}

async function deleteCharacter(ch) {
    await flushEdits();
    const c = ctx();
    const index = c.characters.findIndex(x => x.avatar === ch.avatar);
    if (index < 0) return;
    const {Popup} = c;
    const popup = new Popup(`Delete ${ch.name}?`, ctx().POPUP_TYPE?.CONFIRM ?? 2, '', {okButton: 'Delete', cancelButton: 'Cancel',
        customInputs: [{id: 'rvo_delete_chats', label: 'Also delete the chats with this character', type: 'checkbox', defaultState: true}]});
    if (!await popup.show()) return;
    const deleteChats = !!popup.inputResults?.get('rvo_delete_chats');
    const script = await import('../../../../script.js');
    await script.deleteCharacter(ch.avatar, {deleteChats});
    toastr.success(`Deleted ${ch.name}`);
    showPage('characters', {avatar: ''});
}

async function revertCharacter(ch) {
    if (!editor.snapshot) return;
    const ok = ctx().callGenericPopup ? await ctx().callGenericPopup(`Undo every change made to ${ch.name} since this editor was opened?`, ctx().POPUP_TYPE?.CONFIRM ?? 2) : confirm('Undo every change?');
    if (!ok) return;
    const snap = editor.snapshot;
    const patch = {}, form = {};
    for (const field of FIELDS) { mergeDeep(patch, field.put(String(field.get(snap) ?? ''))); if (field.form) form[field.form] = String(field.get(snap) ?? ''); }
    mergeDeep(patch, {data: {alternate_greetings: [...(snap.data?.alternate_greetings || [])], extensions: {
        world: ext(snap).world || '', talkativeness: snap.talkativeness ?? ext(snap).talkativeness ?? 0.5, fav: !!(snap.fav ?? ext(snap).fav),
        depth_prompt: {prompt: ext(snap).depth_prompt?.prompt || '', depth: ext(snap).depth_prompt?.depth ?? 4, role: ext(snap).depth_prompt?.role || 'system'}}}});
    queueEdit(patch, form);
    await flushEdits();
    renderEditor(pageNode('characters'), ch.avatar, {keepSnapshot: true});
}

/** A one-line hero input (name, creator, version): typed straight into the header. */
function heroInput(ch, key, cls, placeholder) {
    const field = FIELDS.find(f => f.key === key);
    return el('input', {class: `rvo-hinput ${cls}`, type: 'text', value: String(field.get(ch) ?? ''), placeholder, 'aria-label': field.label, spellcheck: 'false',
        oninput: event => { const v = event.target.value; queueEdit(field.put(key === 'name' ? v.trim() || ch.name : v), field.form ? {[field.form]: v} : {}); },
        onfocus: e => { lastFocused = e.target; }});
}

async function renderEditor(root, avatar, {keepSnapshot = false} = {}) {
    const c = ctx();
    let index = c.characters.findIndex(x => x.avatar === avatar);
    if (index < 0) { chars.view = 'grid'; return renderGallery(root); }
    if (c.characters[index].shallow && c.unshallowCharacter) { await c.unshallowCharacter(index); index = c.characters.findIndex(x => x.avatar === avatar); }
    if (chars.view !== 'edit' || chars.avatar !== avatar || index < 0) return;   // moved on while it loaded
    const ch = c.characters[index];
    if (editor.avatar !== avatar || !keepSnapshot) editor.snapshot = JSON.parse(JSON.stringify(ch));
    editor.avatar = avatar;
    const totals = {};
    editor.status = el('span', {class: 'rvo-estatus', text: 'All changes are saved automatically'});
    const image = characterImage(ch.avatar);
    const pic = el('img', {src: image, alt: ''});
    const backdrop = heroBackdrop(image);
    const picture = el('button', {class: 'rvo-dpic rvo-epicbtn', type: 'button', title: 'Change picture', onclick: () => changePicture(ch, pic, backdrop)},
        pic, el('span', {class: 'rvo-epicover'}, icon('fa-camera'), el('span', {text: 'Change picture'})));
    const hero = el('div', {class: 'rvo-dhero rvo-ehero'}, backdrop, picture,
        el('div', {class: 'rvo-dhead'},
            el('small', {class: 'rvo-dkicker', text: 'Editing character'}),
            heroInput(ch, 'name', 'rvo-hname', 'Name'),
            el('div', {class: 'rvo-dcreator'}, icon('fa-pen-nib'), heroInput(ch, 'creator', 'rvo-hcreator', 'Creator'),
                el('span', {class: 'rvo-hsep', text: 'v'}), heroInput(ch, 'character_version', 'rvo-hversion', 'version')),
            tagsEditor(ch)));
    const sections = SECTIONS.map(([id, title, iconName]) => {
        const body = FIELDS.filter(f => f.section === id).map(f => fieldControl(ch, f, totals));
        if (id === 'greetings') body.push(greetingsEditor(ch, totals));
        return el('section', {class: 'rvo-esection', id: `rvo-edit-${id}`}, el('h3', {}, icon(iconName), el('span', {text: title})), ...body);
    });
    const nav = el('nav', {class: 'rvo-enav'}, ...SECTIONS.map(([id, title, iconName]) => el('button', {type: 'button', 'data-section': id,
        onclick: () => root.querySelector(`#rvo-edit-${id}`)?.scrollIntoView({behavior: 'smooth', block: 'start'})}, icon(iconName), el('span', {text: title}))),
        el('button', {type: 'button', 'data-section': 'settings', onclick: () => root.querySelector('#rvo-edit-settings')?.scrollIntoView({behavior: 'smooth', block: 'start'})},
            icon('fa-sliders'), el('span', {text: 'Lorebook & more'})),
        ...(globalThis.RocsyImage ? [['rimage', 'Picture & expressions', 'fa-wand-magic-sparkles'], ['rvoice', 'Voice', 'fa-microphone-lines']] : []).map(([id, title, iconName]) =>
            el('button', {type: 'button', 'data-section': id, onclick: () => root.querySelector(`#rvo-edit-${id}`)?.scrollIntoView({behavior: 'smooth', block: 'start'})}, icon(iconName), el('span', {text: title}))));
    const book = ch.data?.character_book;
    // Rocsy Image / Voice (rocsy-addons rimage.js): paint the picture (when unset) and the expression sprites, make a voice.
    const rocsyCard = (id, title, iconName, body) => el('section', {class: 'rvo-dcard', id: `rvo-edit-${id}`}, el('h4', {}, icon(iconName), el('span', {text: title})), body);
    const newPicture = () => {
        pic.src = `${characterImage(ch.avatar)}?t=${Date.now()}`;
        backdrop.style.backgroundImage = `url("${pic.src}")`;
        renderRecents();
    };
    const side = el('aside', {class: 'rvo-dside rvo-eside'},
        sideCard('Jump to', 'fa-list', nav),
        globalThis.RocsyImage ? rocsyCard('rimage', 'Picture & expressions', 'fa-wand-magic-sparkles', globalThis.RocsyImage.pictureTools(ch, {onPicture: newPicture})) : null,
        globalThis.RocsyImage ? rocsyCard('rvoice', 'Voice', 'fa-microphone-lines', globalThis.RocsyImage.voiceTools(ch)) : null,
        sideCard('Insert', 'fa-i-cursor', el('div', {class: 'rvo-emacros'},
            ...['{{char}}', '{{user}}', '<START>'].map(macro => el('button', {class: 'rvo-macro', type: 'button', text: macro,
                onmousedown: event => event.preventDefault(), onclick: () => insertAtCursor(macro)}))),
            el('small', {class: 'rvo-muted', text: 'Goes in at the cursor of the last box you clicked.'})),
        sideCard('Stats', 'fa-chart-simple', statTiles([['Tokens', '…', 'fa-coins', 'rvo-edit-total'], ['Greetings', 1 + (ch.data?.alternate_greetings || []).length, 'fa-comment-dots'],
            ['Lore entries', book?.entries?.length || 0, 'fa-book'], ['Tags', (c.tagMap[ch.avatar] || []).length, 'fa-tags']])),
        el('section', {class: 'rvo-dcard', id: 'rvo-edit-settings'}, el('h4', {}, icon('fa-sliders'), el('span', {text: 'Lorebook & more'})), ...extrasEditor(ch)));
    const bar = el('div', {class: 'rvo-ebar'},
        el('button', {class: 'rvo-cbtn rvo-back', type: 'button', onclick: () => showPage('characters', {avatar})}, icon('fa-arrow-left'), el('span', {text: 'Done'})),
        editor.status,
        el('div', {class: 'rvo-ebar-actions'},
            el('button', {class: 'rvo-cbtn primary', type: 'button', onclick: async () => { await flushEdits(); chatWith(avatar); }}, icon('fa-comments'), el('span', {text: 'Chat'})),
            el('button', {class: 'rvo-cbtn', type: 'button', title: 'Undo every change since the editor was opened', onclick: () => revertCharacter(ch)}, icon('fa-rotate-left'), el('span', {text: 'Revert'})),
            moreMenu(ch, [['Duplicate', 'fa-clone', () => duplicateCharacter(ch)], ['Export card (PNG)', 'fa-file-image', () => exportCharacter(ch, 'png')],
                ['Export card (JSON)', 'fa-file-code', () => exportCharacter(ch, 'json')],
                ['Delete…', 'fa-trash-can', () => deleteCharacter(ch), true]])));
    root.replaceChildren(el('div', {class: 'rvo-editor rvo-details'}, bar, hero, el('div', {class: 'rvo-dcols'}, el('div', {class: 'rvo-dmain rvo-esections'}, ...sections), side)));
    root.scrollTop = 0;
    // Highlight the section in view.
    const spy = new IntersectionObserver(entries => {
        for (const entry of entries) if (entry.isIntersecting) nav.querySelectorAll('button[data-section]').forEach(b => b.classList.toggle('on', `rvo-edit-${b.dataset.section}` === entry.target.id));
    }, {root, rootMargin: '-20% 0px -70% 0px'});
    sections.forEach(section => spy.observe(section));
}

function refreshCharactersPage() {
    if (page !== 'characters') return;
    // The editor and Autotag keep themselves up to date; redrawing them would throw away text typed in the last
    // moment (not saved yet) and the cursor. Only leave the editor when its character is gone.
    if (chars.view === 'create') return;
    if ((chars.view === 'edit' || chars.view === 'autotag') && (chars.view === 'autotag' || ctx().characters.some(x => x.avatar === chars.avatar))) return;
    renderCharactersPage(pageNode('characters'));
}

function newChat() {
    hideLibrary();
    const c = ctx();
    if (c.characterId === undefined && !c.groupId) {
        openCharacters();
        return;
    }
    $q('#option_start_new_chat')?.click();
    closeSidebarOnMobile();
}

// ---------------------------------------------------------------- recents --

async function loadRecents() {
    try {
        const response = await fetch('/api/chats/recent', {
            method: 'POST', headers: ctx().getRequestHeaders(), cache: 'no-cache',
            body: JSON.stringify({max: settings.recentLimit, pinned: []}),
        });
        const data = response.ok ? await response.json() : [];
        const {characters, groups} = ctx();
        recents = (Array.isArray(data) ? data : []).map(chat => {
            const character = chat.avatar ? characters.find(x => x.avatar === chat.avatar) : null;
            const group = chat.group ? groups.find(x => x.id === chat.group) : null;
            if (!character && !group) return null;
            return {
                name: String(chat.file_name || '').replace(/\.jsonl$/, ''),
                who: character?.name || group?.name || '',
                avatar: chat.avatar || '', group: chat.group || '',
                thumb: character ? ctx().getThumbnailUrl('avatar', character.avatar) : '',
                preview: String(chat.mes || '').replace(/\s+/g, ' ').slice(0, 120),
                when: Number(chat.last_mes) || Date.parse(chat.last_mes) || 0,
            };
        }).filter(Boolean).sort((a, b) => b.when - a.when);
        histories.clear();
        const reload = [];
        for (const key of expanded) {
            const chats = recents.filter(item => entryKey(item) === key);
            if (chats.length) reload.push(loadHistory({...chats[0], chats}));
        }
        await Promise.all(reload);
    } catch (error) {
        console.warn('[Rocsy] recent chats', error);
    }
    renderRecents();
}

function scheduleRecents(delay = 1500) {
    clearTimeout(recentsTimer);
    recentsTimer = setTimeout(loadRecents, delay);
}

async function openRecent(item) {
    hideLibrary();
    const c = ctx();
    try {
        if (item.group) {
            await c.openGroupChat(item.group, item.name);
        } else {
            const index = c.characters.findIndex(x => x.avatar === item.avatar);
            if (index < 0) return toastr.error('That character no longer exists.');
            if (String(c.characterId) !== String(index)) await c.selectCharacterById(index);
            if (c.getCurrentChatId() !== item.name) await ctx().openCharacterChat(item.name);
        }
    } catch (error) {
        console.error('[Rocsy] open chat', error);
        toastr.error('Couldn’t open that chat.');
    }
    closeSidebarOnMobile();
}

// Chat history by character: one row per character (or group), most recently used first, with a new-chat button.
// Clicking the row unfolds that character's chats; for a character the full list comes from SillyTavern
// (/api/characters/chats), for a group the recent chats are used.

const entryKey = item => item.group ? `g:${item.group}` : `c:${item.avatar}`;

function shortWhen(time) {
    if (!time) return '';
    const diff = Date.now() - time, min = 6e4, hour = 36e5, day = 864e5;
    if (diff < hour) return `${Math.max(1, Math.round(diff / min))}m`;
    if (diff < day) return `${Math.round(diff / hour)}h`;
    if (diff < 7 * day) return `${Math.round(diff / day)}d`;
    return new Date(time).toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
}

/** "Robin - 2026-11-11@17h52m38s759ms" → "Nov 11, 17:52"; a renamed chat keeps its name. */
function chatTitle(name, who) {
    const rest = name.startsWith(`${who} - `) ? name.slice(who.length + 3) : name;
    const stamp = rest.match(/^(\d{4})-(\d{2})-(\d{2})\s*@(\d{2})h\s*(\d{2})m/);
    if (!stamp) return rest;
    return new Date(+stamp[1], +stamp[2] - 1, +stamp[3], +stamp[4], +stamp[5])
        .toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
}

/** A chat's name: the one Rocsy Addons keeps (given by the user or named automatically), else the file's. */
const titleInfo = item => globalThis.RocsyAddons?.titles?.get(item.group ? `g:${item.group}` : `c:${item.avatar}`, item.name) || null;
const displayTitle = (item, who) => titleInfo(item)?.title || chatTitle(item.name, who);

// Rename in place, like the Master Agent's session list: Enter saves, Esc cancels, an empty name hands naming
// back. The list isn't redrawn while a name is being edited.
let renaming = '';
function startRename(item, who, holder, after = null) {
    if (!globalThis.RocsyAddons?.titles) return toastr.info('Chat names need Rocsy Addons.');
    const owner = item.group ? `g:${item.group}` : `c:${item.avatar}`;
    const before = titleInfo(item)?.title || '';
    renaming = `${owner}/${item.name}`;
    const box = el('input', {class: 'rvo-rename', type: 'text', maxlength: '80', value: before || chatTitle(item.name, who),
        placeholder: 'Empty: name it automatically', 'aria-label': 'Chat name'});
    let done = false;
    const finish = save => {
        if (done) return;
        done = true;
        renaming = '';
        if (save && box.value.trim() !== before) globalThis.RocsyAddons.titles.rename(owner, item.name, box.value.trim());
        renderRecents();
        after?.();
    };
    box.addEventListener('keydown', event => {
        event.stopPropagation();
        if (event.key === 'Enter') { event.preventDefault(); finish(true); }
        else if (event.key === 'Escape') { event.preventDefault(); finish(false); }
    });
    box.addEventListener('blur', () => finish(true));
    box.addEventListener('click', event => event.stopPropagation());
    holder.replaceChildren(box);
    box.focus();
    box.select();
}

async function loadHistory(entry) {
    const key = entryKey(entry);
    if (entry.group) { histories.set(key, entry.chats); return; }
    try {
        const response = await fetch('/api/characters/chats', {method: 'POST', headers: ctx().getRequestHeaders(), cache: 'no-cache',
            body: JSON.stringify({avatar_url: entry.avatar})});
        const data = response.ok ? await response.json() : [];
        if (!Array.isArray(data)) throw new Error('no chats');
        histories.set(key, data.map(chat => ({
            ...entry.chats[0], name: String(chat.file_name || '').replace(/\.jsonl$/, ''),
            preview: String(chat.mes || '').replace(/\s+/g, ' ').slice(0, 120),
            when: Number(chat.last_mes) || Date.parse(chat.last_mes) || 0, count: Number(chat.chat_items) || 0,
        })).filter(chat => chat.name).sort((a, b) => b.when - a.when));
    } catch {
        histories.set(key, entry.chats);
    }
}

async function toggleEntry(entry) {
    const key = entryKey(entry);
    if (expanded.has(key)) { expanded.delete(key); renderRecents(); return; }
    expanded.add(key);
    renderRecents();
    await loadHistory(entry);
    renderRecents();
}

function openGroupSettings(entry) {
    showPage('groups', {group: entry.group});
}

async function newChatFor(entry) {
    hideLibrary();
    const c = ctx();
    try {
        if (entry.group) {
            if (c.groupId !== entry.group) await c.openGroupChat(entry.group, entry.chats[0].name);
        } else {
            const index = c.characters.findIndex(x => x.avatar === entry.avatar);
            if (index < 0) return toastr.error('That character no longer exists.');
            if (c.groupId || String(c.characterId) !== String(index)) await c.selectCharacterById(index);
        }
        $q('#option_start_new_chat')?.click();
    } catch (error) {
        console.error('[Rocsy] new chat', error);
        toastr.error('Couldn’t start a new chat.');
    }
    closeSidebarOnMobile();
}

function renderRecents() {
    const list = $q('#rvo-recents');
    if (!list || renaming) return;
    const query = ($q('#rvo-search')?.value || '').trim().toLowerCase();
    const c = ctx();
    const current = c.getCurrentChatId?.();
    const currentKey = c.groupId ? `g:${c.groupId}` : c.characters[c.characterId] ? `c:${c.characters[c.characterId].avatar}` : '';
    // One entry per character / group, in order of their latest chat.
    const entries = new Map();
    for (const item of recents) {
        const key = entryKey(item);
        if (!entries.has(key)) entries.set(key, {...item, chats: []});
        entries.get(key).chats.push(item);
    }
    const nodes = [];
    for (const [key, entry] of entries) {
        const all = (histories.get(key) || entry.chats).filter(chat => !CP.isArchived(chat) || chat.name === current)
            .sort((a, b) => (CP.isPinned(b) - CP.isPinned(a)) || 0);
        if (!all.length && !query) continue;
        const nameHit = !query || entry.who.toLowerCase().includes(query);
        const chats = query && !nameHit ? all.filter(chat => `${chat.name} ${chat.preview}`.toLowerCase().includes(query)) : all;
        if (query && !nameHit && !chats.length) continue;
        const open = expanded.has(key) || (query && !nameHit);
        const avatar = entry.thumb ? el('img', {class: 'rvo-avatar', src: entry.thumb, alt: '', loading: 'lazy'})
            : el('span', {class: 'rvo-avatar rvo-avatar-group'}, icon('fa-users'));
        const count = histories.has(key) ? all.length : null;
        nodes.push(el('div', {class: `rvo-person${open ? ' open' : ''}${key === currentKey ? ' current' : ''}`},
            el('div', {class: 'rvo-person-row'},
                el('button', {class: 'rvo-chat rvo-person-main', type: 'button', 'aria-expanded': String(!!open),
                    title: open ? 'Hide chats' : `Show ${entry.who}’s chats`, onclick: () => toggleEntry(entry)},
                    avatar, el('span', {class: 'rvo-chat-text'},
                        el('span', {class: 'rvo-chat-who'}, el('span', {text: entry.who}),
                            el('small', {class: 'rvo-when', text: shortWhen(entry.when)})),
                        el('span', {class: 'rvo-chat-preview', text: entry.preview || chatTitle(entry.name, entry.who)})),
                    el('i', {class: 'fa-solid fa-chevron-down fa-fw rvo-caret', 'aria-hidden': 'true'})),
                el('button', {class: 'rvo-icon-button rvo-person-act', type: 'button', title: `${entry.who}’s settings`,
                    onclick: () => (entry.group ? openGroupSettings(entry) : showPage('characters', {avatar: entry.avatar}))}, icon('fa-gear')),
                el('button', {class: 'rvo-icon-button rvo-person-act rvo-newchat', type: 'button', title: `Start a new chat with ${entry.who}`, 'aria-label': `Start a new chat with ${entry.who}`,
                    onclick: () => newChatFor(entry)}, icon('fa-comment-medical'))),
            open ? el('div', {class: 'rvo-person-chats'},
                ...(chats.length ? chats.map(chat => {
                    const info = titleInfo(chat);
                    const name = el('span', {class: `rvo-history-name${info?.by === 'agent' ? ' by-agent' : ''}`, text: displayTitle(chat, entry.who),
                        title: info?.by === 'agent' ? 'Named automatically' : info?.by === 'user' ? 'Named by you' : chat.name});
                    const holder = el('span', {class: 'rvo-history-title'}, CP.isPinned(chat) ? icon('fa-thumbtack rvo-pinmark') : null, name, el('small', {class: 'rvo-when', text: shortWhen(chat.when)}));
                    const open = el('button', {class: `rvo-chat rvo-history${chat.name === current ? ' active' : ''}`, type: 'button', title: chat.name,
                        onclick: () => openRecent(chat), ondblclick: event => { event.preventDefault(); startRename(chat, entry.who, holder); },
                        onkeydown: event => { if (event.key === 'F2') { event.preventDefault(); startRename(chat, entry.who, holder); } },
                    }, el('span', {class: 'rvo-chat-text'}, holder,
                        el('span', {class: 'rvo-chat-preview', text: chat.preview || 'No messages yet'})));
                    return el('div', {class: 'rvo-history-row'}, open,
                        el('button', {class: 'rvo-icon-button rvo-history-rename', type: 'button', title: 'Rename (F2)',
                            onclick: () => startRename(chat, entry.who, holder)}, icon('fa-pencil')),
                        moreMenu(null, CP.chatMenuItems({...chat, who: entry.who}, {startRename: () => startRename(chat, entry.who, holder)})));
                }) : [el('p', {class: 'rvo-empty', text: 'No chats.'})]),
                count === null && !entry.group ? el('p', {class: 'rvo-empty', text: 'Loading…'}) : null) : null));
    }
    if (!nodes.length) {
        list.replaceChildren(el('p', {class: 'rvo-empty', text: query ? 'No chats match.' : 'No chats yet. Pick a character to start one.'}));
        return;
    }
    list.replaceChildren(el('div', {class: 'rvo-group-label', text: 'Chats'}), ...nodes);
}

// ------------------------------------------------------------------ header --

function renderHeader() {
    const box = $q('#rvo-title');
    if (!box) return;
    if (page) {
        const count = ctx().characters.filter(ch => ch?.avatar && ch.avatar !== 'default_Assistant.png').length;
        const books = page === 'lorebooks' ? (wiModule?.world_names || []).length : 0;
        const sub = page === 'characters' ? `${count} character${count === 1 ? '' : 's'}` : page === 'lorebooks' ? (lore.book || `${books} lorebook${books === 1 ? '' : 's'}`) : PAGES[page].sub;
        box.replaceChildren(el('span', {class: 'rvo-title-text'}, el('strong', {text: PAGES[page].label}), el('small', {text: sub})),
            el('button', {class: 'rvo-icon-button rvo-library-close', type: 'button', title: 'Back to the chat (Esc)', onclick: hidePage}, icon('fa-xmark')));
        return;
    }
    const c = ctx();
    let name = '', thumb = '', sub = '';
    if (c.groupId) {
        const group = c.groups.find(x => x.id === c.groupId);
        name = group?.name || 'Group chat';
        sub = `${group?.members?.length || 0} characters`;
    } else if (c.characterId !== undefined && c.characters[c.characterId]) {
        const character = c.characters[c.characterId];
        name = character.name;
        thumb = c.getThumbnailUrl('avatar', character.avatar);
        const file = c.getCurrentChatId?.() || '';
        sub = file ? displayTitle({avatar: character.avatar, group: '', name: file}, character.name) : '';
    }
    if (!name) {
        box.replaceChildren(el('span', {class: 'rvo-title-text'}, el('strong', {text: 'RocsyTavern'}),
            el('small', {text: 'Choose a character or a recent chat to begin'})));
        return;
    }
    box.replaceChildren(
        thumb ? el('img', {class: 'rvo-avatar', src: thumb, alt: ''}) : el('span', {class: 'rvo-avatar rvo-avatar-group'}, icon('fa-users')),
        el('button', {class: 'rvo-title-text', type: 'button', title: 'This chat’s checkpoints and branches, and all chats', onclick: () => showPage('chats', {tab: 'here', who: ''})},
            el('strong', {text: name}), el('small', {text: sub})));
}

// ----------------------------------------------------------------- building --

// Rocsy mark (same as the gateway home page); a link back to the Rocsy main menu.
function rocsyLogo() {
    const a = el('a', {class: 'rvo-logo', href: '/', target: '_top', title: 'Rocsy main menu', 'aria-label': 'Rocsy main menu'});
    a.innerHTML = '<svg viewBox="40 140 432 260" width="26" height="16" aria-hidden="true">'
        + '<circle class="rvo-mark-sun" cx="256" cy="268" r="124"/>'
        + '<path class="rvo-mark-bird" d="M256 186 C269 186 276 197 274 210 C320 182 380 162 464 156 C418 200 358 234 292 254 L308 314 L256 294 L204 314 L220 254 C154 234 94 200 48 156 C132 162 192 182 238 210 C236 197 243 186 256 186 Z"/></svg>';
    return a;
}

function sideButton(label, faIcon, onclick, extra = {}) {
    return el('button', {class: 'rvo-nav', type: 'button', onclick, ...extra}, icon(faIcon), el('span', {text: label}));
}

function build() {
    if (built) return;
    built = true;
    startMobileExpression();
    const addons = sideButton('Rocsy Addons', 'fa-wand-sparkles', () => { closeSheets(); globalThis.RocsyAddons?.open(); closeSidebarOnMobile(); },
        {'data-key': 'rocsy-addons', hidden: !globalThis.RocsyAddons});
    // Library (Rocsy Addons: Bot Booru, Chub, datacat.run) sits right under Characters.
    const library = sideButton('Library', 'fa-book-open', () => showPage('library', {home: true}),
        {'data-key': 'library', hidden: !globalThis.RocsyAddons, title: 'Find and import character cards'});
    document.addEventListener('rocsy-addons-ready', () => { addons.removeAttribute('hidden'); library.removeAttribute('hidden'); }, {once: true});
    const settingsGroup = el('div', {class: 'rvo-settings-list', id: 'rvo-settings-list'}, addons,
        ...SETTINGS.map(item => sideButton(item.label, item.icon, () => (item.page ? showPage(item.page, item.options || {book: '', settings: true}) : openSetting(item.open)), {'data-key': item.key, title: item.title})));
    const themeSelect = el('select', {id: 'rvo-theme-quick', 'aria-label': 'Theme', onchange: event => setTheme(event.target.value)},
        ...Object.entries(THEMES).map(([value, label]) => el('option', {value, text: label})));

    const sidebar = el('aside', {id: 'rvo-sidebar', 'aria-label': 'Rocsy chat navigation'},
        el('div', {class: 'rvo-brand'},
            rocsyLogo(),
            el('strong', {text: 'RocsyTavern'}),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Hide sidebar (Ctrl+B)', onclick: toggleSidebar}, icon('fa-angles-left'))),
        // Lorebooks (World Info) in the old "New chat" spot: new chats are in the header and on each character row.
        sideButton('Lorebooks', 'fa-book-atlas', () => showPage('lorebooks', {book: ''}), {'data-key': 'lorebooks', title: 'Your lorebooks (World Info)'}),
        // Always the gallery, also from a character's page or when Characters is already open.
        sideButton('Characters', 'fa-address-card', () => showPage('characters', {avatar: ''}), {'data-key': 'characters'}),
        sideButton('Groups', 'fa-users', () => showPage('groups', {group: ''}), {'data-key': 'groups', title: 'Group chats'}),
        sideButton('Chats', 'fa-comments', () => showPage('chats', {tab: 'all', who: ''}), {'data-key': 'chats', title: 'Every chat, branches and backups (Ctrl+Shift+H)'}),
        library,
        el('label', {class: 'rvo-search'}, icon('fa-magnifying-glass'),
            el('input', {id: 'rvo-search', type: 'search', placeholder: 'Search chats  (Ctrl+K)', oninput: renderRecents})),
        el('nav', {id: 'rvo-recents', class: 'rvo-recents', 'aria-label': 'Recent chats'}),
        el('details', {class: 'rvo-settings'},
            el('summary', {}, icon('fa-gear'), el('span', {text: 'Settings'})), settingsGroup),
        el('div', {class: 'rvo-footer'},
            el('label', {class: 'rvo-theme-row'}, icon('fa-palette'), themeSelect),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Switch to the classic SillyTavern layout', onclick: () => setEnabled(false)}, icon('fa-table-columns'))));

    const header = el('header', {id: 'rvo-header'},
        el('button', {class: 'rvo-icon-button rvo-menu', type: 'button', title: 'Show sidebar (Ctrl+B)', onclick: toggleSidebar}, icon('fa-bars')),
        el('div', {id: 'rvo-title', class: 'rvo-title'}),
        el('div', {class: 'rvo-header-actions'},
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Chats with this character (Ctrl+Shift+H)', onclick: () => {
                const c = ctx();
                const who = c.groupId ? `g:${c.groupId}` : c.characters[c.characterId] ? `c:${c.characters[c.characterId].avatar}` : '';
                showPage('chats', {tab: 'all', who});
            }}, icon('fa-clock-rotate-left')),
            el('button', {class: 'rvo-icon-button rvo-newchat', type: 'button', title: 'Start a new chat', 'aria-label': 'Start a new chat', onclick: newChat}, icon('fa-comment-medical')),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Character details', onclick: () => {
                const ch = ctx().characters[ctx().characterId];
                if (ctx().groupId) showPage('groups', {group: ctx().groupId});
                else if (ch) showPage('characters', {avatar: ch.avatar});
                else showPage('characters', {avatar: ''});
            }}, icon('fa-id-card')),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Model & connection', onclick: () => showPage('connection')}, icon('fa-plug'))));

    const scrim = el('div', {id: 'rvo-scrim', onclick: () => { closeSheets(); document.body.classList.remove('rvo-side-open'); }});
    const jump = el('button', {id: 'rvo-jump', type: 'button', title: 'Jump to the latest message',
        onclick: () => { const chat = $q('#chat'); chat?.scrollTo({top: chat.scrollHeight, behavior: 'smooth'}); }}, icon('fa-arrow-down'));
    document.body.append(sidebar, header, scrim);
    $q('#sheld')?.append(jump);
    $q('#chat')?.addEventListener('scroll', onChatScroll, {passive: true});
    themeSelect.value = settings.theme;
    for (const [drawerId, title] of Object.entries(SHEET_TITLES)) {
        const drawer = document.getElementById(drawerId);
        const panel = drawer?.querySelector(':scope > .drawer-content');
        if (!panel || panel.querySelector(':scope > .rvo-sheet-head')) continue;
        panel.prepend(el('div', {class: 'rvo-sheet-head'}, el('strong', {text: title}),
            el('button', {class: 'rvo-icon-button', type: 'button', title: 'Close (Esc)',
                onclick: () => toggleDrawer(drawer)}, icon('fa-xmark'))));
    }
    renderHeader();
    loadRecents();
}

// ------------------------------------------------------ phone expression sprite --
// SillyTavern hides its expression sprite on narrow screens (mobile-styles.css). On phones Rocsy shows the same
// sprite as a small portrait in the chat's top corner: tap it to make it big, tap again to shrink, the × folds it
// into a small mask button (tap to bring it back). It follows SillyTavern's own #expression-image, so whatever the
// classifier picks shows here too.
// Display (settings.mobileExpressionMode): 'corner' (the portrait), 'background' (the sprite fills the chat
// background behind the messages, dimmed to settings.mobileExpressionDim %), or 'off'.
const mexpr = {observer: null, node: null, bg: null, src: '', label: ''};
const MEXPR_STATES = ['small', 'big', 'mini'];
const MEXPR_MODES = ['corner', 'background', 'off'];
const mexprState = () => MEXPR_STATES.includes(settings.mobileExpression) ? settings.mobileExpression : 'small';
const mexprMode = () => MEXPR_MODES.includes(settings.mobileExpressionMode) ? settings.mobileExpressionMode : 'corner';
const mexprDim = () => Math.max(10, Math.min(100, Number(settings.mobileExpressionDim) || 35));

const narrow = window.matchMedia('(max-width: 1000px)');
function applyMexprMode() {
    document.body.classList.toggle('rvo-mexpr-on', narrow.matches || !!settings.desktopExpression);
    document.body.dataset.rvoMexpr = mexprMode();
    document.body.style.setProperty('--rvo-mexpr-dim', String(mexprDim() / 100));
    if (mexpr.bg && !mexpr.bg.isConnected) document.getElementById('sheld')?.prepend(mexpr.bg);
}
/** For the Expressions & images settings page. */
const mobileExpressionSettings = {
    mode: mexprMode, dim: mexprDim,
    setMode(mode) { settings.mobileExpressionMode = MEXPR_MODES.includes(mode) ? mode : 'corner'; save(); applyMexprMode(); },
    setDim(value) { settings.mobileExpressionDim = Math.max(10, Math.min(100, Number(value) || 35)); save(); applyMexprMode(); },
};

function setMexprState(state) {
    settings.mobileExpression = state;
    save();
    if (mexpr.node) mexpr.node.dataset.state = state;
}

function syncMobileExpression() {
    const node = mexpr.node;
    if (!node) return;
    const img = document.getElementById('expression-image');
    const src = img?.getAttribute('src') || '';
    const usable = !!src && !img.classList.contains('default') && (ctx().characterId !== undefined || !!ctx().groupId);
    const label = img?.dataset.expression || img?.getAttribute('title') || '';
    node.hidden = !usable;
    if (mexpr.bg) mexpr.bg.hidden = !usable;
    if (!usable || (src === mexpr.src && label === mexpr.label)) return;
    mexpr.src = src; mexpr.label = label;
    if (mexpr.bg) {
        const bgPic = mexpr.bg.querySelector('img');
        bgPic.classList.add('swap');
        const load = new Image();
        load.onload = load.onerror = () => { bgPic.src = src; bgPic.classList.remove('swap'); };
        load.src = src;
    }
    const pic = node.querySelector('.rvo-mexpr-img');
    pic.classList.add('swap');
    const next = new Image();
    next.onload = next.onerror = () => { pic.src = src; pic.classList.remove('swap'); };
    next.src = src;
    node.querySelector('.rvo-mexpr-label').textContent = label;
    const who = img.dataset.spriteFolderName || ctx().name2 || '';
    node.querySelector('.rvo-mexpr-card').setAttribute('aria-label', `${who.split('/')[0]}${label ? `: ${label}` : ''} (tap to resize)`);
}

function startMobileExpression() {
    if (mexpr.node) return;
    const card = el('button', {class: 'rvo-mexpr-card', type: 'button',
        onclick: () => setMexprState(mexprState() === 'big' ? 'small' : 'big')},
        el('img', {class: 'rvo-mexpr-img', alt: ''}), el('span', {class: 'rvo-mexpr-label'}));
    mexpr.node = el('div', {id: 'rvo-mexpr', hidden: true, 'data-state': mexprState()},
        card,
        el('button', {class: 'rvo-mexpr-hide', type: 'button', title: 'Hide the expression', 'aria-label': 'Hide the expression', onclick: () => setMexprState('mini')}, icon('fa-xmark')),
        el('button', {class: 'rvo-mexpr-asbg', type: 'button', title: 'Show it as the chat background instead', 'aria-label': 'Use as chat background',
            onclick: () => { mobileExpressionSettings.setMode('background'); setMexprState('small'); toastr.info('The expression is now the chat background. Change it in Settings → Expressions & images.', 'Expressions'); }}, icon('fa-image')),
        el('button', {class: 'rvo-mexpr-show', type: 'button', title: 'Show the expression', 'aria-label': 'Show the expression', onclick: () => setMexprState('small')}, icon('fa-masks-theater')));
    document.body.append(mexpr.node);
    mexpr.bg = el('div', {id: 'rvo-mexpr-bg', hidden: true, 'aria-hidden': 'true'}, el('img', {alt: ''}));
    applyMexprMode();
    narrow.addEventListener?.('change', applyMexprMode);
    const target = document.getElementById('expression-wrapper') || document.body;
    let queued = 0;
    mexpr.observer = new MutationObserver(() => { if (!queued) queued = requestAnimationFrame(() => { queued = 0; syncMobileExpression(); }); });
    mexpr.observer.observe(target, {subtree: true, childList: true, attributes: true, attributeFilter: ['src', 'class', 'id', 'data-expression']});
    if (target === document.body) setTimeout(() => {   // Expressions made its wrapper after us: watch that instead
        const wrapper = document.getElementById('expression-wrapper');
        if (wrapper && mexpr.observer) { mexpr.observer.disconnect(); mexpr.observer.observe(wrapper, {subtree: true, childList: true, attributes: true, attributeFilter: ['src', 'class', 'id', 'data-expression']}); }
    }, 3000);
    const events = ctx().eventTypes;
    ctx().eventSource.on(events.CHAT_CHANGED, () => setTimeout(syncMobileExpression, 300));
    syncMobileExpression();
}

function stopMobileExpression() {
    mexpr.observer?.disconnect();
    mexpr.observer = null;
    mexpr.node?.remove();
    mexpr.node = null;
    mexpr.bg?.remove();
    mexpr.bg = null;
    delete document.body.dataset.rvoMexpr;
    document.body.classList.remove('rvo-mexpr-on');
    narrow.removeEventListener?.('change', applyMexprMode);
    mexpr.src = mexpr.label = '';
}

function teardown() {
    if (!built) return;
    stopMobileExpression();
    built = false;
    page = '';
    document.body.classList.remove('rvo-page-open');
    delete document.body.dataset.rvoPage;
    for (const id of ['rvo-sidebar', 'rvo-header', 'rvo-scrim', 'rvo-jump']) document.getElementById(id)?.remove();
    document.querySelectorAll('.rvo-page').forEach(node => node.remove());
    document.querySelectorAll('.rvo-sheet-head').forEach(node => node.remove());
    $q('#chat')?.removeEventListener('scroll', onChatScroll);
}

function onChatScroll() {
    const chat = $q('#chat');
    const far = chat && chat.scrollHeight - chat.scrollTop - chat.clientHeight > 600;
    $q('#rvo-jump')?.classList.toggle('show', !!far);
}

// ------------------------------------------------------------ apply state --

function applyClasses() {
    const body = document.body;
    body.classList.toggle('rvo', settings.enabled);
    body.dataset.rvoTheme = settings.theme;
    body.classList.toggle('rvo-collapsed', settings.enabled && settings.sidebarCollapsed);
    body.classList.toggle('rvo-details', settings.showDetails);
    body.classList.toggle('rvo-bg', settings.showBackground);
    body.classList.toggle('rvo-wide', settings.wideChat);
    const quick = $q('#rvo-theme-quick');
    if (quick) quick.value = settings.theme;
    $q('#rvo-restore')?.classList.toggle('displayNone', settings.enabled);
}

function setEnabled(value) {
    settings.enabled = value;
    save();
    if (value) build(); else { closeSheets(); teardown(); }
    applyClasses();
    syncPanel();
    if (!value) toastr.info('Classic layout. Turn the Rocsy layout back on with the wand icon in the top bar, or in Extensions → Rocsy Visual Overhaul.');
}

function setTheme(value) {
    if (!THEMES[value]) return;
    settings.theme = value;
    save();
    applyClasses();
    syncPanel();
}

function toggleSidebar() {
    if (isPhone()) {
        if (document.body.classList.toggle('rvo-side-open')) navPush({overlay: 'sidebar'});
        else navPush();
        return;
    }
    settings.sidebarCollapsed = !settings.sidebarCollapsed;
    save();
    applyClasses();
}

function closeSidebarOnMobile() {
    document.body.classList.remove('rvo-side-open');
}

// ------------------------------------------------- extension settings panel --

function syncPanel() {
    const panel = $q('#rvo-panel');
    if (!panel) return;
    $q('#rvo-enabled', panel).checked = settings.enabled;
    $q('#rvo-theme', panel).value = settings.theme;
    $q('#rvo-details-opt', panel).checked = settings.showDetails;
    $q('#rvo-bg-opt', panel).checked = settings.showBackground;
    $q('#rvo-wide-opt', panel).checked = settings.wideChat;
}

function buildPanel() {
    const target = $q('#extensions_settings2') || $q('#extensions_settings');
    if (!target || $q('#rvo-panel')) return;
    const option = (id, label, key) => el('label', {class: 'checkbox_label', for: id},
        el('input', {id, type: 'checkbox', onchange: event => { settings[key] = event.target.checked; save(); applyClasses(); }}),
        el('span', {text: label}));
    const panel = el('div', {id: 'rvo-panel', class: 'inline-drawer'},
        el('div', {class: 'inline-drawer-toggle inline-drawer-header'},
            el('b', {text: 'Rocsy Visual Overhaul'}), el('div', {class: 'inline-drawer-icon fa-solid fa-circle-chevron-down down'})),
        el('div', {class: 'inline-drawer-content'},
            el('label', {class: 'checkbox_label', for: 'rvo-enabled'},
                el('input', {id: 'rvo-enabled', type: 'checkbox', onchange: event => setEnabled(event.target.checked)}),
                el('span', {text: 'Use the Rocsy layout (sidebar, chat header, settings sheet)'})),
            el('label', {for: 'rvo-theme', text: 'Theme'}),
            el('select', {id: 'rvo-theme', class: 'text_pole', onchange: event => setTheme(event.target.value)},
                ...Object.entries(THEMES).map(([value, label]) => el('option', {value, text: label}))),
            option('rvo-details-opt', 'Show message details (number, timer, tokens)', 'showDetails'),
            option('rvo-bg-opt', 'Show the chat background picture', 'showBackground'),
            option('rvo-wide-opt', 'Wide chat column', 'wideChat'),
            el('small', {text: 'Shortcuts: Ctrl+/ lists them all · Ctrl+Shift+O new chat · Ctrl+Shift+H chats · Ctrl+K search chats · Ctrl+B sidebar · Esc closes a panel. /rocsy-ui on|off switches the layout.'})));
    target.append(panel);
    syncPanel();
}

function buildRestoreIcon() {
    const holder = $q('#top-settings-holder');
    if (!holder || $q('#rvo-restore')) return;
    holder.append(el('div', {id: 'rvo-restore', class: 'drawer'},
        el('div', {class: 'drawer-toggle'},
            el('div', {class: 'drawer-icon fa-solid fa-wand-magic-sparkles fa-fw closedIcon', title: 'Switch to the Rocsy layout',
                onclick: () => setEnabled(true)}))));
}

// ---------------------------------------------------------------- shortcuts --

function onKey(event) {
    if (!settings.enabled) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && page === 'characters' && chars.view === 'edit') { event.preventDefault(); flushEdits(); return; }
    const mod = event.ctrlKey || event.metaKey;
    if (mod && event.shiftKey && event.key.toLowerCase() === 'o') { event.preventDefault(); newChat(); }
    else if (mod && !event.shiftKey && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        if (isPhone()) document.body.classList.add('rvo-side-open');
        else if (settings.sidebarCollapsed) toggleSidebar();
        $q('#rvo-search')?.focus();
    } else if (mod && !event.shiftKey && event.key.toLowerCase() === 'b') { event.preventDefault(); toggleSidebar(); }
    else if (event.key === 'Escape' && (openDrawers().length || document.body.classList.contains('rvo-side-open'))) {
        closeSheets();
        closeSidebarOnMobile();
    } else if (event.key === 'Escape' && page && !document.querySelector('dialog[open], .popup[open]') && !event.target.closest?.('input, textarea')) hidePage();
}

// --------------------------------------------------------------------- init --

function registerCommand() {
    try {
        const c = ctx();
        c.SlashCommandParser.addCommandObject(c.SlashCommand.fromProps({
            name: 'rocsy-ui',
            callback: (_args, value) => {
                const text = String(value || '').trim().toLowerCase();
                setEnabled(text === 'off' ? false : text === 'on' ? true : !settings.enabled);
                return '';
            },
            helpString: 'Switch the Rocsy layout: /rocsy-ui on, /rocsy-ui off, or /rocsy-ui to toggle.',
        }));
    } catch (error) {
        console.warn('[Rocsy] slash command', error);
    }
}

function init() {
    loadSettings();
    const layout = {
        get: key => settings[key],
        set(key, value) {
            if (key === 'enabled') return setEnabled(!!value);
            settings[key] = value; save(); applyClasses(); syncPanel();
            if (key === 'desktopExpression') applyMexprMode();
        },
        setTheme,
    };
    SP.setup({el, icon, ctx, showPage, hidePage, renderHeader, rvoButton, switchControl, closeSidebarOnMobile, page: () => page,
        statTiles, sideCard, facts, heroBackdrop, pageBanner, filterChips, compactNumber, mobileExpressionSettings, layout, showShortcuts: CT.showShortcuts});
    CP.setup({el, icon, ctx, showPage, hidePage, page: () => page, settings: () => settings, save, openRecent, displayTitle, titleInfo, startRename,
        pageBanner, filterChips, sideCard, statTiles, facts, rvoButton, moreMenu, refreshSidebar: () => scheduleRecents(200),
        menuAction: CT.menuAction, chatMenu: () => CT.chatMenu()});
    CT.setup({el, icon, ctx, showPage, active: () => built});
    try { CT.init(); } catch (error) { console.warn('[Rocsy] chat tools', error); }
    try { SP.init(); } catch (error) { console.warn('[Rocsy] settings pages', error); }
    buildPanel();
    buildRestoreIcon();
    applyClasses();
    if (settings.enabled) build();
    document.addEventListener('keydown', onKey);
    document.addEventListener('rocsy-library-close', hidePage);
    document.addEventListener('rocsy-library-nav', () => { if (page === 'library') navPush(); });
    window.addEventListener('popstate', onPopState);
    navReset();
    document.addEventListener('rocsy-chat-titles', () => { renderRecents(); renderHeader(); });
    globalThis.RocsyVisualOverhaul = {showLibrary, hideLibrary, openLorebook, showPage, showExpressionGallery, showLorebooks: () => showPage('lorebooks'), active: () => built};
    worldInfo().catch(error => console.warn('[Rocsy Visual Overhaul] World Info module not loaded', error));
    document.addEventListener('rocsy-lorebooks-changed', () => setTimeout(refreshLorebooksPage, 100));
    const {eventSource, eventTypes} = ctx();
    // Lorebooks page: entries edited, global list switched, a book imported / created / deleted in SillyTavern's panel.
    if (eventTypes.WORLDINFO_UPDATED) eventSource.on(eventTypes.WORLDINFO_UPDATED, (name, data) => {
        if (name && data?.entries) loreEntries.set(name, Object.keys(data.entries).length);
        refreshLorebooksPage();
    });
    $(document).on('change', '#world_info, #world_editor_select', () => setTimeout(refreshLorebooksPage, 50));
    const refresh = () => { renderHeader(); renderRecents(); scheduleRecents(); CP.invalidate(); if (page === 'chats') CP.refresh(); };
    // Tags nobody uses any more go when a character is deleted (pruneUnusedTags), and once after start-up.
    if (eventTypes.CHARACTER_DELETED) eventSource.on(eventTypes.CHARACTER_DELETED, () => setTimeout(pruneUnusedTags, 500));
    setTimeout(pruneUnusedTags, 8000);
    for (const type of ['CHARACTER_EDITED', 'CHARACTER_DELETED', 'CHARACTER_PAGE_LOADED']) {
        if (eventTypes[type]) eventSource.on(eventTypes[type], () => setTimeout(refreshCharactersPage, 200));
    }
    for (const type of ['CHAT_CHANGED', 'CHARACTER_EDITED', 'CHARACTER_DELETED', 'GROUP_UPDATED', 'CHAT_CREATED', 'GROUP_CHAT_CREATED']) {
        if (eventTypes[type]) eventSource.on(eventTypes[type], refresh);
    }
    for (const type of ['MESSAGE_SENT', 'MESSAGE_RECEIVED']) {
        if (eventTypes[type]) eventSource.on(eventTypes[type], () => { scheduleRecents(3000); CP.invalidate(); });
    }
    for (const type of ['CHAT_DELETED', 'GROUP_CHAT_DELETED']) {
        if (eventTypes[type]) eventSource.on(eventTypes[type], () => { CP.invalidate(); scheduleRecents(300); if (page === 'chats') CP.refresh(); });
    }
    registerCommand();
}

const ready = () => {
    try { init(); } catch (error) { console.error('[Rocsy] Visual Overhaul failed to start', error); }
};
// Built at APP_INITIALIZED, while SillyTavern's splash still covers the page, so the Rocsy layout is already
// there when it lifts (no flash of the classic layout). The event re-fires for late listeners.
const {eventSource, eventTypes} = ctx();
const startEvent = eventTypes.APP_INITIALIZED || eventTypes.APP_READY;
if (startEvent) eventSource.once(startEvent, ready); else ready();
