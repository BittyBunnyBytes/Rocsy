// "Reply ready" notifications and keeping the screen awake (client half; the server half is server/rpush.mjs).
//
// - Notifications: this page registers Rocsy's service worker (served by the plugin at api/plugins/rocsy/sw.js,
//   scope = the SillyTavern page) and subscribes to Web Push. When a reply finishes while nobody is reading it
//   (phone locked, page frozen), the server pushes an empty message and the worker shows "<name> replied".
//   While the page is alive but in the background, it shows the notification itself (and marks the tab title).
// - Keep awake: a screen wake lock while a reply is being written (and a short while after, for the voice), so the
//   phone doesn't lock mid-reply and drop the connection in the first place.
// Settings: extension_settings['rocsy-addons'].phone = {keepAwake, awakeAfter (s), titleBadge}.

const API = '/api/plugins/rocsy';
const ctx = () => globalThis.SillyTavern.getContext();
const base = () => location.pathname.replace(/[^/]*$/, '');   // '/SillyTavern/' through the gateway, '/' direct
const DEFAULTS = {keepAwake: true, awakeAfter: 90, titleBadge: true};

export function phoneSettings() {
    const all = ctx().extensionSettings;
    all['rocsy-addons'] ||= {};
    const s = all['rocsy-addons'];
    s.phone = {...DEFAULTS, ...(s.phone || {})};
    return s.phone;
}
const savePhone = () => ctx().saveSettingsDebounced();

// ------------------------------------------------------------ notifications --

const pushSupported = () => window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
let registration = null;

async function worker() {
    if (registration) return registration;
    // The plugin path goes through the gateway's prefix like every other request (fetch is rewritten by the
    // gateway shim, but serviceWorker.register isn't), so the URL is built from the page's own path.
    registration = await navigator.serviceWorker.register(`${base()}api/plugins/rocsy/sw.js`, {scope: base()});
    return registration;
}

const keyBytes = key => {
    const raw = atob(key.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((key.length + 3) % 4));
    return Uint8Array.from(raw, ch => ch.charCodeAt(0));
};
const post = (path, body) => fetch(API + path, {method: 'POST', headers: {...ctx().getRequestHeaders(), 'Content-Type': 'application/json'}, body: JSON.stringify(body)});

async function state() {
    if (!pushSupported()) return {supported: false, reason: window.isSecureContext ? 'This browser can’t show web notifications.' : 'Notifications need the https address of Rocsy.'};
    const permission = Notification.permission;
    let subscribed = false, preview = false, devices = 0;
    try {
        const reg = await navigator.serviceWorker.getRegistration(base());
        const sub = await reg?.pushManager.getSubscription();
        const answer = await (await fetch(`${API}/push/state?endpoint=${encodeURIComponent(sub?.endpoint || '')}`, {headers: ctx().getRequestHeaders(), cache: 'no-store'})).json();
        subscribed = !!(sub && answer.subscribed);
        preview = !!answer.preview;
        devices = answer.devices || 0;
    } catch { /* not yet */ }
    return {supported: true, permission, subscribed, preview, devices};
}

/** Turn notifications on for this device (must run from a click: the browser asks for permission). */
async function enable({preview = false} = {}) {
    if (!pushSupported()) throw new Error('This browser can’t show notifications here.');
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') throw new Error(permission === 'denied' ? 'Notifications are blocked for this site. Allow them in the browser’s site settings.' : 'Notifications weren’t allowed.');
    const reg = await worker();
    await navigator.serviceWorker.ready;
    const {key} = await (await fetch(`${API}/push/key`, {headers: ctx().getRequestHeaders(), cache: 'no-store'})).json();
    let sub = await reg.pushManager.getSubscription();
    if (sub && sub.options?.applicationServerKey) {
        const old = new Uint8Array(sub.options.applicationServerKey);
        const now = keyBytes(key);
        if (old.length !== now.length || old.some((b, i) => b !== now[i])) { await sub.unsubscribe(); sub = null; }
    }
    sub ||= await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: keyBytes(key)});
    const device = `${navigator.userAgentData?.platform || ''} ${/Mobi/.test(navigator.userAgent) ? 'phone' : 'browser'}`.trim();
    const response = await post('/push/subscribe', {subscription: sub.toJSON(), preview, device});
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || 'The server refused the subscription.');
}

async function disable() {
    const reg = await navigator.serviceWorker.getRegistration(base());
    const sub = await reg?.pushManager.getSubscription();
    if (sub) { await post('/push/unsubscribe', {endpoint: sub.endpoint}).catch(() => {}); await sub.unsubscribe().catch(() => {}); }
}

async function setPreview(preview) {
    const reg = await navigator.serviceWorker.getRegistration(base());
    const sub = await reg?.pushManager.getSubscription();
    if (!sub) return;
    await post('/push/subscribe', {subscription: sub.toJSON(), preview: !!preview});
}

const test = () => post('/push/test', {});

// The page is alive but in the background: say so in the tab title and, with permission, a notification.
let titleBefore = '';
function markTitle() {
    if (!phoneSettings().titleBadge || titleBefore) return;
    titleBefore = document.title;
    document.title = `● Reply ready · ${titleBefore}`;
}
function clearTitle() {
    if (!titleBefore) return;
    document.title = titleBefore;
    titleBefore = '';
}

async function localNotice() {
    if (document.visibilityState === 'visible') return;
    markTitle();
    if (!pushSupported() || Notification.permission !== 'granted') return;
    try {
        const reg = await navigator.serviceWorker.getRegistration(base());
        const sub = await reg?.pushManager.getSubscription();
        if (!reg || !sub) return;   // notifications are off for this device
        const c = ctx();
        const last = c.chat?.at?.(-1);
        if (!last || last.is_user) return;
        const {preview} = await (await fetch(`${API}/push/state?endpoint=${encodeURIComponent(sub.endpoint)}`, {headers: c.getRequestHeaders(), cache: 'no-store'})).json();
        const text = String(last.mes || '').replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/\s+/g, ' ').trim();
        await reg.showNotification(`${last.name || c.name2 || 'Your character'} replied`, {
            body: preview && text ? text.slice(0, 140) + (text.length > 140 ? '…' : '') : 'The reply is ready. Tap to read it.',
            tag: `rocsy-reply-${c.getCurrentChatId?.() || ''}`, renotify: true, icon: `${base()}img/ai4.png`, data: {url: base()},
        });
    } catch (error) {
        console.debug('[Rocsy Addons] notification', error);
    }
}

// --------------------------------------------------------------- keep awake --

const wake = {lock: null, generating: false, releaseTimer: null};
const wakeSupported = () => 'wakeLock' in navigator && window.isSecureContext;

async function holdAwake() {
    if (!phoneSettings().keepAwake || !wakeSupported() || document.visibilityState !== 'visible' || wake.lock) return;
    try {
        wake.lock = await navigator.wakeLock.request('screen');
        wake.lock.addEventListener('release', () => { wake.lock = null; });
    } catch { wake.lock = null; }   // low battery mode etc.
}
function letSleep() {
    clearTimeout(wake.releaseTimer);
    wake.releaseTimer = null;
    wake.lock?.release().catch(() => {});
    wake.lock = null;
}
function generationStarted(_type, _options, dryRun) {
    if (dryRun) return;
    wake.generating = true;
    clearTimeout(wake.releaseTimer);
    holdAwake();
}
function generationEnded() {
    if (!wake.generating) return;
    wake.generating = false;
    clearTimeout(wake.releaseTimer);
    // A little longer, so the voice can read the reply out.
    wake.releaseTimer = setTimeout(letSleep, Math.max(0, Number(phoneSettings().awakeAfter) || 0) * 1000);
}

// -------------------------------------------------------------------- start --

export function initPhoneHelpers() {
    const c = ctx();
    phoneSettings();
    c.eventSource.on(c.eventTypes.GENERATION_STARTED, generationStarted);
    c.eventSource.on(c.eventTypes.GENERATION_ENDED, generationEnded);
    c.eventSource.on(c.eventTypes.GENERATION_STOPPED, () => { wake.generating = false; letSleep(); });
    c.eventSource.on(c.eventTypes.MESSAGE_RECEIVED, (_id, type) => { if (type !== 'first_message') setTimeout(localNotice, 300); });
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
            clearTitle();
            if (wake.generating || wake.releaseTimer) holdAwake();   // the lock is dropped whenever the page is hidden
        }
    });
    addEventListener('focus', clearTitle);
    // Keep the worker up to date where notifications are on (cheap: the browser checks the script itself).
    if (pushSupported() && Notification.permission === 'granted') navigator.serviceWorker.getRegistration(base()).then(reg => reg?.update()).catch(() => {});
}

export const notify = {supported: pushSupported, state, enable, disable, setPreview, test, wakeSupported, settings: phoneSettings, save: savePhone};
