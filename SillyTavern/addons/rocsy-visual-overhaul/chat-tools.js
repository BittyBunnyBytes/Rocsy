// Chat tools (Rocsy Visual Overhaul): the ☰ menu by the chat box, unsent drafts per chat, keyboard shortcuts and
// the home-screen app link.
//
// - Chat menu: in the Rocsy layout the ☰ button opens Rocsy's own menu (SillyTavern's actions underneath: its
//   #option_* items are clicked, so everything they do still happens). Author's Note opens the in-site page.
// - Drafts: what you typed but didn't send is kept per chat (localStorage), so switching chats or a phone
//   reloading the page doesn't lose it.
// - Shortcuts: Ctrl+/ (or ? outside a text box) lists them; Rocsy adds Ctrl+Shift+H (Chats page) and
//   Ctrl+Shift+A (Author's Note).
// - Home screen: a web app manifest (served by Rocsy Addons) so "Add to Home screen" opens RocsyTavern like an app.

let H = null;
export function setup(hooks) { H = hooks; }
const el = (...args) => H.el(...args);
const icon = name => H.icon(name);
const ctx = () => H.ctx();
const $q = (selector, root = document) => root.querySelector(selector);

// --------------------------------------------------------------- chat menu --
const visible = node => !!node && getComputedStyle(node).display !== 'none' && !node.classList.contains('displayNone');

/** Click one of SillyTavern's #options items (it does the work). */
export function menuAction(id) {
    const node = document.getElementById(id);
    if (!node) return toastr.warning('That isn’t available in this SillyTavern version.');
    $(node).trigger('click');
}

let menuNode = null;
function closeMenu() {
    menuNode?.remove();
    menuNode = null;
    document.removeEventListener('pointerdown', outside, true);
    document.removeEventListener('keydown', escape, true);
}
const outside = event => { if (menuNode && !menuNode.contains(event.target) && !event.target.closest?.('#options_button')) closeMenu(); };
const escape = event => { if (event.key === 'Escape') { event.stopPropagation(); closeMenu(); } };

export async function chatMenu(anchor = document.getElementById('options_button')) {
    if (menuNode) return closeMenu();
    const c = ctx();
    try { (await import('../../../bookmarks.js')).showBookmarksButtons(); } catch { /* older SillyTavern */ }
    const open = !!c.getCurrentChatId?.() && (c.characterId !== undefined || !!c.groupId);
    const st = id => document.getElementById(id);
    const item = (label, iconName, run, {hint = '', when = true, danger = false} = {}) => when ? el('button', {type: 'button', role: 'menuitem', class: danger ? 'danger' : '',
        onclick: () => { closeMenu(); run(); }}, icon(iconName), el('span', {text: label}), hint ? el('kbd', {text: hint}) : null) : null;
    const sep = () => el('hr');
    const generating = !!$q('#mes_stop') && visible($q('#mes_stop'));
    menuNode = el('div', {class: 'rvo-chatmenu', role: 'menu', 'aria-label': 'Chat menu'},
        item('Start a new chat', 'fa-comment-medical', () => menuAction('option_start_new_chat'), {when: open || c.characterId !== undefined, hint: 'Ctrl+Shift+O'}),
        item('Chats, branches & backups', 'fa-comments', () => H.showPage('chats', {tab: open ? 'here' : 'all'}), {hint: 'Ctrl+Shift+H'}),
        open ? sep() : null,
        item('Regenerate', 'fa-repeat', () => menuAction('option_regenerate'), {when: open && !generating, hint: 'Ctrl+Enter'}),
        item('Continue the last reply', 'fa-arrow-right', () => menuAction('option_continue'), {when: open && !generating, hint: 'Alt+Enter'}),
        item('Impersonate (write my reply)', 'fa-user-secret', () => menuAction('option_impersonate'), {when: open && !generating}),
        item('Delete messages…', 'fa-trash-can', () => menuAction('option_delete_mes'), {when: open && !generating}),
        open ? sep() : null,
        item('Author’s Note', 'fa-note-sticky', () => H.showPage('authorsnote'), {when: open, hint: 'Ctrl+Shift+A'}),
        item('Save a checkpoint', 'fa-flag', () => menuAction('option_new_bookmark'), {when: open && visible(st('option_new_bookmark'))}),
        item('Back to the parent chat', 'fa-arrow-turn-up', () => menuAction('option_back_to_main'), {when: open && !!c.chatMetadata?.main_chat}),
        item('Turn into a group chat', 'fa-users', () => menuAction('option_convert_to_group'), {when: open && !c.groupId && visible(st('option_convert_to_group'))}),
        item('Token probabilities', 'fa-pie-chart', () => menuAction('option_toggle_logprobs'), {when: open && visible(st('option_toggle_logprobs'))}),
        sep(),
        item('Keyboard shortcuts', 'fa-keyboard', showShortcuts, {hint: 'Ctrl+/'}),
        item('Close this chat', 'fa-xmark', () => menuAction('option_close_chat'), {when: open}));
    // No two separators in a row / at the ends.
    [...menuNode.children].forEach((node, i, all) => { if (node.tagName === 'HR' && (i === 0 || all[i - 1]?.tagName === 'HR' || i === all.length - 1)) node.remove(); });
    document.body.append(menuNode);
    const r = (anchor || document.getElementById('send_form'))?.getBoundingClientRect() || {left: 12, top: innerHeight - 60};
    menuNode.style.left = `${Math.max(8, Math.min(r.left, innerWidth - menuNode.offsetWidth - 8))}px`;
    // top, not bottom: on phones SillyTavern's CSS makes <body> the (zero-height) box fixed elements are placed in.
    menuNode.style.top = `${Math.max(8, r.top - 8 - menuNode.offsetHeight)}px`;
    menuNode.querySelector('button')?.focus({preventScroll: true});
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', escape, true);
}

function hookMenuButton() {
    // Capture phase, before SillyTavern's own handler opens its menu.
    const handler = event => {
        if (!H.active()) return;
        if (!event.target.closest?.('#options_button')) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (event.type === 'click') chatMenu(event.target.closest('#options_button'));
    };
    document.addEventListener('click', handler, true);
    document.addEventListener('mousedown', handler, true);
    document.addEventListener('touchstart', event => { if (H.active() && event.target.closest?.('#options_button')) event.stopImmediatePropagation(); }, {capture: true, passive: true});
}

// ------------------------------------------------------------------ drafts --
const DRAFTS = 'rocsy-drafts';
const draftKey = () => {
    const c = ctx();
    const id = c.getCurrentChatId?.();
    if (!id) return '';
    return `${c.groupId ? `g:${c.groupId}` : `c:${c.characters[c.characterId]?.avatar || ''}`}/${id}`;
};
const readDrafts = () => { try { return JSON.parse(localStorage.getItem(DRAFTS) || '{}') || {}; } catch { return {}; } };
const writeDrafts = drafts => { try { localStorage.setItem(DRAFTS, JSON.stringify(drafts)); } catch { /* full or blocked */ } };
let draftFor = '';
let draftTimer = 0;
function saveDraft() {
    const box = $q('#send_textarea');
    if (!box || !draftFor) return;
    const drafts = readDrafts();
    const text = box.value;
    if (text.trim()) drafts[draftFor] = {text, at: Date.now()}; else delete drafts[draftFor];
    // Keep it small: the 60 newest drafts.
    const keys = Object.keys(drafts).sort((a, b) => drafts[b].at - drafts[a].at);
    for (const key of keys.slice(60)) delete drafts[key];
    writeDrafts(drafts);
}
function switchDraft() {
    const box = $q('#send_textarea');
    if (!box) return;
    const next = draftKey();
    if (next === draftFor) return;
    const previous = draftFor;
    draftFor = next;
    const drafts = readDrafts();
    const saved = drafts[next]?.text || '';
    // The box still holds the previous chat's draft (already saved under it), or SillyTavern's start-up restore.
    const before = previous ? drafts[previous]?.text || '' : '';
    if (!box.value.trim() || box.value === before || !previous) {
        if (box.value === saved) return;
        if (!previous && box.value.trim() && !saved) { saveDraft(); return; }   // first chat after start: keep what SillyTavern restored
        box.value = saved;
        box.dispatchEvent(new Event('input', {bubbles: true}));
    }
}
function hookDrafts() {
    const {eventSource, eventTypes} = ctx();
    $(document).on('input', '#send_textarea', () => { clearTimeout(draftTimer); draftTimer = setTimeout(saveDraft, 400); });
    eventSource.on(eventTypes.CHAT_CHANGED, () => setTimeout(switchDraft, 50));
    const sent = () => { if (!draftFor) return; const drafts = readDrafts(); delete drafts[draftFor]; writeDrafts(drafts); };
    if (eventTypes.MESSAGE_SENT) eventSource.on(eventTypes.MESSAGE_SENT, sent);
    addEventListener('pagehide', saveDraft);
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') saveDraft(); });
}

// --------------------------------------------------------------- shortcuts --
const SHORTCUTS = [
    ['Chat', [['Enter', 'Send (Shift+Enter: new line)'], ['Ctrl+Enter', 'Regenerate the last reply'], ['Alt+Enter', 'Continue the last reply'],
        ['Esc', 'Stop the reply being written'], ['← / →', 'Swipe the last reply (chat box empty)'], ['↑', 'Edit the last message (chat box empty)'],
        ['Ctrl+↑', 'Edit your last message'], ['Ctrl+Shift+↑', 'Scroll to where the model’s memory starts'], ['Ctrl+Shift+↓', 'Scroll to the bottom']]],
    ['Rocsy', [['Ctrl+K', 'Search chats in the sidebar'], ['Ctrl+B', 'Show / hide the sidebar'], ['Ctrl+Shift+O', 'New chat'],
        ['Ctrl+Shift+H', 'Chats, branches & backups'], ['Ctrl+Shift+A', 'Author’s Note'], ['Ctrl+/ or ?', 'This list'], ['Esc', 'Close a page or panel'],
        ['F2', 'Rename the selected chat'], ['Ctrl+S', 'Save now (character editor)']]],
    ['Phone', [['Swipe ← / →', 'Swipe the last reply'], ['Back', 'Close the sidebar, a page or a picture']]],
];
export async function showShortcuts() {
    const c = ctx();
    const body = el('div', {class: 'rvo-keys'}, ...SHORTCUTS.map(([group, rows]) => el('section', {},
        el('h4', {text: group}), el('dl', {}, ...rows.flatMap(([keys, what]) => [el('dt', {}, ...keys.split(' / ').flatMap((k, i) => [i ? ' / ' : '', el('kbd', {text: k})])), el('dd', {text: what})])))));
    await c.callGenericPopup(body, c.POPUP_TYPE.TEXT, '', {wide: true, okButton: 'Close'});
}

function onKey(event) {
    if (!H.active()) return;
    const mod = event.ctrlKey || event.metaKey;
    const typing = event.target.closest?.('input, textarea, select, [contenteditable="true"]');
    if ((mod && event.key === '/') || (!typing && !mod && event.key === '?')) { event.preventDefault(); showShortcuts(); return; }
    if (mod && event.shiftKey && event.key.toLowerCase() === 'h') { event.preventDefault(); H.showPage('chats', {tab: 'all'}); return; }
    if (mod && event.shiftKey && event.key.toLowerCase() === 'a') { event.preventDefault(); H.showPage('authorsnote'); }
}

// --------------------------------------------------------------- home screen --
function addManifest() {
    if ($q('link[rel="manifest"]')) return;
    const base = location.pathname.replace(/[^/]*$/, '');
    document.head.append(
        el('link', {rel: 'manifest', href: `${base}api/plugins/rocsy/manifest.webmanifest`, crossorigin: 'use-credentials'}),   // behind the Rocsy login
        el('meta', {name: 'mobile-web-app-capable', content: 'yes'}),
        el('meta', {name: 'apple-mobile-web-app-capable', content: 'yes'}),
        el('meta', {name: 'apple-mobile-web-app-title', content: 'RocsyTavern'}),
        el('link', {rel: 'apple-touch-icon', href: `${base}api/plugins/rocsy/app-icon.png?size=180`}));
    if (!$q('meta[name="theme-color"]')) document.head.append(el('meta', {name: 'theme-color', content: '#141c22'}));
}

// ------------------------------------------------------------------- start --
export function init() {
    hookMenuButton();
    hookDrafts();
    document.addEventListener('keydown', onKey);
    addManifest();
}
