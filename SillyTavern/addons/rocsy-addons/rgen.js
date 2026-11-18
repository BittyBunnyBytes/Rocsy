// Replies that survive a dropped connection (client half; the server half is server/rgen.mjs).
//
// SillyTavern's streamed chat-completion call (POST /api/backends/chat-completions/generate) is handed to Rocsy
// Addons, which makes the call on the server and keeps the stream. This page reads it back by byte offset and gives
// SillyTavern an ordinary streaming Response. When the read breaks (phone locked, app switched, Wi-Fi ↔ mobile data)
// it quietly picks up where it left off once the tab is back, so the reply finishes as if nothing happened. Stop
// still stops it. If the page was reloaded mid-reply, opening the chat offers to add the finished reply.

const API = '/api/plugins/rocsy';
const ctx = () => globalThis.SillyTavern.getContext();
const GENERATE = /\/api\/backends\/chat-completions\/generate$/;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let lastType = 'normal';
let original = null;
// SillyTavern stops a streaming reply on beforeunload. A reload or a phone discarding the tab must NOT stop it on the
// server (that's the reply we want to recover), so aborts while the page is going away are ignored. Capture-phase
// listener: runs before SillyTavern's own (bubbling) one at the window.
let unloading = false;
addEventListener('beforeunload', () => { unloading = true; setTimeout(() => { unloading = false; }, 1500); }, {capture: true});   // still here 1.5 s later: the reload was cancelled
addEventListener('pagehide', () => { unloading = true; }, {capture: true});
addEventListener('pageshow', () => { unloading = false; });

const json = (body, extra = {}) => ({...extra, method: 'POST', headers: {...ctx().getRequestHeaders(), 'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const get = path => original(API + path, {headers: ctx().getRequestHeaders(), cache: 'no-store'});

/** Wait until the tab is visible and online (a frozen tab can't read anyway). */
function backAgain() {
    if (document.visibilityState === 'visible' && navigator.onLine !== false) return Promise.resolve();
    return new Promise(resolve => {
        const check = () => {
            if (document.visibilityState !== 'visible' || navigator.onLine === false) return;
            document.removeEventListener('visibilitychange', check); removeEventListener('online', check); removeEventListener('focus', check);
            resolve();
        };
        document.addEventListener('visibilitychange', check); addEventListener('online', check); addEventListener('focus', check);
    });
}

function metaNow() {
    const c = ctx();
    const last = c.chat?.at?.(-1);
    return {chatId: c.getCurrentChatId?.() || '', name: c.name2 || '', avatar: c.characters?.[c.characterId]?.avatar || '', groupId: c.groupId || '',
        type: lastType, after: last?.is_user ? String(last.mes || '').slice(0, 2000) : ''};
}

async function resilientGenerate(input, init, body) {
    const signal = init?.signal;
    const start = await original(API + '/gen/start', json({body, meta: metaNow()}, {signal}));
    if (!start.ok) return original(input, init);   // server part not there (out of date): SillyTavern's own way
    const {id} = await start.json();
    // Deferred a tick: SillyTavern aborts inside its own beforeunload handler, which can run before ours.
    const stop = () => setTimeout(() => { if (!unloading) original(API + `/gen/${id}/stop`, json({})).catch(() => {}); }, 0);
    let offset = 0;
    let reader = null;

    // The first read decides the status: SillyTavern's own error answers come back as they were.
    const open = () => original(API + `/gen/${id}/stream?from=${offset}`, {headers: ctx().getRequestHeaders(), cache: 'no-store', signal});
    let first;
    try { first = await open(); } catch (error) { if (signal?.aborted) stop(); throw error; }
    if (!first.ok) return first;
    reader = first.body.getReader();

    const stream = new ReadableStream({
        async pull(controller) {
            for (let attempt = 0; ; attempt++) {
                try {
                    if (!reader) {
                        const response = await open();
                        if (!response.ok) throw new Error(`Reading the reply failed (${response.status})`);
                        reader = response.body.getReader();
                    }
                    const {done, value} = await reader.read();
                    if (!done) { offset += value.byteLength; controller.enqueue(value); return; }
                    reader = null;
                    const state = await (await get(`/gen/${id}/state`)).json();   // finished, or did the read just end early?
                    if (state.done && offset >= state.size) {
                        controller.close();
                        original(API + `/gen/${id}/ack`, json({})).catch(() => {});
                        return;
                    }
                } catch (error) {
                    reader = null;
                    if (signal?.aborted) throw error;
                    if (/No such reply/.test(String(error?.message))) throw error;
                    if (attempt > 0) console.info('[Rocsy Addons] reply connection dropped, resuming at byte', offset);
                    await backAgain();
                    await sleep(Math.min(5000, 400 * (attempt + 1)));
                    if (attempt > 200) throw new Error('Lost the connection to SillyTavern while the reply was being written');
                }
            }
        },
        cancel() { stop(); },
    });
    signal?.addEventListener('abort', () => { stop(); try { reader?.cancel(); } catch { /* gone */ } }, {once: true});
    return new Response(stream, {status: 200, headers: {'Content-Type': first.headers.get('content-type') || 'text/event-stream'}});
}

function wrapFetch() {
    if (window.__rocsyResilientWrapped) return;
    window.__rocsyResilientWrapped = true;
    original = window.fetch;
    window.fetch = function (input, init) {
        try {
            const url = typeof input === 'string' ? input : input?.url || '';
            const method = (init?.method || input?.method || 'GET').toUpperCase();
            if (method === 'POST' && GENERATE.test(new URL(url, location.href).pathname) && typeof init?.body === 'string') {
                const body = JSON.parse(init.body);
                if (body?.stream === true) return resilientGenerate(input, init, body);
            }
        } catch { /* fall through */ }
        return original.apply(this, arguments);
    };
}

/** After a reload: a reply this chat never got (the page closed mid-reply) can be added. */
async function offerRecovery() {
    const c = ctx();
    const chatId = c.getCurrentChatId?.();
    if (!chatId || !original) return;
    let reply;
    try { ({reply} = await (await get(`/gen/recover?chatId=${encodeURIComponent(chatId)}`)).json()); } catch { return; }
    if (!reply || c.getCurrentChatId?.() !== chatId) return;
    const last = c.chat?.at?.(-1);
    const before = c.chat?.at?.(-2);
    const same = message => message?.is_user && String(message.mes || '').slice(0, 2000) === reply.after;
    const squash = text => String(text || '').replace(/\s+/g, ' ').trim();
    // Either the chat ends with the user's message (nothing of the reply was saved), or SillyTavern saved the part it
    // had when the page went away: then that message is filled in with the whole reply.
    const append = ['normal', ''].includes(reply.type) && same(last);
    const fill = ['normal', ''].includes(reply.type) && !last?.is_user && same(before) && squash(reply.text).startsWith(squash(last?.mes).slice(0, 400))
        && squash(reply.text).length > squash(last?.mes).length;
    const dismiss = () => original(API + `/gen/recover/${reply.id}/dismiss`, json({})).catch(() => {});
    if (!append && !fill) return dismiss();
    const preview = reply.text.length > 160 ? `${reply.text.slice(0, 160)}…` : reply.text;
    const ok = await c.callGenericPopup(`${reply.name || 'The character'}’s last reply was cut off when the page closed, but it ${fill ? 'was finished' : 'finished'} on the server${reply.finished ? '' : ' (partly)'}. Add it to the chat?\n\n“${preview}”`,
        c.POPUP_TYPE?.CONFIRM ?? 2, '', {okButton: 'Add it', cancelButton: 'Discard'});
    await dismiss();
    if (!ok || ctx().getCurrentChatId?.() !== chatId) return;
    if (fill) {
        const id = c.chat.length - 1;
        last.mes = reply.text;
        if (Array.isArray(last.swipes)) last.swipes[last.swipe_id ?? 0] = reply.text;
        if (reply.reasoning) { last.extra ||= {}; last.extra.reasoning = reply.reasoning; }
        c.updateMessageBlock(id, last);
        await c.saveChat();
        toastr.success('Reply completed', 'Rocsy');
        return;
    }
    const message = {name: reply.name || c.name2, is_user: false, is_system: false, send_date: new Date().toISOString(), mes: reply.text,
        extra: reply.reasoning ? {reasoning: reply.reasoning} : {}};
    if (reply.avatar && c.groupId) message.original_avatar = reply.avatar;
    c.chat.push(message);
    c.addOneMessage(message);
    await c.saveChat();
    toastr.success('Reply added', 'Rocsy');
}

export function initResilientReplies() {
    wrapFetch();
    const c = ctx();
    c.eventSource.on(c.eventTypes.GENERATION_STARTED, (type, _options, dryRun) => { if (!dryRun) lastType = type || 'normal'; });
    c.eventSource.on(c.eventTypes.CHAT_CHANGED, () => setTimeout(() => offerRecovery().catch(() => {}), 1200));
}
