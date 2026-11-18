// Chats page (Rocsy Visual Overhaul): every chat of every character and group, in-site.
//
// - All chats: search, sort, filter by character; pin, archive, rename (Rocsy's chat names), duplicate, export
//   (.jsonl / .txt), delete. Branches and checkpoints are shown under the chat they came from (their
//   chat_metadata.main_chat), so a chat's whole tree is in one place.
// - This chat: its checkpoints and branches by message (jump to the message or open the branch), back to the
//   parent chat, save a checkpoint / branch from the last message.
// - Backups: SillyTavern's automatic chat backups (data/<user>/backups), matched to their character and chat;
//   look inside, restore as a new chat (never overwrites), download, delete.
// - chatStatsCard(): statistics worked out from the chat files, for the character page.
// The list itself comes from the Rocsy Addons server plugin (/api/plugins/rocsy/chats/*); deleting, exporting and
// opening use SillyTavern's own endpoints and functions. Pins / archive live in the overhaul's settings.

let H = null;
export function setup(hooks) { H = hooks; }
const el = (...args) => H.el(...args);
const icon = name => H.icon(name);
const ctx = () => H.ctx();
const API = '/api/plugins/rocsy';
const imports = {};
const load = (key, path) => (imports[key] ||= import(path));
const scriptModule = () => load('script', '../../../../script.js');
const groupsModule = () => load('groups', '../../../group-chats.js');
const bookmarksModule = () => load('bookmarks', '../../../bookmarks.js');

export const PAGE = {label: 'Chats', sub: 'Every chat, its branches and backups'};
/** For the overhaul's browser history: the tab and the character filter. */
export const navInfo = () => ({view: state.tab, avatar: state.who});
const state = {tab: 'all', q: '', who: '', sort: 'recent', data: null, backups: null, loading: false, openBackup: '', backupText: new Map(), folded: new Set()};

// ------------------------------------------------------------------ helpers --
const keyOf = item => `${item.group ? `g:${item.group}` : `c:${item.avatar}`}/${item.name}`;
const ownerKey = item => (item.group ? `g:${item.group}` : `c:${item.avatar}`);
const marks = () => {
    const s = H.settings();
    s.chatPins ||= {}; s.chatArchive ||= {};
    return s;
};
export const isPinned = item => !!marks().chatPins[keyOf(item)];
export const isArchived = item => !!marks().chatArchive[keyOf(item)];
function setMark(kind, item, on) {
    const s = marks();
    const list = kind === 'pin' ? s.chatPins : s.chatArchive;
    if (on) list[keyOf(item)] = Date.now(); else delete list[keyOf(item)];
    H.save();
    document.dispatchEvent(new CustomEvent('rocsy-chat-titles'));   // sidebar + header redraw
    redraw();
}
const kindOf = name => /^Checkpoint #|- Checkpoint #| - Branch #|^Branch #/i.test(name) ? (/checkpoint/i.test(name) ? 'checkpoint' : 'branch') : '';
const words = n => `${Number(n || 0).toLocaleString()} ${n === 1 ? 'message' : 'messages'}`;
const get = async path => {
    const response = await fetch(API + path, {headers: ctx().getRequestHeaders(), cache: 'no-store'});
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || `The server answered ${response.status}${response.status === 404 ? ' (Rocsy Addons’ server part is out of date: restart SillyTavern)' : ''}`);
    return response.json();
};
const post = async (path, body) => {
    const response = await fetch(API + path, {method: 'POST', headers: {...ctx().getRequestHeaders(), 'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `The server answered ${response.status}`);
    return data;
};
const when = time => time ? new Date(time).toLocaleString(undefined, {month: 'short', day: 'numeric', year: new Date(time).getFullYear() === new Date().getFullYear() ? undefined : 'numeric', hour: '2-digit', minute: '2-digit'}) : '';
const duration = ms => {
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} h`;
};

// ---------------------------------------------------------------- data --
/** All chats as items {name, who, avatar, group, thumb, when, count, preview, mainChat, size}. */
async function loadAll(force = false) {
    if (state.data && !force) return state.data;
    const {characters, groups} = await get('/chats/all');
    const c = ctx();
    const items = [];
    for (const ch of c.characters) {
        if (!ch?.avatar) continue;
        for (const chat of characters[ch.avatar.replace(/\.png$/, '')] || []) {
            items.push({name: chat.file.replace(/\.jsonl$/, ''), who: ch.name, avatar: ch.avatar, group: '', thumb: c.getThumbnailUrl('avatar', ch.avatar),
                when: chat.last || chat.modified, created: chat.first, count: chat.items, preview: chat.mes, mainChat: chat.mainChat, size: chat.size,
                checkpoints: chat.checkpoints, branches: chat.branches});
        }
    }
    for (const group of c.groups || []) {
        for (const id of group.chats || []) {
            const chat = groups[id];
            if (!chat) continue;
            items.push({name: id, who: group.name, avatar: '', group: group.id, thumb: '', when: chat.last || chat.modified, created: chat.first, count: chat.items,
                preview: chat.mes, mainChat: chat.mainChat, size: chat.size, checkpoints: chat.checkpoints, branches: chat.branches});
        }
    }
    state.data = items;
    return items;
}
export function invalidate() { state.data = null; state.backups = null; }

// ---------------------------------------------------------------- actions --
async function openChat(item) {
    H.hidePage();
    await H.openRecent(item);
}

async function deleteChat(item) {
    const c = ctx();
    const ok = await c.callGenericPopup(`Delete “${H.displayTitle(item, item.who)}”? (${words(item.count)})\n\nSillyTavern keeps automatic backups for a while; they’re on the Backups tab.`, c.POPUP_TYPE.CONFIRM, '', {okButton: 'Delete', cancelButton: 'Keep'});
    if (!ok) return;
    const current = c.getCurrentChatId?.() === item.name && (item.group ? c.groupId === item.group : c.characters[c.characterId]?.avatar === item.avatar);
    try {
        if (item.group) {
            const g = await groupsModule();
            if (current) await g.deleteGroupChat(item.group, item.name); else await g.deleteGroupChatByName(item.group, item.name);
        } else {
            const s = await scriptModule();
            const index = c.characters.findIndex(x => x.avatar === item.avatar);
            if (index < 0) throw new Error('That character no longer exists.');
            await s.deleteCharacterChatByName(String(index), item.name);
            if (current) await s.replaceCurrentChat();
        }
        delete marks().chatPins[keyOf(item)]; delete marks().chatArchive[keyOf(item)]; H.save();
        toastr.success('Chat deleted');
    } catch (error) {
        toastr.error(error.message || String(error), 'Couldn’t delete the chat');
    }
    invalidate();
    H.refreshSidebar();
    redraw();
}

async function exportChat(item, format) {
    try {
        const file = `${item.name}.jsonl`;
        const response = await fetch('/api/chats/export', {method: 'POST', headers: ctx().getRequestHeaders(),
            body: JSON.stringify({is_group: !!item.group, avatar_url: item.avatar, file, exportfilename: `${item.name}.${format}`, format})});
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || `Export failed (${response.status})`);
        const blob = new Blob([data.result], {type: format === 'txt' ? 'text/plain' : 'application/json-lines'});
        const a = el('a', {href: URL.createObjectURL(blob), download: `${item.name}.${format}`});
        document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    } catch (error) {
        toastr.error(error.message, 'Couldn’t export the chat');
    }
}

async function duplicateChat(item) {
    try {
        const {file} = await post('/chats/duplicate', {avatar: item.avatar, group: !!item.group, file: item.name});
        if (item.group) await addToGroup(item.group, file);
        toastr.success(`Copied as “${file}”`);
    } catch (error) {
        toastr.error(error.message, 'Couldn’t duplicate the chat');
    }
    invalidate();
    H.refreshSidebar();
    redraw();
}

async function addToGroup(groupId, chatId) {
    const c = ctx();
    const group = c.groups.find(g => g.id === groupId);
    if (!group) throw new Error('That group no longer exists.');
    if (!group.chats.includes(chatId)) group.chats.push(chatId);
    const g = await groupsModule();
    await g.editGroup(groupId, true, false);
}

/** The ⋯ menu for a chat (Chats page and sidebar). */
export function chatMenuItems(item, {startRename = null} = {}) {
    const pinned = isPinned(item), archived = isArchived(item);
    return [
        ['Open', 'fa-comments', () => openChat(item)],
        startRename ? ['Rename', 'fa-pencil', startRename] : null,
        [pinned ? 'Unpin' : 'Pin to the top', 'fa-thumbtack', () => setMark('pin', item, !pinned)],
        [archived ? 'Unarchive' : 'Archive (hide from the sidebar)', 'fa-box-archive', () => setMark('archive', item, !archived)],
        ['Duplicate', 'fa-clone', () => duplicateChat(item)],
        ['Export (.jsonl)', 'fa-file-export', () => exportChat(item, 'jsonl')],
        ['Export as text (.txt)', 'fa-file-lines', () => exportChat(item, 'txt')],
        ['Delete…', 'fa-trash-can', () => deleteChat(item), true],
    ].filter(Boolean);
}

// ----------------------------------------------------------------- render --
let rootNode = null;
const redraw = () => { if (rootNode && H.page() === 'chats') render(rootNode); };
export function refresh() { invalidate(); redraw(); }

export async function render(root, options = {}) {
    rootNode = root;
    if ('who' in options) state.who = options.who || '';
    if (options.tab) state.tab = options.tab;
    root.classList.add('rvo-spage');
    const token = (root.dataset.token = String(Number(root.dataset.token || 0) + 1));
    if (!root.dataset.drawn) root.replaceChildren(el('div', {class: 'rvo-cwrap'}, el('p', {class: 'rvo-empty', text: 'Loading chats…'})));
    const wrap = el('div', {class: 'rvo-cwrap rvo-chatspage'});
    try {
        const items = await loadAll();
        if (root.dataset.token !== token) return;
        drawPage(wrap, items);
    } catch (error) {
        wrap.replaceChildren(el('p', {class: 'rvo-empty', text: `The chats couldn’t be loaded: ${error.message}`}));
    }
    if (root.dataset.token !== token) return;
    const scroll = root.dataset.drawn ? root.scrollTop : 0;
    root.dataset.drawn = '1';
    root.replaceChildren(wrap);
    root.scrollTop = scroll;
}

function drawPage(wrap, items) {
    const c = ctx();
    const total = items.reduce((sum, item) => sum + (item.count || 0), 0);
    const archivedCount = items.filter(isArchived).length;
    const pinnedCount = items.filter(isPinned).length;
    const owners = new Map();
    for (const item of items) if (!owners.has(ownerKey(item))) owners.set(ownerKey(item), item.who);
    const currentOwner = c.groupId ? `g:${c.groupId}` : c.characters[c.characterId] ? `c:${c.characters[c.characterId].avatar}` : '';
    wrap.append(H.pageBanner('fa-comments', 'Chats', 'All your chats in one place. Branches and checkpoints sit under the chat they came from.',
        [['Chats', items.length, 'fa-comments'], ['Messages', total, 'fa-message'], ['Characters', owners.size, 'fa-address-card']]));
    wrap.append(H.filterChips([['all', 'All chats', items.length - archivedCount], ['here', 'This chat'], ['pinned', 'Pinned', pinnedCount], ['archived', 'Archived', archivedCount], ['backups', 'Backups']],
        state.tab, value => { state.tab = value; redraw(); }));
    if (state.tab === 'backups') return drawBackups(wrap);
    if (state.tab === 'here') return drawThisChat(wrap, items);
    const search = el('input', {type: 'search', class: 'rvo-cinput', placeholder: 'Search names, chat titles and last messages', value: state.q,
        oninput: event => { state.q = event.target.value; drawList(list, items); }});
    const who = el('select', {class: 'rvo-cinput', 'aria-label': 'Character', onchange: event => { state.who = event.target.value; drawList(list, items); }},
        el('option', {value: '', text: 'Everyone'}),
        ...(currentOwner && owners.has(currentOwner) ? [el('option', {value: currentOwner, text: `This chat’s ${currentOwner.startsWith('g:') ? 'group' : 'character'}: ${owners.get(currentOwner)}`})] : []),
        ...[...owners].sort((a, b) => a[1].localeCompare(b[1])).map(([key, name]) => el('option', {value: key, text: name})));
    who.value = owners.has(state.who) ? state.who : '';
    const sort = el('select', {class: 'rvo-cinput', 'aria-label': 'Sort', onchange: event => { state.sort = event.target.value; drawList(list, items); }},
        ...[['recent', 'Last message'], ['oldest', 'Oldest first'], ['longest', 'Most messages'], ['name', 'Character name']].map(([v, t]) => el('option', {value: v, text: t})));
    sort.value = state.sort;
    const list = el('div', {class: 'rvo-chatlist'});
    wrap.append(el('div', {class: 'rvo-chattools'}, el('label', {class: 'rvo-search rvo-chatsearch'}, icon('fa-magnifying-glass'), search), who, sort,
        H.rvoButton('Reload', 'fa-rotate', () => refresh(), 'Read the chats again')), list);
    drawList(list, items);
    setTimeout(() => { if (state.q) search.focus(); }, 0);
}

function drawList(list, items) {
    const q = state.q.trim().toLowerCase();
    const titled = item => H.displayTitle(item, item.who);
    let shown = items.filter(item => (state.tab === 'archived' ? isArchived(item) : state.tab === 'pinned' ? isPinned(item) : !isArchived(item)))
        .filter(item => !state.who || ownerKey(item) === state.who)
        .filter(item => !q || `${item.who} ${titled(item)} ${item.name} ${item.preview}`.toLowerCase().includes(q));
    const order = {recent: (a, b) => b.when - a.when, oldest: (a, b) => (a.created || a.when) - (b.created || b.when), longest: (a, b) => b.count - a.count,
        name: (a, b) => a.who.localeCompare(b.who) || b.when - a.when}[state.sort];
    shown.sort((a, b) => (isPinned(b) - isPinned(a)) || order(a, b));
    if (!shown.length) {
        list.replaceChildren(el('p', {class: 'rvo-empty', text: q || state.who ? 'No chats match.' : state.tab === 'archived' ? 'Nothing archived. Archive a chat to hide it from the sidebar.' : state.tab === 'pinned' ? 'Nothing pinned yet.' : 'No chats yet.'}));
        return;
    }
    // Tree: a branch / checkpoint goes under its parent when the parent is in the list (same character / group).
    const byKey = new Map(shown.map(item => [`${ownerKey(item)}/${item.name}`, item]));
    const children = new Map();
    const roots = [];
    for (const item of shown) {
        const parent = item.mainChat ? byKey.get(`${ownerKey(item)}/${item.mainChat}`) : null;
        if (parent && parent !== item && !q) {
            const k = `${ownerKey(parent)}/${parent.name}`;
            if (!children.has(k)) children.set(k, []);
            children.get(k).push(item);
        } else roots.push(item);
    }
    const nodes = [];
    const current = ctx().getCurrentChatId?.();
    const add = (item, depth, seen = new Set()) => {
        const k = `${ownerKey(item)}/${item.name}`;
        if (seen.has(k)) return;
        seen.add(k);
        const kids = (children.get(k) || []).sort((a, b) => a.when - b.when);
        nodes.push(chatRow(item, depth, kids.length, item.name === current));
        if (kids.length && !state.folded.has(k)) for (const kid of kids) add(kid, depth + 1, seen);
    };
    for (const item of roots) add(item, 0);
    list.replaceChildren(...nodes);
}

function chatRow(item, depth, kids, current) {
    const info = H.titleInfo(item);
    const kind = kindOf(item.name) || (item.mainChat ? 'branch' : '');
    const k = `${ownerKey(item)}/${item.name}`;
    const avatar = item.thumb ? el('img', {class: 'rvo-avatar', src: item.thumb, alt: '', loading: 'lazy'}) : el('span', {class: 'rvo-avatar rvo-avatar-group'}, icon('fa-users'));
    const title = el('span', {class: `rvo-history-name${info?.by === 'agent' ? ' by-agent' : ''}`, text: H.displayTitle(item, item.who)});
    const holder = el('span', {class: 'rvo-crow-title'}, title);
    const rename = () => H.startRename(item, item.who, holder, () => redraw());
    const badges = [
        current ? el('span', {class: 'rvo-cbadge on', text: 'Open now'}) : null,
        isPinned(item) ? el('span', {class: 'rvo-cbadge', title: 'Pinned'}, icon('fa-thumbtack')) : null,
        kind ? el('span', {class: 'rvo-cbadge', title: item.mainChat ? `From “${item.mainChat}”` : ''}, icon(kind === 'checkpoint' ? 'fa-flag' : 'fa-code-branch'), el('span', {text: kind === 'checkpoint' ? 'Checkpoint' : 'Branch'})) : null,
        isArchived(item) ? el('span', {class: 'rvo-cbadge', text: 'Archived'}) : null,
    ].filter(Boolean);
    const fold = kids ? el('button', {class: 'rvo-icon-button rvo-cfold', type: 'button', title: state.folded.has(k) ? `Show ${kids} branch${kids === 1 ? '' : 'es'}` : 'Hide branches',
        onclick: event => { event.stopPropagation(); state.folded.has(k) ? state.folded.delete(k) : state.folded.add(k); redraw(); }},
        icon(state.folded.has(k) ? 'fa-chevron-right' : 'fa-chevron-down'), el('small', {text: String(kids)})) : null;
    return el('div', {class: `rvo-crow${current ? ' current' : ''}`, style: depth ? `--depth: ${Math.min(depth, 6)}` : undefined, 'data-depth': depth || undefined},
        el('button', {class: 'rvo-crow-main', type: 'button', title: item.name, onclick: () => openChat(item),
            ondblclick: event => { event.preventDefault(); rename(); }, onkeydown: event => { if (event.key === 'F2') { event.preventDefault(); rename(); } }},
            depth ? el('span', {class: 'rvo-ctree', 'aria-hidden': 'true'}, icon('fa-turn-up fa-rotate-90')) : avatar,
            el('span', {class: 'rvo-crow-text'},
                el('span', {class: 'rvo-crow-top'}, holder, ...badges),
                el('span', {class: 'rvo-crow-sub'}, el('strong', {text: item.who}), el('span', {text: ` · ${words(item.count)} · ${when(item.when)}`})),
                el('span', {class: 'rvo-chat-preview', text: item.preview || 'No messages yet'}))),
        fold,
        el('button', {class: 'rvo-icon-button', type: 'button', title: isPinned(item) ? 'Unpin' : 'Pin', onclick: () => setMark('pin', item, !isPinned(item))}, icon('fa-thumbtack')),
        H.moreMenu(null, chatMenuItems(item, {startRename: rename})));
}

// ------------------------------------------------------------- this chat --
function drawThisChat(wrap, items) {
    const c = ctx();
    const chatId = c.getCurrentChatId?.();
    if (!chatId) { wrap.append(el('p', {class: 'rvo-empty', text: 'No chat is open. Open one first, then its checkpoints and branches show here.'})); return; }
    const owner = c.groupId ? `g:${c.groupId}` : `c:${c.characters[c.characterId]?.avatar}`;
    const here = items.find(item => ownerKey(item) === owner && item.name === chatId) || {name: chatId, who: c.groupId ? c.groups.find(g => g.id === c.groupId)?.name : c.name2, avatar: c.characters[c.characterId]?.avatar || '', group: c.groupId || ''};
    const parent = c.chatMetadata?.main_chat || '';
    const marksList = [];
    (c.chat || []).forEach((message, id) => {
        if (message?.extra?.bookmark_link) marksList.push({id, kind: 'checkpoint', name: message.extra.bookmark_link, message});
        for (const name of Array.isArray(message?.extra?.branches) ? message.extra.branches : []) marksList.push({id, kind: 'branch', name, message});
    });
    const fromHere = items.filter(item => ownerKey(item) === owner && item.mainChat === chatId);
    const known = new Set(marksList.map(m => m.name));
    for (const item of fromHere) if (!known.has(item.name)) marksList.push({id: -1, kind: kindOf(item.name) || 'branch', name: item.name});
    const exists = name => items.some(item => ownerKey(item) === owner && item.name === name);
    const last = (c.chat?.length || 0) - 1;
    wrap.append(el('section', {class: 'rvo-dcard rvo-thischat'},
        el('h4', {}, icon('fa-comments'), el('span', {text: H.displayTitle(here, here.who)})),
        el('p', {class: 'rvo-muted', text: `${words(c.chat?.length || 0)}${parent ? ` · from “${parent}”` : ''}`}),
        el('div', {class: 'rvo-sbtns'},
            parent ? H.rvoButton('Back to the parent chat', 'fa-arrow-turn-up', () => H.menuAction('option_back_to_main'), `Open “${parent}”`, 'primary') : null,
            last >= 0 ? H.rvoButton('Save a checkpoint here', 'fa-flag', async () => {
                const b = await bookmarksModule();
                const name = await b.createNewBookmark(last);
                if (name) { toastr.success(`Checkpoint “${name}” saved`); invalidate(); redraw(); }
            }, 'A copy of the chat up to the last message, linked from it') : null,
            last >= 0 ? H.rvoButton('Branch from the last message', 'fa-code-branch', async () => {
                const b = await bookmarksModule();
                const name = await b.branchChat(last);
                if (name) { invalidate(); H.refreshSidebar(); }
            }, 'Open a new chat that continues from here') : null,
            H.rvoButton('Chat menu', 'fa-bars', () => H.chatMenu(), 'New chat, continue, impersonate, author’s note…'))));
    if (!marksList.length) {
        wrap.append(el('p', {class: 'rvo-empty', text: 'This chat has no checkpoints or branches yet. Use the buttons above, or the flag / branch buttons in a message’s menu (⋯).'}));
        return;
    }
    marksList.sort((a, b) => a.id - b.id);
    wrap.append(el('div', {class: 'rvo-chatlist'}, ...marksList.map(mark => {
        const text = String(mark.message?.mes || '').replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/\s+/g, ' ').trim();
        const item = items.find(i => ownerKey(i) === owner && i.name === mark.name);
        return el('div', {class: 'rvo-crow'},
            el('div', {class: 'rvo-crow-main rvo-crow-static'},
                el('span', {class: 'rvo-ctree'}, icon(mark.kind === 'checkpoint' ? 'fa-flag' : 'fa-code-branch')),
                el('span', {class: 'rvo-crow-text'},
                    el('span', {class: 'rvo-crow-top'}, el('span', {class: 'rvo-crow-title', text: mark.name}),
                        el('span', {class: 'rvo-cbadge', text: mark.kind === 'checkpoint' ? 'Checkpoint' : 'Branch'}),
                        exists(mark.name) ? null : el('span', {class: 'rvo-cbadge warn', text: 'Deleted'})),
                    el('span', {class: 'rvo-crow-sub', text: `${mark.id >= 0 ? `At message #${mark.id}` : 'From this chat'}${item ? ` · ${words(item.count)}` : ''}`}),
                    text ? el('span', {class: 'rvo-chat-preview', text: `${mark.message?.name || ''}: ${text.slice(0, 160)}`}) : null)),
            mark.id >= 0 ? el('button', {class: 'rvo-icon-button', type: 'button', title: 'Show this message in the chat', onclick: () => jumpTo(mark.id)}, icon('fa-location-crosshairs')) : null,
            exists(mark.name) ? el('button', {class: 'rvo-cbtn', type: 'button', onclick: () => openChat(item || {...here, name: mark.name})}, icon('fa-arrow-right'), el('span', {text: 'Open'})) : null);
    })));
}

async function jumpTo(id) {
    H.hidePage();
    const c = ctx();
    try {
        await c.executeSlashCommandsWithOptions(`/chat-jump ${id}`, {handleExecutionErrors: false, handleParserErrors: false});
    } catch {
        document.querySelector(`#chat .mes[mesid="${id}"]`)?.scrollIntoView({behavior: 'smooth', block: 'center'});
    }
    const node = document.querySelector(`#chat .mes[mesid="${id}"]`);
    if (node) { node.classList.add('rvo-flash'); setTimeout(() => node.classList.remove('rvo-flash'), 1800); }
}

// ---------------------------------------------------------------- backups --
async function drawBackups(wrap) {
    const box = el('div', {class: 'rvo-chatlist'}, el('p', {class: 'rvo-empty', text: 'Reading the backups…'}));
    wrap.append(el('p', {class: 'rvo-lnote', text: 'SillyTavern saves a copy of a chat every time it changes (at most every few seconds) and keeps the latest ones per character. Restoring makes a NEW chat from the backup; nothing is overwritten.'}), box);
    try {
        state.backups ||= (await get('/chats/backups')).backups;
    } catch (error) {
        box.replaceChildren(el('p', {class: 'rvo-empty', text: `The backups couldn’t be read: ${error.message}`}));
        return;
    }
    const c = ctx();
    const ownerName = b => b.owner?.avatar ? c.characters.find(ch => ch.avatar === b.owner.avatar)?.name || b.owner.avatar.replace(/\.png$/, '')
        : b.owner?.groupChat !== undefined ? c.groups.find(g => g.chats?.includes(b.owner.groupChat))?.name || 'Group chat' : b.key;
    const q = state.q.trim().toLowerCase();
    const list = state.backups.filter(b => !q || `${ownerName(b)} ${b.chat} ${b.mes}`.toLowerCase().includes(q));
    const search = el('input', {type: 'search', class: 'rvo-cinput', placeholder: 'Search backups', value: state.q,
        oninput: event => { state.q = event.target.value; clearTimeout(drawBackups.t); drawBackups.t = setTimeout(redraw, 250); }});
    wrap.insertBefore(el('div', {class: 'rvo-chattools'}, el('label', {class: 'rvo-search rvo-chatsearch'}, icon('fa-magnifying-glass'), search),
        H.rvoButton('Reload', 'fa-rotate', () => { state.backups = null; redraw(); })), box);
    if (!list.length) { box.replaceChildren(el('p', {class: 'rvo-empty', text: q ? 'No backups match.' : 'No backups yet.'})); return; }
    let lastDay = '';
    const nodes = [];
    for (const b of list) {
        const day = new Date(b.taken).toLocaleDateString(undefined, {weekday: 'long', month: 'long', day: 'numeric'});
        if (day !== lastDay) { nodes.push(el('div', {class: 'rvo-group-label', text: day})); lastDay = day; }
        const ch = b.owner?.avatar ? c.characters.find(x => x.avatar === b.owner.avatar) : null;
        const chatItem = b.chat ? {name: b.chat, avatar: b.owner?.avatar || '', group: b.owner?.groupChat !== undefined ? c.groups.find(g => g.chats?.includes(b.chat))?.id || '' : '', who: ownerName(b)} : null;
        const open = state.openBackup === b.name;
        nodes.push(el('div', {class: `rvo-crow rvo-brow${open ? ' open' : ''}`},
            el('button', {class: 'rvo-crow-main', type: 'button', onclick: () => { state.openBackup = open ? '' : b.name; redraw(); }},
                ch ? el('img', {class: 'rvo-avatar', src: c.getThumbnailUrl('avatar', ch.avatar), alt: '', loading: 'lazy'}) : el('span', {class: 'rvo-avatar rvo-avatar-group'}, icon('fa-box-archive')),
                el('span', {class: 'rvo-crow-text'},
                    el('span', {class: 'rvo-crow-top'}, el('span', {class: 'rvo-crow-title', text: ownerName(b)}),
                        el('span', {class: 'rvo-cbadge', text: new Date(b.taken).toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'})}),
                        b.owner ? null : el('span', {class: 'rvo-cbadge warn', text: 'Character gone'})),
                    el('span', {class: 'rvo-crow-sub', text: `${chatItem ? `Chat: ${H.displayTitle(chatItem, chatItem.who)}` : 'A chat that no longer exists (or an older copy)'} · ${words(b.items)}`}),
                    el('span', {class: 'rvo-chat-preview', text: b.mes ? `${b.lastName}: ${b.mes}` : 'Empty'}))),
            b.owner ? el('button', {class: 'rvo-cbtn', type: 'button', title: 'Make a new chat from this backup', onclick: () => restoreBackup(b, chatItem)}, icon('fa-clock-rotate-left'), el('span', {text: 'Restore'})) : null,
            H.moreMenu(null, [
                [open ? 'Hide messages' : 'Look inside', 'fa-eye', () => { state.openBackup = open ? '' : b.name; redraw(); }],
                ['Download', 'fa-download', () => downloadBackup(b)],
                ['Delete backup…', 'fa-trash-can', () => deleteBackup(b), true]]),
            open ? backupViewer(b) : null));
    }
    box.replaceChildren(...nodes);
}

async function backupFile(b) {
    const response = await fetch('/api/backups/chat/download', {method: 'POST', headers: ctx().getRequestHeaders(), body: JSON.stringify({name: b.name})});
    if (!response.ok) throw new Error(`The backup couldn’t be read (${response.status})`);
    return response.text();
}

function backupViewer(b) {
    const box = el('div', {class: 'rvo-bview'}, el('small', {class: 'rvo-muted', text: 'Loading…'}));
    const show = text => {
        const rows = text.split('\n').map(line => { try { return JSON.parse(line); } catch { return null; } }).filter(row => row && typeof row.mes === 'string' && !row.chat_metadata);
        box.replaceChildren(...rows.slice(-60).map(row => el('div', {class: `rvo-bmsg${row.is_user ? ' user' : ''}`},
            el('strong', {text: row.name || (row.is_user ? 'You' : '')}), el('span', {text: row.mes.slice(0, 1500)}))),
        rows.length > 60 ? el('small', {class: 'rvo-muted', text: `Last 60 of ${rows.length} messages.`}) : '');
    };
    const cached = state.backupText.get(b.name);
    if (cached) show(cached); else backupFile(b).then(text => { state.backupText.set(b.name, text); show(text); }).catch(error => box.replaceChildren(el('small', {class: 'rvo-muted', text: error.message})));
    return box;
}

async function restoreBackup(b, chatItem) {
    const c = ctx();
    const stamp = new Date(b.taken).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}).replace(/[/:]/g, '.');
    try {
        const body = {name: b.name, label: `backup ${stamp}`, base: chatItem?.name || ''};
        if (b.owner?.avatar) body.avatar = b.owner.avatar; else body.groupChat = b.owner.groupChat;
        const {file, group} = await post('/chats/backups/restore', body);
        let groupId = '';
        if (group) {
            groupId = chatItem?.group || c.groups.find(g => g.chats?.includes(b.owner.groupChat))?.id || '';
            if (!groupId) throw new Error('The group this backup belongs to is gone; download it instead.');
            await addToGroup(groupId, file);
        }
        invalidate();
        H.refreshSidebar();
        const item = {name: file, avatar: b.owner?.avatar || '', group: groupId, who: ''};
        const ok = await c.callGenericPopup(`Restored as a new chat: “${file}”. Open it now?`, c.POPUP_TYPE.CONFIRM, '', {okButton: 'Open it', cancelButton: 'Not now'});
        if (ok) openChat(item); else redraw();
    } catch (error) {
        toastr.error(error.message, 'Couldn’t restore the backup');
    }
}

async function downloadBackup(b) {
    try {
        const text = state.backupText.get(b.name) || await backupFile(b);
        const a = el('a', {href: URL.createObjectURL(new Blob([text], {type: 'application/json-lines'})), download: b.name});
        document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    } catch (error) { toastr.error(error.message); }
}

async function deleteBackup(b) {
    const c = ctx();
    if (!await c.callGenericPopup(`Delete this backup of ${b.chat || 'the chat'} (${new Date(b.taken).toLocaleString()})?`, c.POPUP_TYPE.CONFIRM, '', {okButton: 'Delete', cancelButton: 'Keep'})) return;
    const response = await fetch('/api/backups/chat/delete', {method: 'POST', headers: c.getRequestHeaders(), body: JSON.stringify({name: b.name})});
    if (!response.ok) toastr.error('The backup couldn’t be deleted.');
    state.backups = null;
    redraw();
}

// ------------------------------------------------------------------ stats --
/** Side card for the character page: statistics from the character's chat files. */
export function chatStatsCard({avatar = '', group = null} = {}) {
    const body = el('div', {}, el('small', {class: 'rvo-muted', text: 'Counting…'}));
    const query = group ? `?group=1&chats=${encodeURIComponent((group.chats || []).join(','))}` : `?avatar=${encodeURIComponent(avatar)}`;
    get(`/chats/stats${query}`).then(s => {
        if (!s.chats) { body.replaceChildren(el('small', {class: 'rvo-muted', text: 'No chats yet.'})); return; }
        body.replaceChildren(H.statTiles([
            ['Your messages', s.user, 'fa-user'], ['Their replies', s.char, 'fa-robot'],
            ['Your words', s.userWords, 'fa-keyboard'], ['Their words', s.charWords, 'fa-feather'],
            ['Swipes', s.swipes, 'fa-left-right'], ['Writing time', s.genMs ? duration(s.genMs) : '—', 'fa-stopwatch'],
        ]), H.facts([
            ['Chats', `${s.chats}${s.branches || s.checkpoints ? ` (${[s.branches ? `${s.branches} branch${s.branches === 1 ? '' : 'es'}` : '', s.checkpoints ? `${s.checkpoints} checkpoint${s.checkpoints === 1 ? '' : 's'}` : ''].filter(Boolean).join(', ')})` : ''}`],
            ['First message', s.first ? new Date(s.first).toLocaleDateString(undefined, {year: 'numeric', month: 'short', day: 'numeric'}) : ''],
            ['Last message', s.last ? when(s.last) : ''],
            ['Longest chat', s.longest ? words(s.longest.items) : ''],
            ['Avg. reply', s.char ? `${Math.round(s.charWords / s.char)} words` : ''],
        ]));
    }).catch(error => body.replaceChildren(el('small', {class: 'rvo-muted', text: `Couldn’t count: ${error.message}`})));
    return H.sideCard('Chat stats', 'fa-chart-line', body);
}
