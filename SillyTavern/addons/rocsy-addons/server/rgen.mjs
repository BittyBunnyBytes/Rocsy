// Replies that survive a dropped connection (part of the Rocsy Addons server plugin).
//
// A phone that locks its screen or switches apps mid-reply freezes the tab and drops its connection; SillyTavern then
// throws the half-written reply away (it saves a reply only when it's complete). With this, the page's streaming
// generate call goes through here instead: the server makes SillyTavern's own /api/backends/chat-completions/generate
// call itself (same cookie + CSRF token, so same user, settings and key), keeps the whole stream in memory, and the
// page reads it with GET /gen/:id/stream?from=<byte offset>. When the connection drops the reply keeps being written;
// the page resumes from the byte it had when the tab wakes up, so SillyTavern never notices. If the page was reloaded
// instead, /gen/recover hands the finished reply back so the page can add it to the chat.

import crypto from 'node:crypto';
import { notifyReply } from './rpush.mjs';

const jobs = new Map();                 // id -> job
const KEEP_MS = 60 * 60 * 1000;         // finished jobs are kept an hour (recovery after a reload)
const MAX_BYTES = 32 * 1024 * 1024;

function sweep() {
    const now = Date.now();
    for (const [id, job] of jobs) if (job.ended && now - job.ended > KEEP_MS) jobs.delete(id);
    while (jobs.size > 40) jobs.delete(jobs.keys().next().value);
}

const total = job => job.size;
function bytesFrom(job, from) {
    if (from >= job.size) return Buffer.alloc(0);
    let skip = from;
    const parts = [];
    for (const chunk of job.chunks) {
        if (skip >= chunk.length) { skip -= chunk.length; continue; }
        parts.push(skip ? chunk.subarray(skip) : chunk);
        skip = 0;
    }
    return Buffer.concat(parts);
}

/** The reply text (and reasoning) from the OpenAI-style SSE stream. */
function replyText(job) {
    let text = '', reasoning = '';
    for (const line of Buffer.concat(job.chunks).toString('utf8').split('\n')) {
        if (!line.startsWith('data:')) continue;
        const data = line.slice(5).trim();
        if (!data || data === '[DONE]') continue;
        try {
            const json = JSON.parse(data);
            const delta = json?.choices?.[0]?.delta || {};
            text += delta.content || json?.choices?.[0]?.text || '';
            reasoning += delta.reasoning_content || delta.reasoning || '';
        } catch { /* partial or foreign line */ }
    }
    return { text, reasoning };
}

async function run(job, url, headers, body) {
    try {
        const response = await fetch(url, { method: 'POST', headers, body, signal: job.abort.signal });
        job.status = response.status;
        job.contentType = response.headers.get('content-type') || 'text/event-stream';
        job.notify();
        for await (const chunk of response.body) {
            const buffer = Buffer.from(chunk);
            if (job.size + buffer.length > MAX_BYTES) throw new Error('The reply is too large');
            job.chunks.push(buffer);
            job.size += buffer.length;
            job.notify();
        }
    } catch (error) {
        if (!job.abort.signal.aborted) {
            job.error = String(error?.message || error).slice(0, 300);
            console.warn('[Rocsy Addons] reply stream:', job.error);
            if (!job.status) job.status = 502;
        }
    } finally {
        job.ended = Date.now();
        job.notify();
        sweep();
        // Nobody read the end of it (phone locked, page frozen or closed): push a "reply ready" notification.
        if (job.status === 200 && !job.error && !job.abort.signal.aborted && job.root) {
            setTimeout(() => {
                if (job.claimed) return;
                const { text } = replyText(job);
                if (text.trim()) notifyReply(job.root, job.user, { name: job.meta.name, text, chatId: job.meta.chatId }).catch(() => {});
            }, 2500);
        }
    }
}

export function initGeneration(router) {
    // Start a reply: {body: <what the page would send to /api/backends/chat-completions/generate>, meta}.
    router.post('/gen/start', (request, response) => {
        const { body, meta = {} } = request.body || {};
        if (!body || typeof body !== 'object' || body.stream !== true) return response.status(400).send({ error: 'Only streamed replies go through here' });
        const port = request.socket.localPort;
        const headers = { 'Content-Type': 'application/json' };
        for (const name of ['cookie', 'x-csrf-token', 'authorization', 'user-agent']) if (request.headers[name]) headers[name] = request.headers[name];
        const listeners = new Set();
        const job = {
            id: crypto.randomBytes(12).toString('hex'), user: request.user?.profile?.handle || '', root: request.user?.directories?.root || '', created: Date.now(), ended: 0,
            status: 0, contentType: '', chunks: [], size: 0, error: '', claimed: false, abort: new AbortController(),
            meta: {
                chatId: String(meta.chatId || '').slice(0, 300), name: String(meta.name || '').slice(0, 200), avatar: String(meta.avatar || '').slice(0, 300),
                groupId: String(meta.groupId || '').slice(0, 100), type: String(meta.type || '').slice(0, 30), after: String(meta.after || '').slice(0, 2000),
            },
            listeners, notify: () => { for (const fn of [...listeners]) fn(); },
        };
        jobs.set(job.id, job);
        run(job, `http://127.0.0.1:${port}/api/backends/chat-completions/generate`, headers, JSON.stringify(body));
        response.send({ id: job.id });
    });

    // Read the stream from a byte offset. Ends when the reply is complete (or the page goes away).
    router.get('/gen/:id/stream', async (request, response) => {
        const job = jobs.get(request.params.id);
        if (!job || job.user !== (request.user?.profile?.handle || '')) return response.status(404).send({ error: 'No such reply (SillyTavern restarted?)' });
        let from = Math.max(0, Number(request.query.from) || 0);
        if (!job.status && !job.ended) await new Promise(resolve => { const fn = () => { if (job.status || job.ended) { job.listeners.delete(fn); resolve(); } }; job.listeners.add(fn); request.on('close', () => { job.listeners.delete(fn); resolve(); }); });
        if (job.status && job.status !== 200 && from === 0) {   // SillyTavern's own error answer: pass it on as it was
            if (!job.ended) await new Promise(resolve => { const fn = () => { if (job.ended) { job.listeners.delete(fn); resolve(); } }; job.listeners.add(fn); });
            return response.status(job.status).type(job.contentType || 'text/plain').send(bytesFrom(job, 0));
        }
        response.status(200);
        response.set({ 'Content-Type': job.contentType || 'text/event-stream', 'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no',
            'X-Rocsy-Total': String(total(job)) });
        response.flushHeaders?.();
        let closed = false;
        const pump = () => {
            if (closed) return;
            if (job.size > from) { const bytes = bytesFrom(job, from); from += bytes.length; response.write(bytes); }
            if (job.ended && from >= job.size) { finish(); }
        };
        const finish = () => { if (closed) return; closed = true; job.listeners.delete(pump); response.end(); };
        request.on('close', () => { closed = true; job.listeners.delete(pump); });
        job.listeners.add(pump);
        pump();
    });

    // Is it finished, and how big is it? (The page checks this when a read ends, to tell "done" from "dropped".)
    router.get('/gen/:id/state', (request, response) => {
        const job = jobs.get(request.params.id);
        if (!job || job.user !== (request.user?.profile?.handle || '')) return response.status(404).send({ error: 'No such reply' });
        response.send({ id: job.id, done: !!job.ended, size: job.size, status: job.status, error: job.error });
    });

    router.post('/gen/:id/stop', (request, response) => {
        const job = jobs.get(request.params.id);
        if (!job || job.user !== (request.user?.profile?.handle || '')) return response.status(404).send({ error: 'No such reply' });
        job.claimed = true;
        job.abort.abort();
        console.info(`[Rocsy Addons] reply ${job.id.slice(0, 6)} stopped from the page`);
        response.send({ ok: true });
    });

    // The page got the whole reply: no recovery needed.
    router.post('/gen/:id/ack', (request, response) => {
        const job = jobs.get(request.params.id);
        if (job && job.user === (request.user?.profile?.handle || '')) job.claimed = true;
        response.send({ ok: true });
    });

    // After a reload: finished replies for this chat that the page never got.
    router.get('/gen/recover', (request, response) => {
        const chatId = String(request.query.chatId || '');
        const user = request.user?.profile?.handle || '';
        const found = [...jobs.values()].filter(job => job.user === user && !job.claimed && job.ended && job.status === 200 && job.meta.chatId === chatId)
            .sort((a, b) => b.created - a.created)[0];
        if (!found) return response.send({ reply: null });
        const { text, reasoning } = replyText(found);
        if (!text.trim()) return response.send({ reply: null });
        response.send({ reply: { id: found.id, text, reasoning, name: found.meta.name, avatar: found.meta.avatar, after: found.meta.after, type: found.meta.type,
            created: found.created, finished: !found.error } });
    });

    router.post('/gen/recover/:id/dismiss', (request, response) => {
        const job = jobs.get(request.params.id);
        if (job && job.user === (request.user?.profile?.handle || '')) job.claimed = true;
        response.send({ ok: true });
    });
}

export function exitGeneration() {
    for (const job of jobs.values()) job.abort.abort();
    jobs.clear();
}
