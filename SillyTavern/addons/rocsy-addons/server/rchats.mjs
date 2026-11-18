// Chats page helpers (part of the Rocsy Addons server plugin): every chat of every character and group in one
// list (with the branch / checkpoint parent each one came from), chat statistics worked out from the chat files,
// SillyTavern's automatic chat backups matched to the chat they came from, restoring a backup as a new chat, and
// duplicating a chat. Deleting, exporting and opening chats use SillyTavern's own endpoints from the page.

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import sanitize from 'sanitize-filename';
import { getBackupKey, CHAT_BACKUPS_PREFIX } from '../../src/endpoints/chats.js';

const cache = new Map();   // file path -> {key: mtime+size, info}

/** One pass over a chat file: header metadata, message counts, words, swipes, generation time, dates, last message. */
async function scan(file) {
    let stat;
    try { stat = await fs.promises.stat(file); } catch { return null; }
    const key = `${stat.mtimeMs}:${stat.size}`;
    const hit = cache.get(file);
    if (hit?.key === key) return hit.info;
    const info = {
        file: path.basename(file), size: stat.size, modified: stat.mtimeMs, meta: {}, items: 0,
        user: 0, char: 0, userWords: 0, charWords: 0, swipes: 0, genMs: 0, first: 0, last: 0, mes: '', lastName: '',
        checkpoints: 0, branches: 0,
    };
    const words = text => (String(text || '').match(/\S+/g) || []).length;
    await new Promise(resolve => {
        const stream = fs.createReadStream(file);
        stream.on('error', resolve);
        const lines = readline.createInterface({ input: stream, crlfDelay: Infinity });
        lines.on('error', resolve);
        let first = true;
        lines.on('line', line => {
            if (!line.trim()) return;
            let row;
            try { row = JSON.parse(line); } catch { return; }
            if (first) {
                first = false;
                if (row && typeof row.chat_metadata === 'object') { info.meta = row.chat_metadata || {}; return; }
            }
            if (!row || typeof row !== 'object' || row.is_system) return;
            info.items += 1;
            if (row.is_user) { info.user += 1; info.userWords += words(row.mes); }
            else { info.char += 1; info.charWords += words(row.mes); }
            if (Array.isArray(row.swipes) && row.swipes.length > 1) info.swipes += row.swipes.length - 1;
            const started = Date.parse(row.gen_started), finished = Date.parse(row.gen_finished);
            if (started && finished && finished > started && finished - started < 30 * 60 * 1000) info.genMs += finished - started;
            const when = Date.parse(row.send_date) || Number(row.send_date) || 0;
            if (when) { info.first = info.first ? Math.min(info.first, when) : when; info.last = Math.max(info.last, when); }
            if (row.extra?.bookmark_link) info.checkpoints += 1;
            if (Array.isArray(row.extra?.branches)) info.branches += row.extra.branches.length;
            info.mes = String(row.mes || '').replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/\s+/g, ' ').trim().slice(0, 200);
            info.lastName = String(row.name || '');
        });
        lines.on('close', resolve);
    });
    info.mainChat = String(info.meta.main_chat || '');
    info.integrity = String(info.meta.integrity || '');
    delete info.meta;
    cache.set(file, { key, info });
    return info;
}

async function jsonlFiles(dir) {
    try {
        return (await fs.promises.readdir(dir, { withFileTypes: true })).filter(d => d.isFile() && d.name.endsWith('.jsonl')).map(d => path.join(dir, d.name));
    } catch { return []; }
}

async function characterDirs(chatsDir) {
    try { return (await fs.promises.readdir(chatsDir, { withFileTypes: true })).filter(d => d.isDirectory()).map(d => d.name); } catch { return []; }
}

const slim = info => info && ({
    file: info.file, size: info.size, modified: info.modified, items: info.items, user: info.user, char: info.char,
    first: info.first, last: info.last || info.modified, mes: info.mes, lastName: info.lastName, mainChat: info.mainChat, checkpoints: info.checkpoints, branches: info.branches,
});

function sumStats(list) {
    const total = { chats: 0, items: 0, user: 0, char: 0, userWords: 0, charWords: 0, swipes: 0, genMs: 0, first: 0, last: 0, checkpoints: 0, branches: 0 };
    for (const info of list) {
        if (!info) continue;
        total.chats += 1;
        for (const k of ['items', 'user', 'char', 'userWords', 'charWords', 'swipes', 'genMs', 'checkpoints', 'branches']) total[k] += info[k];
        if (info.first) total.first = total.first ? Math.min(total.first, info.first) : info.first;
        total.last = Math.max(total.last, info.last || 0);
    }
    return total;
}

/** Name for a new chat file that doesn't exist yet: "<base> (<suffix>)", "<base> (<suffix> 2)", … */
function freeName(dir, base, suffix) {
    for (let i = 1; i < 500; i++) {
        const name = sanitize(`${base} (${suffix}${i > 1 ? ` ${i}` : ''})`).slice(0, 200);
        if (!fs.existsSync(path.join(dir, `${name}.jsonl`))) return name;
    }
    return sanitize(`${base} ${Date.now()}`);
}

/** Copy a chat file with a fresh integrity id (so SillyTavern treats it as its own chat). */
async function copyChat(from, to, { mainChat } = {}) {
    const text = await fs.promises.readFile(from, 'utf8');
    const lines = text.split('\n');
    try {
        const head = JSON.parse(lines[0]);
        if (head && typeof head.chat_metadata === 'object') {
            head.chat_metadata = { ...head.chat_metadata, integrity: crypto.randomUUID() };
            if (mainChat !== undefined) { if (mainChat) head.chat_metadata.main_chat = mainChat; else delete head.chat_metadata.main_chat; }
            lines[0] = JSON.stringify(head);
        }
    } catch { /* no header: copied as it is */ }
    await fs.promises.writeFile(to, lines.join('\n'), { flag: 'wx' });
}

export function initChats(router) {
    const dirs = request => request.user?.directories || {};

    // Every chat: {characters: {<avatar without .png>: [chat]}, groups: {<chat id>: chat}}.
    router.get('/chats/all', async (request, response) => {
        const { chats, groupChats } = dirs(request);
        if (!chats) return response.status(500).send({ error: 'No chats folder' });
        const characters = {};
        for (const name of await characterDirs(chats)) {
            const list = (await Promise.all((await jsonlFiles(path.join(chats, name))).map(scan))).filter(Boolean).map(slim);
            if (list.length) characters[name] = list;
        }
        const groups = {};
        for (const file of await jsonlFiles(groupChats)) { const info = await scan(file); if (info) groups[info.file.replace(/\.jsonl$/, '')] = slim(info); }
        response.send({ characters, groups });
    });

    // Statistics: one character (?avatar=Name.png), one group's chats (?group=1&chats=a,b), or everything (no query).
    router.get('/chats/stats', async (request, response) => {
        const { chats, groupChats } = dirs(request);
        if (!chats) return response.status(500).send({ error: 'No chats folder' });
        let files = [];
        if (request.query.avatar) {
            const dir = path.join(chats, sanitize(String(request.query.avatar).replace(/\.png$/, '')));
            files = await jsonlFiles(dir);
        } else if (request.query.group) {
            files = String(request.query.chats || '').split(',').filter(Boolean).map(id => path.join(groupChats, sanitize(`${id}.jsonl`)));
        } else {
            for (const name of await characterDirs(chats)) files.push(...await jsonlFiles(path.join(chats, name)));
            files.push(...await jsonlFiles(groupChats));
        }
        const infos = await Promise.all(files.map(scan));
        const total = sumStats(infos);
        const longest = infos.filter(Boolean).sort((a, b) => b.items - a.items)[0];
        response.send({ ...total, longest: longest ? { file: longest.file, items: longest.items } : null });
    });

    // SillyTavern's automatic backups, each matched to its owner and (by integrity id) to the chat it came from.
    router.get('/chats/backups', async (request, response) => {
        const { chats, groupChats, backups } = dirs(request);
        if (!backups) return response.status(500).send({ error: 'No backups folder' });
        const owners = new Map();   // backup key -> {avatar} | {group chat id}
        for (const name of await characterDirs(chats)) owners.set(getBackupKey(name), { avatar: `${name}.png`, dir: name });
        for (const file of await jsonlFiles(groupChats)) { const id = path.basename(file, '.jsonl'); owners.set(getBackupKey(id), { groupChat: id }); }
        const byIntegrity = new Map();
        for (const [, owner] of owners) {
            if (owner.dir) for (const file of await jsonlFiles(path.join(chats, owner.dir))) { const info = await scan(file); if (info?.integrity) byIntegrity.set(info.integrity, info.file.replace(/\.jsonl$/, '')); }
            if (owner.groupChat) { const info = await scan(path.join(groupChats, `${owner.groupChat}.jsonl`)); if (info?.integrity) byIntegrity.set(info.integrity, owner.groupChat); }
        }
        const list = [];
        for (const file of await jsonlFiles(backups)) {
            const base = path.basename(file);
            if (!base.startsWith(CHAT_BACKUPS_PREFIX)) continue;
            const m = base.slice(CHAT_BACKUPS_PREFIX.length).match(/^(.*)_(\d{8})-(\d{6})\.jsonl$/);
            if (!m) continue;
            const info = await scan(file);
            if (!info) continue;
            const [, key, d, t] = m;
            const owner = owners.get(key) || null;
            list.push({
                name: base, key, owner, chat: info.integrity ? byIntegrity.get(info.integrity) || '' : '',
                taken: Date.UTC(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6), +t.slice(0, 2), +t.slice(2, 4), +t.slice(4)) || info.modified,
                items: info.items, size: info.size, mes: info.mes, lastName: info.lastName, last: info.last,
            });
        }
        list.sort((a, b) => b.taken - a.taken);
        response.send({ backups: list });
    });

    // Restore a backup as a NEW chat next to the others (nothing is overwritten). Group: the page adds the new chat id
    // to the group's chat list.
    router.post('/chats/backups/restore', async (request, response) => {
        const { chats, groupChats, backups } = dirs(request);
        const name = sanitize(String(request.body?.name || ''));
        const from = path.join(backups, name);
        if (!name.startsWith(CHAT_BACKUPS_PREFIX) || !fs.existsSync(from)) return response.status(404).send({ error: 'No such backup' });
        const { avatar, groupChat, base } = request.body || {};
        const when = String(request.body?.label || 'restored').slice(0, 40);
        try {
            if (avatar) {
                const dir = path.join(chats, sanitize(String(avatar).replace(/\.png$/, '')));
                if (!fs.existsSync(dir)) return response.status(404).send({ error: 'That character has no chats folder' });
                const file = freeName(dir, String(base || String(avatar).replace(/\.png$/, '')), when);
                await copyChat(from, path.join(dir, `${file}.jsonl`));
                return response.send({ ok: true, file });
            }
            if (groupChat !== undefined) {
                const id = freeName(groupChats, String(base || groupChat || 'Group chat'), when);
                await copyChat(from, path.join(groupChats, `${id}.jsonl`));
                return response.send({ ok: true, file: id, group: true });
            }
            response.status(400).send({ error: 'Say which character or group the backup goes to' });
        } catch (error) {
            response.status(500).send({ error: String(error?.message || error) });
        }
    });

    // Duplicate a chat: {avatar, file} or {group: true, file}. The copy is a chat of its own (no branch link).
    router.post('/chats/duplicate', async (request, response) => {
        const { chats, groupChats } = dirs(request);
        const { avatar, group, file } = request.body || {};
        const stem = sanitize(String(file || '').replace(/\.jsonl$/, ''));
        if (!stem) return response.status(400).send({ error: 'Which chat?' });
        const dir = group ? groupChats : path.join(chats, sanitize(String(avatar || '').replace(/\.png$/, '')));
        const from = path.join(dir, `${stem}.jsonl`);
        if (!fs.existsSync(from)) return response.status(404).send({ error: 'No such chat' });
        try {
            const name = freeName(dir, stem, 'copy');
            await copyChat(from, path.join(dir, `${name}.jsonl`), { mainChat: '' });
            response.send({ ok: true, file: name });
        } catch (error) {
            response.status(500).send({ error: String(error?.message || error) });
        }
    });
}
