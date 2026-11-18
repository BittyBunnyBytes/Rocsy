// "Reply ready" notifications (part of the Rocsy Addons server plugin).
//
// A phone that locks its screen freezes the page, so the page itself can't say when the reply is done. With Web
// Push the phone's own push service wakes Rocsy's small service worker (GET /sw.js) instead, and it shows the
// notification. Pushes are sent WITHOUT a payload (nothing of the chat goes through the push service): the worker
// asks this server what to show (GET /push/latest, with the page's own login cookie).
//
// Keys: one VAPID key pair per SillyTavern user, made on first use, kept with the subscriptions in
// <user root>/rocsy-push.json. A subscription the push service says is gone (404/410) is dropped.

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { Jimp } from '../../src/jimp.js';

const FILE = 'rocsy-push.json';
const SUBJECT = 'mailto:rocsy-addons@example.com';   // VAPID contact claim; a placeholder, push services don't write to it
const latest = new Map();                             // user handle -> {title, body, tag, at}
const b64url = buffer => Buffer.from(buffer).toString('base64url');

function storePath(root) { return path.join(path.resolve(root), FILE); }
function load(root) {
    try { return JSON.parse(fs.readFileSync(storePath(root), 'utf8')); } catch { return {}; }
}
function store(root, data) {
    fs.writeFileSync(storePath(root), JSON.stringify(data, null, 1), { mode: 0o600 });
}

/** The user's VAPID keys (made on first use). */
function keys(root) {
    const data = load(root);
    if (data.vapid?.privateJwk && data.vapid?.publicKey) return data;
    const { publicKey, privateKey } = crypto.generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
    const jwk = publicKey.export({ format: 'jwk' });
    data.vapid = {
        privateJwk: privateKey.export({ format: 'jwk' }),
        publicKey: b64url(Buffer.concat([Buffer.from([4]), Buffer.from(jwk.x, 'base64url'), Buffer.from(jwk.y, 'base64url')])),
    };
    data.subs ||= [];
    store(root, data);
    return data;
}

function vapidHeader(data, endpoint) {
    const header = b64url(JSON.stringify({ typ: 'JWT', alg: 'ES256' }));
    const claims = b64url(JSON.stringify({ aud: new URL(endpoint).origin, exp: Math.floor(Date.now() / 1000) + 12 * 3600, sub: SUBJECT }));
    const key = crypto.createPrivateKey({ key: data.vapid.privateJwk, format: 'jwk' });
    const signature = crypto.sign('sha256', Buffer.from(`${header}.${claims}`), { key, dsaEncoding: 'ieee-p1363' });
    return `vapid t=${header}.${claims}.${b64url(signature)}, k=${data.vapid.publicKey}`;
}

/**
 * Tell this user's phones (and other subscribed browsers) a reply is ready.
 * @param {string} root the user's root directory
 * @param {string} user the user's handle
 * @param {{name?: string, text?: string, chatId?: string}} reply
 */
export async function notifyReply(root, user, reply) {
    let data;
    try { data = load(root); } catch { return; }
    if (!data.vapid || !data.subs?.length) return;
    const name = reply.name || 'Your character';
    const preview = String(reply.text || '').replace(/\s+/g, ' ').trim();
    latest.set(user, {
        title: `${name} replied`,
        body: preview ? preview.slice(0, 140) + (preview.length > 140 ? '…' : '') : 'The reply is ready.',
        bodyPlain: 'The reply is ready. Tap to read it.',
        tag: `rocsy-reply-${String(reply.chatId || '').slice(0, 80)}`,
        at: Date.now(),
    });
    let changed = false;
    for (const sub of [...data.subs]) {
        try {
            const response = await fetch(sub.endpoint, {
                method: 'POST',
                headers: { TTL: '600', Urgency: 'high', Authorization: vapidHeader(data, sub.endpoint), 'Content-Length': '0' },
                signal: AbortSignal.timeout(15000),
            });
            if (response.status === 404 || response.status === 410) { data.subs = data.subs.filter(s => s.endpoint !== sub.endpoint); changed = true; }
            else if (!response.ok) console.warn('[Rocsy Addons] push:', new URL(sub.endpoint).host, response.status, (await response.text().catch(() => '')).slice(0, 200));
        } catch (error) {
            console.warn('[Rocsy Addons] push:', error?.message || error);
        }
    }
    if (changed) { try { store(root, data); } catch { /* next time */ } }
}

const SW = `// Rocsy Addons: shows "reply ready" notifications (pushes carry no data; the text comes from the server).
const base = self.registration.scope;
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('push', event => event.waitUntil((async () => {
    const open = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    if (open.some(c => c.visibilityState === 'visible' && c.focused)) return;   // looking at it already
    let notice = {title: 'Reply ready', body: 'Tap to read it.', tag: 'rocsy-reply'};
    try {
        const sub = await self.registration.pushManager.getSubscription();
        const response = await fetch(base + 'api/plugins/rocsy/push/latest?endpoint=' + encodeURIComponent(sub?.endpoint || ''), {credentials: 'include', cache: 'no-store'});
        if (response.ok) notice = {...notice, ...(await response.json())};
    } catch { /* generic text */ }
    await self.registration.showNotification(notice.title, {body: notice.body, tag: notice.tag, renotify: true,
        icon: base + 'img/ai4.png', badge: base + 'favicon.ico', data: {url: base}});
})()));
self.addEventListener('notificationclick', event => {
    event.notification.close();
    event.waitUntil((async () => {
        const open = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
        const tab = open.find(c => c.url.startsWith(base));
        if (tab) return tab.focus();
        return self.clients.openWindow(event.notification.data?.url || base);
    })());
});
`;

// ------------------------------------------------------------ home-screen app --
// The Rocsy mark (sun + bird, same path as the gateway home page) drawn onto a dark tile, for "Add to Home screen".
const BIRD = 'M256 186 C269 186 276 197 274 210 C320 182 380 162 464 156 C418 200 358 234 292 254 L308 314 L256 294 L204 314 L220 254 C154 234 94 200 48 156 C132 162 192 182 238 210 C236 197 243 186 256 186 Z';
function birdPolygon() {
    const tokens = BIRD.match(/[MCLZ]|-?\d+(?:\.\d+)?/g);
    const points = [];
    let i = 0, cur = [0, 0], cmd = '';
    const num = () => Number(tokens[i++]);
    while (i < tokens.length) {
        if (/[MCLZ]/.test(tokens[i])) cmd = tokens[i++];
        if (cmd === 'M' || cmd === 'L') { cur = [num(), num()]; points.push(cur); }
        else if (cmd === 'C') {
            const p1 = [num(), num()], p2 = [num(), num()], p3 = [num(), num()], p0 = cur;
            for (let t = 1; t <= 16; t++) {
                const u = t / 16, a = (1 - u) ** 3, b = 3 * (1 - u) ** 2 * u, c = 3 * (1 - u) * u * u, d = u ** 3;
                points.push([a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]]);
            }
            cur = p3;
        } else if (cmd === 'Z') { i++; }
    }
    return points;
}
const icons = new Map();
async function appIcon(size) {
    if (icons.has(size)) return icons.get(size);
    const image = new Jimp({ width: size, height: size, color: 0x141c22ff });
    const poly = birdPolygon();
    const scale = (size * 0.74) / 432, cx = 256, cy = 262;
    const inside = (x, y) => {
        let hit = false;
        for (let a = 0, b = poly.length - 1; a < poly.length; b = a++) {
            const [xa, ya] = poly[a], [xb, yb] = poly[b];
            if ((ya > y) !== (yb > y) && x < ((xb - xa) * (y - ya)) / (yb - ya) + xa) hit = !hit;
        }
        return hit;
    };
    const data = image.bitmap.data;
    for (let py = 0; py < size; py++) {
        for (let px = 0; px < size; px++) {
            // 2x2 supersampling for smooth edges
            let sun = 0, bird = 0;
            for (const [ox, oy] of [[0.25, 0.25], [0.75, 0.25], [0.25, 0.75], [0.75, 0.75]]) {
                const x = (px + ox - size / 2) / scale + cx, y = (py + oy - size / 2) / scale + cy;
                if (inside(x, y)) bird++;
                else if ((x - 256) ** 2 + (y - 268) ** 2 <= 124 * 124) sun++;
            }
            const k = (py * size + px) * 4;
            const mix = (bg, s, b) => Math.round((bg * (4 - sun - bird) + s * sun + b * bird) / 4);
            data[k] = mix(0x14, 0xe1, 0xf2); data[k + 1] = mix(0x1c, 0xa7, 0xf7); data[k + 2] = mix(0x22, 0x44, 0xfb); data[k + 3] = 255;
        }
    }
    const png = await image.getBuffer('image/png');
    icons.set(size, png);
    return png;
}

export function initPush(router) {
    router.get('/manifest.webmanifest', (_request, response) => {
        // Relative to this file (…/api/plugins/rocsy/), so it works with and without the gateway's prefix.
        response.set({ 'Content-Type': 'application/manifest+json', 'Cache-Control': 'no-cache' });
        response.send(JSON.stringify({
            name: 'RocsyTavern', short_name: 'RocsyTavern', description: 'Character chats on Rocsy',
            start_url: '../../../', scope: '../../../', id: '../../../', display: 'standalone', orientation: 'any',
            background_color: '#141c22', theme_color: '#141c22',
            icons: [192, 512].flatMap(size => [
                { src: `app-icon.png?size=${size}`, sizes: `${size}x${size}`, type: 'image/png', purpose: 'any' },
                { src: `app-icon.png?size=${size}`, sizes: `${size}x${size}`, type: 'image/png', purpose: 'maskable' },
            ]),
        }));
    });
    router.get('/app-icon.png', async (request, response) => {
        const size = [180, 192, 512].includes(Number(request.query.size)) ? Number(request.query.size) : 192;
        try {
            response.set({ 'Content-Type': 'image/png', 'Cache-Control': 'public, max-age=86400' });
            response.send(await appIcon(size));
        } catch (error) {
            response.status(500).send({ error: String(error?.message || error) });
        }
    });

    const who = request => ({ root: request.user?.directories?.root, user: request.user?.profile?.handle || '' });

    router.get('/sw.js', (_request, response) => {
        response.set({ 'Content-Type': 'text/javascript; charset=utf-8', 'Service-Worker-Allowed': '/', 'Cache-Control': 'no-cache' });
        response.send(SW);
    });

    router.get('/push/key', (request, response) => {
        const { root } = who(request);
        if (!root) return response.status(500).send({ error: 'No user folder' });
        response.send({ key: keys(root).vapid.publicKey });
    });

    router.get('/push/state', (request, response) => {
        const { root } = who(request);
        const endpoint = String(request.query.endpoint || '');
        const data = root ? load(root) : {};
        const sub = (data.subs || []).find(s => s.endpoint === endpoint);
        response.send({ subscribed: !!sub, preview: !!sub?.preview, devices: (data.subs || []).length });
    });

    router.post('/push/subscribe', (request, response) => {
        const { root } = who(request);
        const { subscription, preview = false, device = '' } = request.body || {};
        if (!root || !subscription?.endpoint || !/^https:\/\//.test(subscription.endpoint)) return response.status(400).send({ error: 'Not a push subscription' });
        const data = keys(root);
        data.subs = (data.subs || []).filter(s => s.endpoint !== subscription.endpoint);
        data.subs.push({ endpoint: String(subscription.endpoint).slice(0, 1000), preview: !!preview, device: String(device).slice(0, 120), added: Date.now() });
        while (data.subs.length > 12) data.subs.shift();
        store(root, data);
        response.send({ ok: true, devices: data.subs.length });
    });

    router.post('/push/unsubscribe', (request, response) => {
        const { root } = who(request);
        const endpoint = String(request.body?.endpoint || '');
        if (!root) return response.status(400).send({ error: 'No user folder' });
        const data = load(root);
        data.subs = (data.subs || []).filter(s => s.endpoint !== endpoint);
        store(root, data);
        response.send({ ok: true });
    });

    // What the service worker shows. The preview text is only sent when that device asked for it.
    router.get('/push/latest', (request, response) => {
        const { root, user } = who(request);
        const notice = latest.get(user);
        if (!notice || Date.now() - notice.at > 15 * 60 * 1000) return response.send({ title: 'Reply ready', body: 'Tap to read it.', tag: 'rocsy-reply' });
        const endpoint = String(request.query.endpoint || '');
        const preview = !!(root ? load(root).subs || [] : []).find(s => s.endpoint === endpoint)?.preview;
        response.send({ title: notice.title, body: preview ? notice.body : notice.bodyPlain, tag: notice.tag });
    });

    router.post('/push/test', async (request, response) => {
        const { root, user } = who(request);
        if (!root) return response.status(400).send({ error: 'No user folder' });
        await notifyReply(root, user, { name: 'Rocsy', text: 'Test notification: replies will show up like this.', chatId: 'test' });
        response.send({ ok: true });
    });
}
