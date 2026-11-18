// A Rocsy Voice for one SillyTavern character (part of the Rocsy Addons server plugin).
// Two ways in, both ending as an ordinary Rocsy Voice character (category "SillyTavern", the card's picture as
// its portrait) that SillyTavern's TTS then speaks with:
//   - Describe: a llama-swap model reads the card and writes a voice description + a sample line in the character's
//     own words; Qwen3-TTS VoiceDesign speaks it (POST /api/reference/design), the clip becomes the reference.
//   - Recording: an audio file (any length) is cut to 15 s, music removed, cleaned (POST /api/reference/upload →
//     /clean), and transcribed with Whisper.
// Long steps are Rocsy Voice jobs; the page polls /voice/job/:id.

import fs from 'node:fs';
import path from 'node:path';
import express from 'express';

const VOICE = (process.env.ROCSY_VOICE_URL || 'http://127.0.0.1:8950/Voice').replace(/\/$/, '');
const AVATAR = /^[^/\\\0]{1,200}\.png$/i;
const avatarOk = avatar => typeof avatar === 'string' && AVATAR.test(avatar) && !avatar.includes('..') && !avatar.startsWith('.');
const idOk = id => /^[0-9a-f]{32}$/.test(String(id || ''));
let llama = { url: '', key: '' };

async function voiceFetch(pathname, options = {}, timeout = 60000) {
    const response = await fetch(`${VOICE}${pathname}`, { ...options, signal: AbortSignal.timeout(timeout) });
    if (!response.ok) {
        let message = `Rocsy Voice answered ${response.status}`;
        try { const data = await response.json(); message = data.detail || data.message || data.error || message; } catch { /* not JSON */ }
        throw new Error(typeof message === 'string' ? message : JSON.stringify(message).slice(0, 300));
    }
    return response;
}

const cardBlock = (card = {}) => [
    ['Name', card.name], ['Description', card.description], ['Personality', card.personality], ['Scenario', card.scenario],
    ['First message', card.first_mes], ['Example dialogue', card.mes_example], ['Tags', Array.isArray(card.tags) ? card.tags.join(', ') : card.tags],
].filter(([, v]) => String(v || '').trim()).map(([k, v]) => `${k}: ${String(v).replace(/\{\{char\}\}/gi, card.name || 'the character').replace(/\{\{user\}\}/gi, 'you').slice(0, 3000)}`).join('\n\n');

const DESCRIBE_SYSTEM = 'You cast voices for characters. From the character card, reply with exactly two lines and nothing else:\n'
    + 'VOICE: one sentence (20-45 words) describing how they sound for a voice designer: gender, an age range that fits the card (adult when the card doesn’t say), pitch, timbre, pace, accent if any, and attitude (e.g. "A young adult woman with a bright, slightly husky mid-pitched voice, quick and playful, a light British accent, teasing warmth").\n'
    + 'LINE: one thing the character would say, in their own words and style, 15-30 words, spoken dialogue only (no actions, asterisks, emoji or names).';

const errorOf = error => String(error?.message || error).slice(0, 400);

export function initVoiceMaker(router, options = {}) {
    llama = { url: options.llama || '', key: options.key || '' };

    // The card → a voice description and a sample line.
    router.post('/voice/describe', async (request, response) => {
        const { model = '', card = {} } = request.body || {};
        if (!model) return response.status(400).send({ error: 'No chat model: connect SillyTavern to llama-swap first' });
        try {
            const upstream = await fetch(`${llama.url}/chat/completions`, {
                method: 'POST', signal: AbortSignal.timeout(240000),
                headers: { 'Content-Type': 'application/json', ...(llama.key ? { Authorization: `Bearer ${llama.key}` } : {}) },
                body: JSON.stringify({
                    model: String(model), temperature: 0.7, max_tokens: 400, stream: false,
                    chat_template_kwargs: { enable_thinking: false }, reasoning_effort: 'none',
                    messages: [{ role: 'system', content: DESCRIBE_SYSTEM }, { role: 'user', content: `${cardBlock(card)}\n\nCast this character's voice.` }],
                }),
            });
            if (!upstream.ok) throw new Error(`llama-swap answered ${upstream.status}: ${(await upstream.text()).slice(0, 200)}`);
            const data = await upstream.json();
            const text = String(data?.choices?.[0]?.message?.content || '').replace(/<think>[\s\S]*?<\/think>/gi, '').replace(/^[\s\S]*<\/think>/i, '');
            const pick = key => (text.match(new RegExp(`^\\s*\\**${key}\\**\\s*:\\s*(.+)$`, 'im'))?.[1] || '').replace(/^["“]|["”]$/g, '').trim();
            const description = pick('VOICE'), line = pick('LINE');
            if (!description) throw new Error('The chat model didn’t write a voice description');
            response.send({ description: description.slice(0, 1000), line: (line || `Hi, I'm ${card.name || 'here'}. It's good to finally talk to you.`).slice(0, 400) });
        } catch (error) {
            response.status(502).send({ error: errorOf(error) });
        }
    });

    // Speak the line in a designed voice (Qwen3-TTS VoiceDesign): a Voice job whose clip can be saved.
    router.post('/voice/design', async (request, response) => {
        const { description = '', text = '' } = request.body || {};
        if (!String(description).trim() || !String(text).trim()) return response.status(400).send({ error: 'Describe the voice and give it a line to say' });
        try {
            const job = await (await voiceFetch('/api/reference/design', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ engine: 'qwen3_design', description: String(description).slice(0, 1000), text: String(text).slice(0, 400) }),
            })).json();
            response.send(job);
        } catch (error) {
            response.status(502).send({ error: errorOf(error) });
        }
    });

    // A recording (raw audio body): upload, then cut 15 s from ?start=, remove music and clean it up.
    router.post('/voice/recording', express.raw({ type: () => true, limit: '120mb' }), async (request, response) => {
        const body = request.body;
        if (!Buffer.isBuffer(body) || !body.length) return response.status(400).send({ error: 'No audio' });
        const filename = String(request.query.name || 'recording').replace(/[^\w.-]+/g, '_').slice(0, 80) || 'recording';
        try {
            const form = new FormData();
            form.append('audio', new Blob([body], { type: request.get('content-type') || 'application/octet-stream' }), filename);
            const upload = await (await voiceFetch('/api/reference/upload', { method: 'POST', body: form }, 300000)).json();
            const start = Math.max(0, Math.min(Number(request.query.start) || 0, Math.max(0, upload.duration - 3)));
            const job = await (await voiceFetch(`/api/reference/${upload.id}/clean`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ start, length: Math.min(15, Math.max(3, upload.duration - start)), remove_music: 100, noise: 20, normalize: true, focus: true }),
            })).json();
            response.send({ ...job, duration: upload.duration });
        } catch (error) {
            response.status(502).send({ error: errorOf(error) });
        }
    });

    router.post('/voice/transcribe/:clip', async (request, response) => {
        if (!idOk(request.params.clip)) return response.status(400).send({ error: 'Bad clip' });
        try { response.send(await (await voiceFetch(`/api/reference/${request.params.clip}/transcribe`, { method: 'POST' })).json()); } catch (error) { response.status(502).send({ error: errorOf(error) }); }
    });

    router.get('/voice/job/:id', async (request, response) => {
        if (!idOk(request.params.id)) return response.status(400).send({ error: 'Bad job' });
        try { response.send(await (await voiceFetch(`/api/jobs/${request.params.id}`, {}, 15000)).json()); } catch (error) { response.status(502).send({ error: errorOf(error) }); }
    });

    router.get('/voice/clip/:id', async (request, response) => {
        if (!idOk(request.params.id)) return response.status(400).send({ error: 'Bad clip' });
        try {
            const upstream = await voiceFetch(`/api/reference/${request.params.id}/audio`, {}, 30000);
            response.set('Content-Type', 'audio/wav').set('Cache-Control', 'private, no-store').send(Buffer.from(await upstream.arrayBuffer()));
        } catch (error) { response.status(502).send({ error: errorOf(error) }); }
    });

    // Keep the clip as a Rocsy Voice character named after the card (its picture as the portrait).
    router.post('/voice/save', async (request, response) => {
        const { clip = '', name = '', transcript = '', avatar = '', summary = '' } = request.body || {};
        if (!idOk(clip) || !String(name).trim()) return response.status(400).send({ error: 'Bad clip or name' });
        try {
            const state = await (await voiceFetch('/api/state', {}, 8000)).json();
            const taken = new Set((state.voices || []).map(v => String(v.name).toLowerCase()));
            let voiceName = String(name).trim().slice(0, 80);
            for (let n = 2; taken.has(voiceName.toLowerCase()); n++) voiceName = `${String(name).trim().slice(0, 74)} ${n}`;
            const form = new FormData();
            form.append('name', voiceName);
            form.append('transcript', String(transcript).slice(0, 2000));
            form.append('category', 'SillyTavern');
            form.append('summary', String(summary).slice(0, 500));
            form.append('icon', '🎭');
            if (avatarOk(avatar)) {
                const file = path.join(request.user.directories.characters, avatar);
                if (fs.existsSync(file)) form.append('icon_image', new Blob([fs.readFileSync(file)], { type: 'image/png' }), 'portrait.png');
            }
            const data = await (await voiceFetch(`/api/reference/${clip}/save`, { method: 'POST', body: form })).json();
            response.send({ voice: data.voice });
        } catch (error) {
            response.status(502).send({ error: errorOf(error) });
        }
    });
}
