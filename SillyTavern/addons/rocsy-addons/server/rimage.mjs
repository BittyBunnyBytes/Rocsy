// Rocsy Image for SillyTavern (part of the Rocsy Addons server plugin).
//
// 1. Character pictures: a llama-swap model writes a portrait prompt from the card, Rocsy Image paints it
//    (/image/avatar → a task with candidate pictures; the page puts the chosen one on the card itself).
// 2. Character expressions: Qwen Image 2.1 edits the character's own picture once per expression label and the
//    results are written straight into SillyTavern's sprites folder (characters/<name>/<label>.png), one render at
//    a time so other GPU work keeps its turn in the Rocsy queue. Runs on the server, so the tab can be closed.
// 3. SillyTavern's own Image Generation (source "Stable Diffusion WebUI / A1111"): a small A1111-compatible API on
//    127.0.0.1:8146 (/sdapi/v1/...) that renders through Rocsy Image, so /sd, the wand menu and per-character
//    prompt prefixes all work with Rocsy's models and the GPU queue.
//
// Renders are ordinary Rocsy Image jobs (they show in Image → Generations, where they can be deleted).

import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { Jimp } from '../../src/jimp.js';

const IMAGE = (process.env.ROCSY_IMAGE_URL || 'http://127.0.0.1:8300/Image').replace(/\/$/, '');
const IMAGE_ORIGIN = new URL(IMAGE).origin;
const SD_PORT = Number(process.env.ROCSY_SDAPI_PORT || 8146);
const QWEN_TURBO = 'qwen_image_2.1_turbo_int8_convrot.safetensors';

// Rocsy Image models, by the A1111 "checkpoint title" SillyTavern shows.
export const MODELS = [
    { id: 'qwen_image_21', title: 'Rocsy · Qwen Image 2.1', max: 2752, edit: true, negative: true },
    { id: 'krea2', title: 'Rocsy · Krea 2', max: 1536 },
    { id: 'lustify', title: 'Rocsy · Lustify (Krea 2)', max: 1536 },
    { id: 'anima', title: 'Rocsy · Anima (anime)', max: 1536 },
    { id: 'moss', title: 'Rocsy · MOSS-Image', max: 1536 },
    { id: 'h3_image', title: 'Rocsy · MiniMax H3 Image', max: 4096, edit: true, picture: '<Picture 1>' },
];
const modelOf = id => MODELS.find(m => m.id === id || m.title === id) || MODELS[0];
// How a model's prompt names the picture it edits (H3 Image labels its references <Picture 1>, <Picture 2>, ...).
const pictureRef = id => modelOf(id).picture || 'this picture';

// The guidance prompt each expression picture is edited with. The page can send its own (the character editor →
// Expressions → Model & guidance prompt). Placeholders: {{char}} the character's name, {{label}} the expression,
// {{hint}} what that expression should look like, {{picture}} how the model names the master picture,
// {{background}} the background instruction (transparent or keep), {{appearance}} optional looks.
export const DEFAULT_EXPRESSION_PROMPT = 'Edit {{picture}} of {{char}}. Keep exactly the same character: the same face, hair, body, outfit, colors, art style, framing and camera angle. '
    + 'Change only the facial expression and body language so they clearly show {{label}}: {{hint}}. {{background}}';

function expressionPrompt(template, values) {
    let text = String(template || '').trim().slice(0, 3000) || DEFAULT_EXPRESSION_PROMPT;
    // Without the expression in it every picture would come out the same: add it.
    if (!/\{\{\s*(label|hint)\s*\}\}/i.test(text)) text = `${text.replace(/[.!?]?$/, m => m || '.')} Show the expression {{label}}: {{hint}}.`;
    return text.replace(/\{\{\s*(char|label|hint|picture|background|appearance)\s*\}\}/gi, (_m, key) => values[key.toLowerCase()] ?? '')
        .replace(/[ \t]{2,}/g, ' ').trim();
}

// SillyTavern's expression labels, each with what the face and body should show.
export const EXPRESSIONS = {
    admiration: 'looking on in admiration, eyes shining, a soft impressed smile',
    amusement: 'amused, a playful grin, about to laugh',
    anger: 'angry, furrowed brows, glaring, clenched jaw',
    annoyance: 'annoyed, an eye-roll or flat unimpressed look, slight frown',
    approval: 'approving, a confident nod and a small satisfied smile',
    caring: 'caring and gentle, warm soft eyes, a kind reassuring smile',
    confusion: 'confused, head tilted, one brow raised, puzzled look',
    curiosity: 'curious, leaning in slightly, wide interested eyes',
    desire: 'longing, half-lidded eyes and a wistful look',
    disappointment: 'disappointed, downcast eyes, a small sigh, mouth turned down',
    disapproval: 'disapproving, arms crossed, stern frown',
    disgust: 'disgusted, wrinkled nose, recoiling slightly',
    embarrassment: 'embarrassed, blushing, looking away shyly',
    excitement: 'excited, big open smile, bright eyes, energetic pose',
    fear: 'afraid, wide eyes, tense shoulders, shrinking back',
    gratitude: 'grateful, a warm thankful smile, hand over heart',
    grief: 'grieving, tears, deep sorrow',
    joy: 'joyful, beaming happy smile, eyes crinkled',
    love: 'loving and affectionate, tender gaze, gentle blush and smile',
    nervousness: 'nervous, anxious smile, fidgeting, a bead of sweat',
    optimism: 'optimistic, hopeful upward gaze, bright smile',
    pride: 'proud, chin up, confident smug smile',
    realization: 'sudden realization, eyes widening, mouth slightly open',
    relief: 'relieved, exhaling, relaxed shoulders, soft smile',
    remorse: 'remorseful, guilty look, eyes lowered, apologetic',
    sadness: 'sad, teary eyes, downturned mouth',
    surprise: 'surprised, wide eyes, raised brows, open mouth',
    neutral: 'calm neutral expression, relaxed face, looking at the viewer',
};

const tasks = new Map();      // id -> task (kept in memory; the newest 30)
let sdCurrent = null;         // the A1111 bridge's running render {job, abort}
let sdModel = MODELS[0].title;
// The bridge's model survives restarts (SillyTavern asks the "WebUI" which model is loaded when it starts).
const STATE_FILE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'rimage-state.json');
try { sdModel = modelOf(JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')).sdModel).title; } catch { /* first run */ }
function setSdModel(value) {
    sdModel = modelOf(value).title;
    try { fs.writeFileSync(STATE_FILE, JSON.stringify({ sdModel })); } catch { /* read-only: memory only */ }
}
let sdServer = null;
let llama = { url: '', key: '' };

// ------------------------------------------------------------------ Rocsy Image calls --

async function imageFetch(pathname, options = {}, timeout = 30000) {
    const response = await fetch(`${IMAGE}${pathname}`, { ...options, signal: options.signal || AbortSignal.timeout(timeout) });
    if (!response.ok) {
        let message = `Rocsy Image answered ${response.status}`;
        try { const data = await response.json(); message = data.message || data.detail || data.error || message; } catch { /* not JSON */ }
        throw new Error(typeof message === 'string' ? message : JSON.stringify(message).slice(0, 300));
    }
    return response;
}

async function installedCheckpoints() {
    try { return await (await imageFetch('/api/checkpoints', {}, 8000)).json(); } catch { return null; }
}

/** The models this Rocsy Image install can render (all of them when it can't say). */
async function availableModels() {
    const files = await installedCheckpoints();
    if (!files) return MODELS.map(m => ({ ...m, ready: false }));
    return MODELS.map(m => {
        if (m.id === 'lustify') return { ...m, ready: (files.krea2 || []).some(f => /lustify/i.test(f)) };
        if (m.id === 'anima') return { ...m, ready: true };
        return { ...m, ready: Array.isArray(files[m.id]) && files[m.id].length > 0 };
    });
}

const size = (value, max) => Math.max(512, Math.min(max, Math.round((Number(value) || 1024) / 32) * 32));

/** Pixel size that fits a model, keeping the aspect ratio. */
function fitSize(model, width, height) {
    let w = Number(width) || 1024, h = Number(height) || 1024;
    const scale = Math.min(1, model.max / Math.max(w, h));
    w *= scale; h *= scale;
    if (Math.min(w, h) < 512) { const up = 512 / Math.min(w, h); w *= up; h *= up; }
    return [size(w, model.max), size(h, model.max)];
}

async function buildRequest({ model: modelId, prompt, negative = '', width, height, seed, reference, transparent = false, fast = true, count = 1 }) {
    const model = modelOf(modelId);
    const [w, h] = fitSize(model, width, height);
    const body = { model: model.id, prompt: String(prompt).slice(0, 3900), width: w, height: h, count: Math.max(1, Math.min(4, Number(count) || 1)) };
    if (Number.isInteger(seed) && seed >= 0 && seed <= 2 ** 32 - 1) body.seed = seed;
    if (model.id === 'qwen_image_21') {
        body.negative_prompt = String(negative || '').slice(0, 3900);
        body.transparent = !!transparent;
        if (fast) {
            const files = await installedCheckpoints();
            if (files?.qwen_image_21?.includes(QWEN_TURBO)) body.checkpoint = QWEN_TURBO;
        }
    }
    if (reference) {
        body.use_references = true;
        body.references = [{ enabled: true, image: reference.image, strength: 1, width: reference.width, height: reference.height }];
        body.match_reference_size = true;
    }
    return body;
}

/** A picture into ComfyUI's input folder (Rocsy Image's reference upload). Re-encoded: card PNGs carry the card text. */
async function uploadReference(buffer, name = 'rocsy-reference.png') {
    const picture = await Jimp.read(buffer);
    const png = await picture.getBuffer('image/png');
    const form = new FormData();
    form.append('file', new Blob([png], { type: 'image/png' }), name.replace(/[^\w.-]+/g, '_'));
    const data = await (await imageFetch('/api/upload', { method: 'POST', body: form }, 60000)).json();
    return { image: data.image, width: picture.bitmap.width, height: picture.bitmap.height };
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/** Queue one Rocsy Image job, wait for it, return its pictures (PNG buffers). onJob sees each poll. */
async function render(request, { signal, onJob } = {}) {
    let job = await (await imageFetch('/api/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request) }, 60000)).json();
    onJob?.(job);
    try {
        while (!['complete', 'failed', 'cancelled'].includes(job.state)) {
            if (signal?.aborted) throw new Error('Stopped');
            await sleep(1500);
            job = await (await imageFetch(`/api/jobs/${job.id}`, {}, 15000)).json();
            onJob?.(job);
        }
    } catch (error) {
        if (signal?.aborted) await imageFetch(`/api/jobs/${job.id}/cancel`, { method: 'POST' }).catch(() => {});
        throw error;
    }
    if (job.state !== 'complete') throw new Error(job.error || (job.state === 'cancelled' ? 'Cancelled in Rocsy Image' : 'The render failed'));
    const urls = job.images?.length ? job.images.map(i => i.url) : [job.output_url].filter(Boolean);
    const pictures = [];
    for (const url of urls) {
        const response = await fetch(`${IMAGE_ORIGIN}${url}`, { signal: AbortSignal.timeout(60000) });
        if (response.ok) pictures.push(Buffer.from(await response.arrayBuffer()));
    }
    if (!pictures.length) throw new Error('Rocsy Image finished without a picture');
    return { job, pictures };
}

// ------------------------------------------------------------------------- llama-swap --

async function ask(model, system, user, maxTokens = 700) {
    const response = await fetch(`${llama.url}/chat/completions`, {
        method: 'POST', signal: AbortSignal.timeout(240000),
        headers: { 'Content-Type': 'application/json', ...(llama.key ? { Authorization: `Bearer ${llama.key}` } : {}) },
        body: JSON.stringify({
            model: String(model), temperature: 0.6, max_tokens: maxTokens, stream: false,
            chat_template_kwargs: { enable_thinking: false }, reasoning_effort: 'none',
            messages: [{ role: 'system', content: system }, { role: 'user', content: user }],
        }),
    });
    if (!response.ok) throw new Error(`llama-swap answered ${response.status}: ${(await response.text()).slice(0, 200)}`);
    const data = await response.json();
    return String(data?.choices?.[0]?.message?.content || '').replace(/<think>[\s\S]*?<\/think>/gi, '').replace(/^[\s\S]*<\/think>/i, '').trim();
}

const cardBlock = (card = {}) => [
    ['Name', card.name], ['Description', card.description], ['Personality', card.personality], ['Scenario', card.scenario],
    ['Creator notes', card.creator_notes], ['Tags', Array.isArray(card.tags) ? card.tags.join(', ') : card.tags],
].filter(([, v]) => String(v || '').trim()).map(([k, v]) => `${k}: ${String(v).replace(/\{\{char\}\}/gi, card.name || 'the character').slice(0, 4000)}`).join('\n\n');

const PORTRAIT_SYSTEM = 'You write one prompt for an image model that paints a character\'s profile picture. Use what the character card says about looks: species, build, apparent age, hair, eyes, skin, face, clothes, accessories, colors, plus a setting and mood that fit them. If the card says nothing about looks, invent a fitting, specific appearance. Begin with the art style (the card\'s if it hints at one, e.g. anime, semi-realistic, photo; otherwise "detailed digital illustration"). Framing: upper-body portrait, facing the viewer, centered, eyes visible, clean background that fits the character. Depict the age, dress and level of nudity the card describes; nothing about the character is off limits. One paragraph of 60-120 words of plain descriptive English. No names of real artists, no text or watermarks in the picture, no quotes, no lists, no preface: reply with only the prompt.';

const MASTER_SYSTEM = 'You write one prompt for an image model that paints a character reference picture, the master picture all of the character\'s expression pictures are later edited from. Use what the character card says about looks: species, build, apparent age, hair, eyes, skin, face, clothes, accessories, colors. If the card says nothing about looks, invent a fitting, specific appearance. Begin with the art style (the card\'s if it hints at one, e.g. anime, semi-realistic, photo; otherwise "detailed digital illustration"). Framing: upper body from the waist up, facing the viewer, centered, calm neutral expression, relaxed face, eyes visible, arms relaxed, even soft lighting, plain light grey background with nothing else in it. Depict the age, dress and level of nudity the card describes; nothing about the character is off limits. One paragraph of 60-120 words of plain descriptive English. No names of real artists, no text or watermarks in the picture, no quotes, no lists, no preface: reply with only the prompt.';

// The master reference: one neutral picture per character that every expression is edited from, so a set stays
// consistent. Kept outside the sprite folder (SillyTavern would list it as an expression): <user root>/rocsy-image/masters.
const masterFiles = (directories, avatar) => {
    const base = path.join(directories.root, 'rocsy-image', 'masters', avatar);
    return { png: base, meta: `${base}.json` };
};
const fileTime = file => { try { return Math.round(fs.statSync(file).mtimeMs); } catch { return 0; } };
function readMaster(directories, avatar) {
    const files = masterFiles(directories, avatar);
    if (!fs.existsSync(files.png)) return null;
    let meta = {};
    try { meta = JSON.parse(fs.readFileSync(files.meta, 'utf8')); } catch { /* no sidecar */ }
    return { ...files, meta };
}
/** Is the saved master still right for this source? (A picture master goes stale when the card picture changes.) */
function masterFresh(master, source, avatarFile) {
    if (master?.meta.source === 'upload') return true;   // the user's own picture: kept until they replace it
    if (!master || master.meta.source !== source) return false;
    return source !== 'picture' || master.meta.avatarTime === fileTime(avatarFile);
}

async function makeMaster(task, directories, { source, card, transparent, fast, model }) {
    const avatarFile = path.join(directories.characters, task.avatar);
    const onJob = job => { task.stage = `Master reference: ${jobStage(job)}`; task.progress = job.progress || 0; };
    let request, prompt;
    if (source === 'picture') {
        task.stage = 'Master reference: uploading the character picture…';
        const reference = await uploadReference(fs.readFileSync(avatarFile), `rocsy-${task.avatar}`);
        prompt = `Edit ${pictureRef(model || 'qwen_image_21')} of ${task.name} into a clean character reference picture. Keep exactly the same character: the same face, hair, body, outfit, colors and art style. `
            + 'Show them from the waist up, facing the viewer, centered, with a calm neutral expression, relaxed face and relaxed arms, in even soft lighting, on a plain light grey background with nothing else in it.';
        request = await buildRequest({ model: model || 'qwen_image_21', prompt, reference, fast, count: 1, width: reference.width, height: reference.height });
    } else {
        if (!card?.model) throw new Error('No chat model to describe the character: connect SillyTavern to llama-swap, or base the master on the character picture');
        task.stage = 'Master reference: writing the prompt from the card…';
        prompt = await ask(card.model, MASTER_SYSTEM, `${cardBlock(card)}\n\nWrite the reference picture prompt.`);
        if (!prompt) throw new Error('The chat model wrote no prompt for the master reference');
        request = await buildRequest({ model: model || 'qwen_image_21', prompt, fast, count: 1, width: 896, height: 1344, negative: 'text, watermark, signature, blurry, deformed hands, extra limbs' });
    }
    task.prompt = prompt;
    const { pictures } = await render(request, { signal: task.abort.signal, onJob });
    const files = masterFiles(directories, task.avatar);
    fs.mkdirSync(path.dirname(files.png), { recursive: true });
    fs.writeFileSync(files.png, pictures[0]);
    fs.writeFileSync(files.meta, JSON.stringify({ source, created: Date.now(), prompt, avatarTime: source === 'picture' ? fileTime(avatarFile) : 0 }));
    task.masterMade = true;
    return pictures[0];
}

// ------------------------------------------------------------------------------ tasks --

function newTask(kind, fields) {
    const task = { id: crypto.randomBytes(8).toString('hex'), kind, state: 'running', created: Date.now(), done: 0, items: [], pictures: [], prompt: '', error: '', stage: 'Starting…', progress: 0, abort: new AbortController(), ...fields };
    tasks.set(task.id, task);
    while (tasks.size > 30) tasks.delete(tasks.keys().next().value);
    return task;
}

const publicTask = task => ({
    id: task.id, kind: task.kind, state: task.state, avatar: task.avatar, name: task.name, created: task.created, stage: task.stage,
    progress: task.progress, prompt: task.prompt, error: task.error, pictures: task.pictures.length, done: task.done,
    items: task.items.map(({ label, state, error }) => ({ label, state, error })), masterMade: !!task.masterMade,
});

const jobStage = job => job.state === 'waiting_for_gpu' ? 'Waiting for the GPU queue…' : job.state === 'rendering' ? `Rendering… ${job.progress || 0}%`
    : job.state === 'enhancing' ? 'Enhancing the prompt…' : job.state === 'complete' ? 'Done' : 'Preparing…';

const AVATAR = /^[^/\\\0]{1,200}\.png$/i;
const avatarOk = avatar => typeof avatar === 'string' && AVATAR.test(avatar) && !avatar.includes('..') && !avatar.startsWith('.');
// SillyTavern's sprite folder is the character's name (sanitize-filename rules, roughly).
const folderOk = name => typeof name === 'string' && name.trim() && name.length <= 200 && !/[/\\\0<>:"|?*]/.test(name) && !/^\.+$/.test(name.trim());

async function runAvatar(task, { model, card, prompt, width, height, count, fast }) {
    try {
        if (!prompt) {
            if (!card?.model) throw new Error('No chat model to write the prompt: connect SillyTavern to llama-swap, or type a prompt');
            task.stage = 'Writing the prompt from the card…';
            prompt = await ask(card.model, PORTRAIT_SYSTEM, `${cardBlock(card)}\n\nWrite the portrait prompt.`);
            if (!prompt) throw new Error('The chat model wrote no prompt');
        }
        task.prompt = prompt;
        const request = await buildRequest({ model, prompt, width: width || 896, height: height || 1344, count: count || 2, fast, negative: 'text, watermark, signature, blurry, deformed hands, extra limbs' });
        const { pictures } = await render(request, { signal: task.abort.signal, onJob: job => { task.stage = jobStage(job); task.progress = job.progress || 0; } });
        task.pictures = pictures;
        task.state = 'done';
        task.stage = 'Done';
    } catch (error) {
        task.state = task.abort.signal.aborted ? 'stopped' : 'failed';
        task.error = String(error.message || error).slice(0, 400);
        task.stage = task.state === 'stopped' ? 'Stopped' : 'Failed';
    }
}

/** Only the master reference (the editor's "Create master" button). */
async function runMaster(task, directories, options) {
    try {
        await makeMaster(task, directories, options);
        task.state = 'done';
        task.stage = 'Done: master reference made';
    } catch (error) {
        task.state = task.abort.signal.aborted ? 'stopped' : 'failed';
        task.error = String(error.message || error).slice(0, 400);
        task.stage = task.state === 'stopped' ? 'Stopped' : 'Failed';
    }
}

async function runExpressions(task, directories, { labels, appearance, transparent, fast, overwrite, model, hints = {}, masterFrom = 'picture', newMaster = false, card = {}, promptTemplate = '' }) {
    const folder = path.join(directories.characters, task.name);
    try {
        // Every expression edits the master reference (made first when there's no fresh one). If making it fails or
        // is cancelled, the whole set stops: no silent fallback to the card picture.
        const saved = readMaster(directories, task.avatar);
        const master = !newMaster && masterFresh(saved, masterFrom, path.join(directories.characters, task.avatar))
            ? fs.readFileSync(saved.png)
            : await makeMaster(task, directories, { source: masterFrom, card, transparent, fast, model });
        if (task.abort.signal.aborted) throw new Error('Stopped');
        task.stage = 'Uploading the master reference…';
        const reference = await uploadReference(master, `rocsy-master-${task.avatar}`);
        fs.mkdirSync(folder, { recursive: true });
        for (const item of task.items) {
            if (task.abort.signal.aborted) { item.state = 'skipped'; continue; }
            const existing = fs.readdirSync(folder).filter(file => path.parse(file).name === item.label);
            if (existing.length && !overwrite) { item.state = 'skipped'; item.error = 'already has one'; task.done++; continue; }
            item.state = 'running';
            task.stage = `${item.label}: starting…`;
            const hint = String(hints[item.label] || '').slice(0, 300) || EXPRESSIONS[item.label] || `showing ${item.label.replace(/[_-]+/g, ' ')}`;
            const canClear = modelOf(model || 'qwen_image_21').id === 'qwen_image_21';   // only Qwen renders transparency
            const prompt = expressionPrompt(promptTemplate, {
                char: task.name, label: item.label, hint, picture: pictureRef(model || 'qwen_image_21'),
                appearance: appearance ? `The character: ${String(appearance).slice(0, 600)}.` : '',
                background: transparent && canClear ? 'Remove the background: the character alone on a transparent background.' : 'Keep the same background.',
            });
            task.prompt = prompt;
            try {
                const request = await buildRequest({ model: model || 'qwen_image_21', prompt, reference, transparent, fast, count: 1, width: reference.width, height: reference.height });
                const { pictures } = await render(request, { signal: task.abort.signal, onJob: job => { task.stage = `${item.label}: ${jobStage(job)}`; task.progress = job.progress || 0; } });
                for (const file of fs.readdirSync(folder)) if (path.parse(file).name === item.label) fs.unlinkSync(path.join(folder, file));
                fs.writeFileSync(path.join(folder, `${item.label}.png`), pictures[0]);
                item.state = 'done';
            } catch (error) {
                item.state = task.abort.signal.aborted ? 'skipped' : 'failed';
                item.error = String(error.message || error).slice(0, 300);
                if (/Cancelled in Rocsy Image/.test(item.error)) task.abort.abort();   // cancelled on the Image page or the queue: stop the set
                else if (/offline|unavailable|missing|not installed/i.test(item.error)) throw error;   // every next one would fail the same way
            }
            task.done++;
        }
        task.state = task.abort.signal.aborted ? 'stopped' : 'done';
        task.stage = task.state === 'stopped' ? 'Stopped' : `Done: ${task.items.filter(i => i.state === 'done').length} of ${task.items.length} made`;
    } catch (error) {
        for (const item of task.items) if (item.state === 'waiting' || item.state === 'running') item.state = 'skipped';
        task.state = task.abort.signal.aborted ? 'stopped' : 'failed';
        task.error = String(error.message || error).slice(0, 400);
        task.stage = task.state === 'stopped' ? 'Stopped' : 'Failed';
    }
}

// ---------------------------------------------------------------- default picture check --

let defaultPrint = null;
async function fingerprint(buffer) {
    const picture = await Jimp.read(buffer);
    picture.resize({ w: 16, h: 24 });
    return picture.bitmap.data;
}

/** True when the card still has SillyTavern's stock picture (img/ai4.png). */
async function isStockPicture(file) {
    try {
        defaultPrint ||= await fingerprint(fs.readFileSync(path.resolve(process.cwd(), 'public/img/ai4.png')));
        const print = await fingerprint(fs.readFileSync(file));
        let diff = 0;
        for (let i = 0; i < print.length; i++) diff += Math.abs(print[i] - defaultPrint[i]);
        return diff / print.length < 8;
    } catch {
        return false;
    }
}

// ------------------------------------------------------------------- the A1111 bridge --

function sdJson(response, status, data) {
    response.writeHead(status, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify(data));
}

async function readBody(request, limit = 40 * 1024 * 1024) {
    const chunks = [];
    let total = 0;
    for await (const chunk of request) {
        total += chunk.length;
        if (total > limit) throw new Error('Request too large');
        chunks.push(chunk);
    }
    const text = Buffer.concat(chunks).toString('utf8');
    return text ? JSON.parse(text) : {};
}

async function sdGenerate(body, image2image) {
    if (sdCurrent) throw new Error('Rocsy Image is already making a picture for SillyTavern');
    const model = modelOf(body.override_settings?.sd_model_checkpoint || sdModel);
    const prompt = String(body.prompt || '').trim();
    if (!prompt) throw new Error('Empty prompt');
    const abort = new AbortController();
    sdCurrent = { abort, progress: 0 };
    try {
        let reference = null;
        if (image2image && body.init_images?.[0]) {
            if (!model.edit) throw new Error(`${model.title} can't edit pictures: pick Qwen Image 2.1`);
            reference = await uploadReference(Buffer.from(String(body.init_images[0]).replace(/^data:[^,]+,/, ''), 'base64'), 'rocsy-sd-init.png');
        }
        const seed = Number(body.seed);
        const request = await buildRequest({ model: model.id, prompt, negative: body.negative_prompt, width: body.width, height: body.height, seed: seed >= 0 ? seed : undefined, reference, count: 1, fast: true });
        const { job, pictures } = await render(request, { signal: abort.signal, onJob: j => { sdCurrent.progress = (j.progress || 0) / 100; } });
        return { images: pictures.map(p => p.toString('base64')), parameters: {}, info: JSON.stringify({ seed: job.seed, prompt, model: model.title, width: job.width, height: job.height }) };
    } finally {
        sdCurrent = null;
    }
}

async function sdHandle(request, response) {
    const url = new URL(request.url, 'http://localhost');
    const route = `${request.method} ${url.pathname.replace(/\/+$/, '')}`;
    try {
        switch (route) {
            case 'GET /sdapi/v1/options': return sdJson(response, 200, { sd_model_checkpoint: sdModel, rocsy: true });
            case 'POST /sdapi/v1/options': {
                const body = await readBody(request);
                if (body.sd_model_checkpoint) setSdModel(body.sd_model_checkpoint);
                return sdJson(response, 200, null);
            }
            case 'GET /sdapi/v1/sd-models':
                return sdJson(response, 200, (await availableModels()).filter(m => m.ready).map(m => ({ title: m.title, model_name: m.id, hash: null, filename: m.id })));
            case 'GET /sdapi/v1/samplers': return sdJson(response, 200, [{ name: 'Rocsy (model default)', aliases: [], options: {} }]);
            case 'GET /sdapi/v1/schedulers': return sdJson(response, 200, [{ name: 'automatic', label: 'Automatic' }]);
            case 'GET /sdapi/v1/upscalers': case 'GET /sdapi/v1/latent-upscale-modes': case 'GET /sdapi/v1/sd-vae': case 'GET /sdapi/v1/sd-modules':
                return sdJson(response, 200, []);
            case 'GET /sdapi/v1/progress':
                return sdJson(response, 200, { progress: sdCurrent ? sdCurrent.progress : 0, eta_relative: 0, state: { job_count: sdCurrent ? 1 : 0 }, current_image: null });
            case 'POST /sdapi/v1/interrupt': case 'POST /sdapi/v1/skip':
                sdCurrent?.abort.abort();
                return sdJson(response, 200, null);
            case 'POST /sdapi/v1/txt2img': return sdJson(response, 200, await sdGenerate(await readBody(request), false));
            case 'POST /sdapi/v1/img2img': return sdJson(response, 200, await sdGenerate(await readBody(request), true));
            default: return sdJson(response, 404, { detail: 'Not Found' });
        }
    } catch (error) {
        return sdJson(response, 500, { error: 'RocsyImageError', detail: String(error.message || error) });
    }
}

function startSdBridge() {
    sdServer = http.createServer((request, response) => { sdHandle(request, response); });
    sdServer.on('error', error => console.warn(`[Rocsy Addons] Image bridge not started on 127.0.0.1:${SD_PORT}: ${error.message}`));
    sdServer.listen(SD_PORT, '127.0.0.1', () => console.log(`[Rocsy Addons] Rocsy Image bridge (A1111 API) on http://127.0.0.1:${SD_PORT}`));
}

// ------------------------------------------------------------------------------ routes --

export function initImage(router, options = {}) {
    llama = { url: options.llama || '', key: options.key || '' };
    startSdBridge();

    router.get('/image', async (_request, response) => {
        let online = false;
        try { await imageFetch('/api/health', {}, 4000); online = true; } catch { /* offline */ }
        response.send({ online, url: IMAGE, bridge: `http://127.0.0.1:${SD_PORT}`, bridgeUp: !!sdServer?.listening, models: await availableModels(), expressions: Object.keys(EXPRESSIONS), expressionPrompt: DEFAULT_EXPRESSION_PROMPT, sdModel });
    });

    router.post('/image/sd-model', (request, response) => {
        setSdModel(String(request.body?.model || ''));
        response.send({ sdModel });
    });

    // Does the card still have SillyTavern's stock picture? And which expressions does it have already?
    router.get('/image/character', async (request, response) => {
        const { avatar = '', name = '' } = request.query;
        if (!avatarOk(avatar)) return response.status(400).send({ error: 'Bad avatar' });
        const directories = request.user.directories;
        const file = path.join(directories.characters, avatar);
        if (!fs.existsSync(file)) return response.status(404).send({ error: 'No such character' });
        let sprites = [];
        if (folderOk(name)) {
            try { sprites = fs.readdirSync(path.join(directories.characters, name)).filter(f => /\.(png|webp|gif|jpe?g)$/i.test(f)).map(f => path.parse(f).name); } catch { /* none yet */ }
        }
        const running = [...tasks.values()].filter(t => t.avatar === avatar && t.state === 'running').map(publicTask);
        const master = readMaster(directories, avatar);
        response.send({ stock: await isStockPicture(file), sprites, running,
            master: master ? { source: master.meta.source || '', created: master.meta.created || fileTime(master.png), fresh: masterFresh(master, master.meta.source, file) } : null });
    });

    // The saved master reference picture (see makeMaster).
    router.get('/image/master', (request, response) => {
        const avatar = String(request.query.avatar || '');
        if (!avatarOk(avatar)) return response.status(400).send({ error: 'Bad avatar' });
        const master = readMaster(request.user.directories, avatar);
        if (!master) return response.status(404).send({ error: 'No master reference yet' });
        response.set('Content-Type', 'image/png').set('Cache-Control', 'private, no-store').send(fs.readFileSync(master.png));
    });

    // Upload your own master reference: {avatar, image: data URL or base64}. Re-encoded to PNG.
    router.post('/image/master', async (request, response) => {
        const { avatar = '', image = '' } = request.body || {};
        if (!avatarOk(avatar)) return response.status(400).send({ error: 'Bad avatar' });
        const directories = request.user.directories;
        if (!fs.existsSync(path.join(directories.characters, avatar))) return response.status(404).send({ error: 'No such character' });
        if ([...tasks.values()].some(t => t.avatar === avatar && t.state === 'running' && ['master', 'expressions'].includes(t.kind))) return response.status(409).send({ error: 'A master or expressions are being made for this character right now' });
        let png;
        try {
            const picture = await Jimp.read(Buffer.from(String(image).replace(/^data:[^,]+,/, ''), 'base64'));
            png = await picture.getBuffer('image/png');
        } catch { return response.status(400).send({ error: 'That file isn’t a picture Rocsy can read (use PNG, JPEG, WebP, GIF or BMP)' }); }
        const files = masterFiles(directories, avatar);
        fs.mkdirSync(path.dirname(files.png), { recursive: true });
        fs.writeFileSync(files.png, png);
        fs.writeFileSync(files.meta, JSON.stringify({ source: 'upload', created: Date.now(), prompt: '', avatarTime: 0 }));
        response.send({ ok: true });
    });

    // Make only the master reference, from the card picture or the card text.
    router.post('/image/master/create', (request, response) => {
        const { avatar = '', name = '', masterFrom = 'picture', card = {}, fast = true, model = 'qwen_image_21' } = request.body || {};
        if (!avatarOk(avatar) || !folderOk(name)) return response.status(400).send({ error: 'Bad character' });
        const directories = request.user.directories;
        if (!fs.existsSync(path.join(directories.characters, avatar))) return response.status(404).send({ error: 'No such character' });
        if (!['picture', 'text'].includes(masterFrom)) return response.status(400).send({ error: 'Bad master source' });
        if ([...tasks.values()].some(t => t.avatar === avatar && t.state === 'running' && ['master', 'expressions'].includes(t.kind))) return response.status(409).send({ error: 'A master or expressions are being made for this character right now' });
        if (!modelOf(model).edit && masterFrom === 'picture') return response.status(400).send({ error: 'A master from the picture needs a model that edits pictures (Qwen Image 2.1 or MiniMax H3 Image)' });
        const task = newTask('master', { avatar, name: name.trim() });
        runMaster(task, directories, { source: masterFrom, card: card && typeof card === 'object' ? card : {}, fast: !!fast, model });
        response.send(publicTask(task));
    });

    router.delete('/image/master', (request, response) => {
        const avatar = String(request.query.avatar || '');
        if (!avatarOk(avatar)) return response.status(400).send({ error: 'Bad avatar' });
        const files = masterFiles(request.user.directories, avatar);
        for (const file of [files.png, files.meta]) fs.rmSync(file, { force: true });
        response.send({ ok: true });
    });

    router.post('/image/avatar', async (request, response) => {
        const { avatar = '', model = 'qwen_image_21', card = {}, prompt = '', width, height, count = 2, fast = true } = request.body || {};
        if (avatar && !avatarOk(avatar)) return response.status(400).send({ error: 'Bad avatar' });
        const task = newTask('avatar', { avatar, name: String(card.name || '') });
        runAvatar(task, { model, card, prompt: String(prompt || '').trim(), width, height, count, fast });
        response.send(publicTask(task));
    });

    router.post('/image/expressions', async (request, response) => {
        const { avatar = '', name = '', labels = [], appearance = '', transparent = true, fast = true, overwrite = false, model = 'qwen_image_21', hints = {}, masterFrom = 'picture', newMaster = false, card = {}, promptTemplate = '' } = request.body || {};
        if (!avatarOk(avatar) || !folderOk(name)) return response.status(400).send({ error: 'Bad character' });
        const directories = request.user.directories;
        if (!fs.existsSync(path.join(directories.characters, avatar))) return response.status(404).send({ error: 'No such character' });
        if ([...tasks.values()].some(t => ['expressions', 'master'].includes(t.kind) && t.avatar === avatar && t.state === 'running')) return response.status(409).send({ error: 'Expressions or a master are already being made for this character' });
        const list = [...new Set((Array.isArray(labels) && labels.length ? labels : Object.keys(EXPRESSIONS)).map(l => String(l).trim().toLowerCase()).filter(l => /^[\p{L}\p{N}_ -]{1,40}$/u.test(l)))];
        if (!list.length) return response.status(400).send({ error: 'No expressions picked' });
        if (!['picture', 'text'].includes(masterFrom)) return response.status(400).send({ error: 'Bad master source' });
        if (!modelOf(model).edit) return response.status(400).send({ error: 'Expressions need a model that edits pictures (Qwen Image 2.1 or MiniMax H3 Image)' });
        const task = newTask('expressions', { avatar, name: name.trim(), items: list.map(label => ({ label, state: 'waiting', error: '' })) });
        runExpressions(task, directories, { labels: list, appearance, transparent: !!transparent, fast: !!fast, overwrite: !!overwrite, model, hints: hints && typeof hints === 'object' ? hints : {}, masterFrom, newMaster: !!newMaster, card: card && typeof card === 'object' ? card : {}, promptTemplate: typeof promptTemplate === 'string' ? promptTemplate : '' });
        response.send(publicTask(task));
    });

    router.get('/image/task/:id', (request, response) => {
        const task = tasks.get(request.params.id);
        return task ? response.send(publicTask(task)) : response.status(404).send({ error: 'Task not found (SillyTavern was restarted?)' });
    });

    router.get('/image/task/:id/picture/:index', (request, response) => {
        const picture = tasks.get(request.params.id)?.pictures[Number(request.params.index)];
        if (!picture) return response.status(404).send({ error: 'No such picture' });
        response.set('Content-Type', 'image/png').set('Cache-Control', 'private, no-store').send(picture);
    });

    router.post('/image/task/:id/stop', (request, response) => {
        const task = tasks.get(request.params.id);
        if (!task) return response.status(404).send({ error: 'Task not found' });
        task.abort.abort();
        response.send(publicTask(task));
    });
}

export function exitImage() {
    for (const task of tasks.values()) task.abort.abort();
    sdCurrent?.abort.abort();
    sdServer?.close();
}
