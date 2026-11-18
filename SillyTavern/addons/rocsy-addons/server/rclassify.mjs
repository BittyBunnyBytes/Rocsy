// Expression classifiers for SillyTavern's character expressions (part of the Rocsy Addons server plugin).
// SillyTavern's "Local" classifier always runs the one ONNX model named in config.yaml. With a Rocsy classifier
// picked (Rocsy Addons → Image), the page sends its /api/extra/classify calls here instead, and the request names
// the backend:
//   onnx - any transformers.js text-classification model from Hugging Face (downloaded once into data/_cache, runs
//          on the CPU in SillyTavern's own transformers pipeline, so only one is loaded at a time);
//   llm  - a llama-swap model picks one label, held to the label list by a GBNF grammar (thinking off, a few tokens).

import path from 'node:path';
import '../../src/transformers.js';   // configures the library (local WASM, threads) before we use it
import { pipeline } from 'sillytavern-transformers';
import { getConfigValue } from '../../src/util.js';

const TASK = 'text-classification';

// Checked on Hugging Face (2026-11-14): transformers.js layout with onnx/model_quantized.onnx.
export const ONNX_MODELS = [
    { id: 'Cohee/distilbert-base-uncased-go-emotions-onnx', name: 'DistilBERT GoEmotions (SillyTavern’s default)', labels: 28, size: 'small, fast' },
    { id: 'Cohee/roberta-base-go_emotions-onnx', name: 'RoBERTa GoEmotions', labels: 28, size: 'medium, more accurate' },
    { id: 'SamLowe/roberta-base-go_emotions-onnx', name: 'RoBERTa GoEmotions (SamLowe)', labels: 28, size: 'medium, more accurate' },
    { id: 'onnx-community/tanaos-emotion-detection-v1-ONNX', name: 'Tanaos emotion detection', labels: 8, size: 'joy, anger, fear, sadness, surprise, disgust, excitement, neutral' },
    { id: 'onnx-community/emotion-english-distilroberta-base-ONNX', name: 'Emotion English DistilRoBERTa (Ekman)', labels: 7, size: 'anger, disgust, fear, joy, neutral, sadness, surprise' },
    { id: 'Cohee/bert-base-uncased-emotion-onnx', name: 'BERT emotion', labels: 6, size: 'sadness, joy, love, anger, fear, surprise' },
    { id: 'onnx-community/albert-base-v2-emotion-ONNX', name: 'ALBERT emotion', labels: 6, size: 'sadness, joy, love, anger, fear, surprise' },
];

export const GO_EMOTIONS = ['admiration', 'amusement', 'anger', 'annoyance', 'approval', 'caring', 'confusion', 'curiosity', 'desire',
    'disappointment', 'disapproval', 'disgust', 'embarrassment', 'excitement', 'fear', 'gratitude', 'grief', 'joy', 'love', 'nervousness',
    'optimism', 'pride', 'realization', 'relief', 'remorse', 'sadness', 'surprise', 'neutral'];

const MODEL_ID = /^[\w.-]{1,96}\/[\w.-]{1,96}$/;
const cache = new Map();     // `${backend}|${model}|${text}` -> classification
let llama = { url: '', key: '' };
let loading = null;          // one ONNX model load at a time
// Our own pipeline (not SillyTavern's shared one: a model that fails to load would leave that one disposed).
const onnx = { model: '', pipe: null };

const remember = (key, value) => {
    cache.set(key, value);
    while (cache.size > 500) cache.delete(cache.keys().next().value);
    return value;
};

const labelList = labels => {
    const list = (Array.isArray(labels) && labels.length ? labels : GO_EMOTIONS).map(l => String(l).trim().toLowerCase()).filter(l => /^[\p{L}\p{N}_ -]{1,40}$/u.test(l));
    return [...new Set(list)].slice(0, 80);
};

async function onnxPipeline(model) {
    if (!MODEL_ID.test(model)) throw new Error('Not a Hugging Face model id (owner/name)');
    while (loading) await loading.catch(() => {});
    if (onnx.model === model && onnx.pipe) return onnx.pipe;
    loading = (async () => {
        console.log(`[Rocsy Addons] Loading expression classifier ${model}`);
        const pipe = await pipeline(TASK, model, { cache_dir: path.join(globalThis.DATA_ROOT, '_cache'), quantized: true,
            local_files_only: !getConfigValue('extensions.models.autoDownload', true, 'boolean') });
        const old = onnx.pipe;
        Object.assign(onnx, { model, pipe });
        await old?.dispose?.().catch?.(() => {});
        return pipe;
    })();
    try { return await loading; } catch (error) {
        throw new Error(/IR version/i.test(String(error?.message || error)) || /create a session/i.test(String(error?.message || error))
            ? `${model} is in a newer ONNX format than SillyTavern's runtime can load` : String(error?.message || error));
    } finally { loading = null; }
}

async function onnxLabels(model) {
    const pipe = await onnxPipeline(model);
    return Object.keys(pipe.model.config.label2id || {}).length ? Object.keys(pipe.model.config.label2id) : Object.values(pipe.model.config.id2label || {});
}

async function classifyOnnx(model, text, options = null) {
    const pipe = await onnxPipeline(model);
    if (!options) {
        const result = await pipe(text, { topk: 5 });
        return (Array.isArray(result) ? result : [result]).sort((a, b) => b.score - a.score);
    }
    // A pseudo model on ONNX: each of its labels scores the sum of the model labels it is made from.
    const all = await pipe(text, { topk: Object.keys(pipe.model.config.id2label || {}).length || 64 });
    const base = new Map((Array.isArray(all) ? all : [all]).map(x => [String(x.label).toLowerCase(), x.score]));
    const scored = options.map(o => ({ label: o.label, score: (o.from?.length ? o.from : [o.label]).reduce((sum, l) => sum + (base.get(String(l).toLowerCase()) || 0), 0) }));
    const total = scored.reduce((sum, x) => sum + x.score, 0) || 1;
    return scored.map(x => ({ label: x.label, score: x.score / total })).sort((a, b) => b.score - a.score);
}

const gbnf = labels => `root ::= ${labels.map(l => JSON.stringify(l)).join(' | ')}`;
const authHeaders = () => (llama.key ? { Authorization: `Bearer ${llama.key}` } : {});
const DEFAULT_QUESTION = 'Which emotion does the speaker of this roleplay message show most strongly?';

async function classifyLlm(model, text, options, instructions) {
    const labels = options.map(o => o.label);
    const described = options.some(o => o.description);
    const response = await fetch(`${llama.url}/chat/completions`, {
        method: 'POST', signal: AbortSignal.timeout(120000),
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
            model, temperature: 0, max_tokens: 16, stream: false, grammar: gbnf(labels),
            chat_template_kwargs: { enable_thinking: false }, reasoning_effort: 'none',
            messages: [
                { role: 'system', content: `You read one roleplay message. ${instructions || DEFAULT_QUESTION} Reply with exactly one label from this list${described ? ' (label: what it means)' : ''}:\n`
                    + options.map(o => (described && o.description ? `${o.label}: ${o.description}` : o.label)).join('\n') },
                { role: 'user', content: String(text).slice(0, 4000) },
            ],
        }),
    });
    if (!response.ok) throw new Error(`llama-swap answered ${response.status}: ${(await response.text()).slice(0, 200)}`);
    const data = await response.json();
    const answer = String(data?.choices?.[0]?.message?.content || '').replace(/<think>[\s\S]*?<\/think>/gi, '').trim().toLowerCase();
    const label = labels.find(l => l === answer) || labels.find(l => answer.includes(l)) || (labels.includes('neutral') ? 'neutral' : labels[0]);
    return [{ label, score: 1 }];
}

// Decision models (Laya, Jev-Style, OpenJev, Lev, Kev, Julia…): llama.cpp's /v1/systemone, one forward pass, a
// probability per label. Reached through llama-swap's /upstream/<model>/ passthrough.
async function classifyDecision(model, text, options, instructions) {
    // A URL = another systemone-compatible server (e.g. `jev-style serve` for Jev-Style models); otherwise a llama-swap model.
    const direct = /^https?:\/\/[^\s]+$/i.test(model);
    const root = llama.url.replace(/\/v1\/?$/, '');
    const url = direct ? `${model.replace(/\/+$/, '').replace(/\/v1\/systemone$/, '')}/v1/systemone` : `${root}/upstream/${encodeURIComponent(model)}/v1/systemone`;
    const response = await fetch(url, {
        method: 'POST', signal: AbortSignal.timeout(180000),
        headers: { 'Content-Type': 'application/json', ...(direct ? {} : authHeaders()) },
        body: JSON.stringify({
            state: String(text).slice(0, 3000),   // Laya reads ~1k tokens: the end of a reply carries its mood best
            questions: { emotion: { type: 'choice', instructions: instructions || DEFAULT_QUESTION,
                criteria: Object.fromEntries(options.map(o => [o.label, o.description || null])) } },
        }),
    });
    if (response.status === 404 || response.status === 501) {
        throw new Error(`${model} doesn't answer /v1/systemone: it needs a decision model (Laya, Jev-Style, OpenJev…) on a llama.cpp build from 2026-10-02 or newer`);
    }
    if (!response.ok) throw new Error(`The decision model answered ${response.status}: ${(await response.text()).slice(0, 200)}`);
    const answer = (await response.json())?.answers?.emotion;
    const probabilities = answer?.probabilities || (answer?.choice ? { [answer.choice]: 1 } : {});
    return Object.entries(probabilities).map(([label, score]) => ({ label, score: Number(score) || 0 })).sort((a, b) => b.score - a.score);
}

// A classifier request → {engine, model, instructions, options}. options = [{label, description, from}] or null (an
// ONNX model's own labels). backend: onnx | llm | decision | pseudo (a user-made label set on one of the others).
const optionList = (list, hints = {}) => {
    const seen = new Set();
    return (Array.isArray(list) ? list : []).map(o => (typeof o === 'string' ? { label: o } : o || {}))
        .map(o => ({ label: String(o.label || '').trim().toLowerCase(), description: String(o.description ?? hints[String(o.label || '').trim().toLowerCase()] ?? '').trim().slice(0, 300),
            from: Array.isArray(o.from) ? o.from.map(x => String(x).trim().toLowerCase()).filter(Boolean).slice(0, 12) : [] }))
        .filter(o => /^[\p{L}\p{N}_ -]{1,40}$/u.test(o.label) && !seen.has(o.label) && seen.add(o.label)).slice(0, 80);
};

function specOf(body = {}, hints = {}) {
    const { backend = 'onnx', model = '', labels, pseudo } = body;
    if (backend === 'pseudo') {
        const engine = ['onnx', 'llm', 'decision'].includes(pseudo?.engine) ? pseudo.engine : 'llm';
        const options = optionList(pseudo?.labels);
        if (options.length < 2) throw new Error('A pseudo emotion model needs at least two labels');
        return { engine, model: String(pseudo?.model || (engine === 'onnx' ? ONNX_MODELS[0].id : '')), instructions: String(pseudo?.instructions || '').slice(0, 500), options };
    }
    if (backend === 'onnx') return { engine: 'onnx', model: String(model || ONNX_MODELS[0].id), instructions: '', options: null };
    return { engine: backend === 'decision' ? 'decision' : 'llm', model: String(model || ''), instructions: '', options: optionList(labelList(labels), hints) };
}

export function initClassifier(router, options = {}) {
    llama = { url: options.llama || '', key: options.key || '' };
    const hints = options.hints || {};

    router.get('/classify/models', async (_request, response) => {
        let models = [];
        try {
            const data = await (await fetch(`${llama.url}/models`, { headers: authHeaders(), signal: AbortSignal.timeout(8000) })).json();
            models = (data?.data || []).map(m => ({ id: String(m.id), name: String(m.name || m.id),
                decision: (m.architecture?.output_modalities || []).includes('decisions') || /laya|jev|lev|kev|julia|decision|clef/i.test(`${m.id} ${m.name || ''}`) }));
        } catch { /* llama-swap offline: ONNX only */ }
        response.send({ onnx: ONNX_MODELS, llama: models, goEmotions: GO_EMOTIONS, hints });
    });

    // The label list SillyTavern offers sprites for (its /api/extra/classify/labels).
    router.post('/classify/labels', async (request, response) => {
        try {
            const spec = specOf(request.body, hints);
            response.send({ labels: spec.options ? spec.options.map(o => o.label) : await onnxLabels(spec.model),
                descriptions: spec.options ? Object.fromEntries(spec.options.filter(o => o.description).map(o => [o.label, o.description])) : {} });
        } catch (error) {
            response.status(502).send({ error: String(error.message || error).slice(0, 300) });
        }
    });

    // One message → [{label, score}] best first (its /api/extra/classify).
    router.post('/classify', async (request, response) => {
        const input = String(request.body?.text || '').slice(0, 8000);
        try {
            const spec = specOf(request.body, hints);
            if (!input.trim()) return response.send({ classification: [{ label: spec.options?.some(o => o.label === 'neutral') || !spec.options ? 'neutral' : spec.options[0].label, score: 1 }] });
            if (!spec.model) throw new Error('Pick a model for the classifier');
            const key = JSON.stringify([spec, input]);
            if (cache.has(key)) return response.send({ classification: cache.get(key) });
            const classification = spec.engine === 'onnx' ? await classifyOnnx(spec.model, input, spec.options)
                : spec.engine === 'decision' ? await classifyDecision(spec.model, input, spec.options, spec.instructions)
                    : await classifyLlm(spec.model, input, spec.options, spec.instructions);
            response.send({ classification: remember(key, classification) });
        } catch (error) {
            console.warn('[Rocsy Addons] classify:', error.message);
            response.status(502).send({ error: String(error.message || error).slice(0, 300) });
        }
    });
}
