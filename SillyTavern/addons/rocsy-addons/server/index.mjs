// Rocsy Addons: SillyTavern server plugin (installed into SillyTavern's plugins/rocsy, routes at /api/plugins/rocsy).
// SillyTavern runs on the host network, so it reaches Rocsy's Voice service and llama-swap on 127.0.0.1 even when
// the browser is somewhere else. The llama-swap key comes from Rocsy (ROCSY_LLAMA_KEY in compose.yml) and only
// ever goes into SillyTavern's own secret store; it is never sent to the browser.

import fs from 'node:fs';
import path from 'node:path';
import { readSecret, writeSecret, deleteSecret, SECRET_KEYS } from '../../src/endpoints/secrets.js';
import { initImage, exitImage, EXPRESSIONS } from './rimage.mjs';
import { initVoiceMaker } from './rvoice.mjs';
import { initClassifier } from './rclassify.mjs';
import { initGeneration, exitGeneration } from './rgen.mjs';
import { initPush } from './rpush.mjs';
import { initChats } from './rchats.mjs';

const VOICE = (process.env.ROCSY_VOICE_URL || 'http://127.0.0.1:8950/Voice').replace(/\/$/, '');
const LLAMA = (process.env.ROCSY_LLAMA_URL || 'http://127.0.0.1:8400/v1').replace(/\/$/, '');
const KEY = process.env.ROCSY_LLAMA_KEY || '';

export const info = {
    id: 'rocsy',
    name: 'Rocsy Addons',
    description: 'Bridges SillyTavern to Rocsy: Voice engines and voices for TTS, llama-swap as the default connection, and the card Library.',
};

// ----------------------------------------------------------------- library --
// Card search for the Library tab, done here so the browser has no CORS trouble and the Bot Booru token stays on
// the server. Each site's results come back in one shape: {items: [{id, name, creator, tagline, tags, tokens,
// stats, thumb, url, nsfw}], total, more}. Chub cards are downloaded by SillyTavern's own importer
// (/api/content/importURL); Bot Booru cards through /library/card; datacat builds its cards in the browser.

const USER_AGENT = 'SillyTavern Rocsy-Addons';
const BOORU = 'https://botbooru.com';
const CHUB_API = 'https://api.chub.ai';
const BOORU_SECRET = 'rocsy_botbooru_token';
const PAGE = 24;

class SiteError extends Error {
    constructor(message, status = 502) { super(message); this.status = status; }
}

async function siteFetch(url, options = {}, timeout = 20000) {
    let response;
    try {
        response = await fetch(url, { ...options, headers: { 'User-Agent': USER_AGENT, ...(options.headers || {}) }, signal: AbortSignal.timeout(timeout) });
    } catch (error) {
        throw new SiteError(`${new URL(url).host} isn't reachable (${error.cause?.code || error.message})`);
    }
    if (response.ok) return response;
    const text = (await response.text().catch(() => '')).slice(0, 300);
    const host = new URL(url).host;
    if (/not available in your country/i.test(text)) throw new SiteError(`${host} says: “${text.trim()}”`, 451);
    if (response.status === 429) throw new SiteError(`${host} is rate-limiting searches; wait a minute and try again.`, 429);
    if (response.status === 401 || response.status === 403) throw new SiteError(`${host} refused the request (${response.status})${text ? `: ${text}` : ''}`, 403);
    throw new SiteError(`${host} answered ${response.status}${text ? `: ${text}` : ''}`);
}

const clean = value => String(value ?? '').replace(/<[^>]*>/g, ' ').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#0?39;/g, "'").replace(/\s+/g, ' ').trim();

// Site accounts: the sign-in token each site's own web page keeps (Bot Booru "token", Chub "URQL_TOKEN"), pasted
// by the user, checked against the site, kept in SillyTavern's secret store and never sent back to the browser.
const ACCOUNTS = {
    booru: {
        secret: BOORU_SECRET,
        headers: token => ({ Authorization: `Bearer ${token}` }),
        me: `${BOORU}/auth/me`,
        user: data => data?.username || data?.user?.username || '',
    },
    chub: {
        secret: 'rocsy_chub_token',
        headers: token => ({ 'CH-API-KEY': token, samwise: token }),
        me: `${CHUB_API}/api/account`,
        // Chub answers an unknown token with its anonymous placeholder (negative id, "You"): only a positive id counts.
        user: data => (Number(data?.id) > 0 ? data.user_name || data.name || '' : ''),
    },
};

const isAccount = site => Object.hasOwn(ACCOUNTS, site);   // not 'constructor' & co.
function siteToken(request, site) {
    if (!isAccount(site)) return '';
    try { return readSecret(request.user.directories, ACCOUNTS[site].secret) || ''; } catch { return ''; }
}
// A Chub path is "creator/name": no "." or ".." parts (they would walk to other Chub API endpoints with the token).
const chubPath = id => /^[^/\s]+\/[^/\s]+$/.test(id) && !id.split('/').some(part => part === '.' || part === '..');
const booruToken = request => siteToken(request, 'booru');

async function whoAmI(site, token) {
    const account = ACCOUNTS[site];
    const response = await siteFetch(account.me, { headers: { Accept: 'application/json', ...account.headers(token) } });
    const data = await response.json().catch(() => ({}));
    const user = account.user(data);
    if (!user) throw new Error('the site doesn’t know that token');
    return user;
}

const BOORU_SORTS = new Set(['latest', 'downloads', 'favorites', 'views', 'random', 'curated']);

const BOORU_WINDOWS = new Set(['day', 'week', 'month', 'all']);
const BOORU_CURATED = new Set(['recent', 'random']);
const tokenCount = value => { const n = parseInt(String(value ?? ''), 10); return Number.isFinite(n) && n > 0 ? String(n) : ''; };
// A creator ("artist") to search by: a site user name, no path or query characters.
const creatorName = value => String(value || '').trim().replace(/^@/, '').slice(0, 80).replace(/[^\p{L}\p{N}_.\- ]/gu, '');
const tagWords = value => (Array.isArray(value) ? value : String(value || '').split(/[\s,]+/)).map(t => String(t).trim()).filter(Boolean).slice(0, 30);

// Bot Booru's own search: text (qtext), tags (q: space separated, -tag excludes, underscores for spaces), its sorts
// with a day/week/month/all window for downloads/favorites/views, curated order, and a token range.
async function searchBooru({ q, sort, page, nsfw, f }, token) {
    const order = BOORU_SORTS.has(sort) ? sort : 'latest';
    const params = new URLSearchParams({ sort: order, limit: String(PAGE), offset: String((page - 1) * PAGE) });
    if (q) params.set('qtext', q);
    const tags = tagWords(f.tags).map(t => t.toLowerCase().replace(/^#/, ''));
    const creator = creatorName(f.creator);
    if (creator) tags.unshift(creator.toLowerCase().replace(/\s+/g, '_'));   // Bot Booru's creators are Writer tags
    params.set('q', tags.join(' '));
    if (['downloads', 'favorites', 'views'].includes(order)) params.set('time_window', BOORU_WINDOWS.has(f.window) ? f.window : 'all');
    if (order === 'curated') params.set('curated_sort', BOORU_CURATED.has(f.curated) ? f.curated : 'recent');
    if (tokenCount(f.minTokens)) params.set('min_tokens', tokenCount(f.minTokens));
    if (tokenCount(f.maxTokens)) params.set('max_tokens', String(Math.max(200, Number(tokenCount(f.maxTokens)))));
    if (!nsfw) params.set('sfw_only', 'true');
    const response = await siteFetch(`${BOORU}/posts/?${params}`, { headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) } });
    const data = await response.json();
    const posts = Array.isArray(data.posts) ? data.posts : [];
    return {
        total: Number(data.total) || 0,
        more: (page * PAGE) < (Number(data.total) || 0) && sort !== 'random',
        items: posts.map(post => {
            const tagList = Array.isArray(post.tags) ? post.tags : [];
            const shown = tagList.filter(t => t.category !== 'Auto' && t.category !== 'Writer' && t.category !== 'Artist' && t.category !== 'Language').map(t => t.name.replace(/_/g, ' '));
            return {
                id: String(post.id),
                name: clean(post.meta_name || post.character_name) || 'Unnamed',
                creator: tagList.filter(t => t.category === 'Writer').map(t => t.name).join(', '),
                tagline: clean(post.tagline || post.creator_notes_excerpt || post.description_excerpt),
                tags: shown.slice(0, 8),
                tokens: Number(post.token_count) || 0,
                stats: { downloads: post.downloads || 0, favorites: post.favorite_count || 0 },
                thumb: post.filename ? `${BOORU}/images/preview/320/${encodeURIComponent(post.filename)}${post.card_image_revision ? `?v=${encodeURIComponent(post.card_image_revision)}` : ''}` : '',
                url: `${BOORU}/character/${post.id}`,
                nsfw: !tagList.some(t => t.name === 'sfw'),
            };
        }),
    };
}

// ------------------------------------------------------------- card details --
// One card's full data for the Library's details view, in one shape for every site.

const text = value => String(value ?? '').replace(/\r\n/g, '\n').trim();
const lorebookOf = book => {
    const entries = Array.isArray(book?.entries) ? book.entries : book?.entries && typeof book.entries === 'object' ? Object.values(book.entries) : [];
    return entries.length ? {
        name: text(book.name), entries: entries.slice(0, 200).map(e => ({
            keys: (Array.isArray(e.keys) ? e.keys : Array.isArray(e.key) ? e.key : String(e.keys || e.key || '').split(',')).map(k => String(k).trim()).filter(Boolean),
            comment: text(e.comment || e.name), content: text(e.content).slice(0, 1500),
        })),
    } : null;
};

// Chub keeps comments as project "ratings": one flat list, replies point at their parent (parentId).
// Chub sends the whole list at once, so it is cached a few minutes and paged here, newest thread first.
const chubCommentCache = new Map();
async function chubComments(projectId) {
    const hit = chubCommentCache.get(projectId);
    if (hit && Date.now() - hit.at < 5 * 60 * 1000) return hit.list;
    const data = await (await siteFetch(`${CHUB_API}/api/project/${projectId}/ratings`, { headers: { Accept: 'application/json' } })).json();
    const rows = Array.isArray(data?.ratings) ? data.ratings : [];
    const one = r => ({ author: text(r.username) || 'Anonymous', text: text(r.comment).slice(0, 4000), at: r.commentAt || r.createdAt || '',
        rating: Number(r.rating) || 0, score: 0 });
    const replies = new Map();
    for (const r of rows) if (r.parentId != null && String(r.comment || '').trim()) {
        if (!replies.has(r.parentId)) replies.set(r.parentId, []);
        replies.get(r.parentId).push(r);
    }
    const byTime = (a, b) => Date.parse(b.commentAt || b.createdAt || 0) - Date.parse(a.commentAt || a.createdAt || 0);
    const list = rows.filter(r => r.parentId == null && (String(r.comment || '').trim() || replies.has(r.id))).sort(byTime)
        .map(r => ({ ...one(r), replies: (replies.get(r.id) || []).sort((a, b) => -byTime(a, b)).map(one) }));
    chubCommentCache.set(projectId, { at: Date.now(), list });
    if (chubCommentCache.size > 50) chubCommentCache.delete(chubCommentCache.keys().next().value);
    return list;
}

async function chubDetails(id, token) {
    const response = await siteFetch(`${CHUB_API}/api/characters/${id.split('/').map(encodeURIComponent).join('/')}?full=true`,
        { headers: { Accept: 'application/json', ...(token ? ACCOUNTS.chub.headers(token) : {}) } });
    const node = (await response.json())?.node;
    if (!node?.fullPath) throw new SiteError('Chub has no such character (it may be private or deleted)', 404);
    const def = node.definition || {};
    return {
        name: text(node.name || def.name), creator: node.fullPath.split('/')[0], tagline: clean(node.tagline),
        image: node.max_res_url || node.avatar_url || '', url: `https://chub.ai/characters/${node.fullPath}`,
        creatorNotes: text(def.description || node.description), description: text(def.personality), personality: text(def.tavern_personality),
        scenario: text(def.scenario), firstMessage: text(def.first_message), alternateGreetings: (def.alternate_greetings || []).map(text).filter(Boolean),
        exampleDialogs: text(def.example_dialogs).replace(/^<START>$/i, ''), systemPrompt: text(def.system_prompt), postHistory: text(def.post_history_instructions),
        tags: (node.topics || []).filter(t => !/^(nsfw|sfw)$/i.test(t)), tokens: Number(node.nTokens) || 0, nsfw: !!node.nsfw_image,
        stats: [['Stars', node.starCount, 'fa-star'], ['Favorites', node.n_favorites, 'fa-heart'], ['Chats', node.nChats, 'fa-comments'],
            ['Messages', node.nMessages, 'fa-message'], ['Forks', node.forksCount, 'fa-code-fork'],
            ['Rating', node.ratingCount ? `${Number(node.rating).toFixed(1)}/5` : null, 'fa-star-half-stroke', node.ratingCount ? `${node.ratingCount} ratings` : ''],
            ['Public chats', node.n_public_chats, 'fa-users']].filter(([, v]) => v !== null && v !== undefined),
        comments: { supported: true, id: String(node.id), url: `https://chub.ai/characters/${node.fullPath}` },
        created: node.createdAt || '', updated: node.lastActivityAt || '', lorebook: lorebookOf(def.embedded_lorebook),
    };
}

async function booruDetails(id, token) {
    const response = await siteFetch(`${BOORU}/post/${id}`, { headers: { Accept: 'application/json', ...(token ? ACCOUNTS.booru.headers(token) : {}) } });
    const p = await response.json();
    const tags = Array.isArray(p.tags) ? p.tags : [];
    let book = null;
    try { book = typeof p.lorebook_json === 'string' ? JSON.parse(p.lorebook_json) : p.lorebook_json; } catch { /* not JSON */ }
    return {
        name: text(p.meta_name || p.character_name), creator: tags.filter(t => t.category === 'Writer').map(t => t.name).join(', ') || text(p.uploader_name),
        tagline: clean(p.tagline), image: p.filename ? `${BOORU}/images/preview/640/${encodeURIComponent(p.filename)}${p.card_image_revision ? `?v=${p.card_image_revision}` : ''}` : '',
        url: `${BOORU}/character/${p.id}`,
        creatorNotes: text(p.creator_notes_display || p.creator_notes), description: text(p.description), personality: text(p.personality),
        scenario: text(p.scenario), firstMessage: text(p.first_mes), alternateGreetings: (p.alternate_greetings || []).map(text).filter(Boolean),
        exampleDialogs: text(p.mes_example), systemPrompt: text(p.system_prompt), postHistory: text(p.post_history_instructions),
        tags: tags.filter(t => !['Auto', 'Writer', 'Artist', 'Language'].includes(t.category)).map(t => t.name.replace(/_/g, ' ')),
        tokens: 0, nsfw: !tags.some(t => t.name === 'sfw'),
        stats: [['Views', p.views, 'fa-eye'], ['Downloads', p.downloads, 'fa-download'], ['Forks', p.fork_count, 'fa-code-fork'],
            ['Revisions', p.revision_count, 'fa-clock-rotate-left'], ['Origin', p.origin, 'fa-seedling'],
            ['Slop score', typeof p.slop_score === 'number' ? p.slop_score.toFixed(1) : null, 'fa-gauge', 'Lower is less clichéd writing']].filter(([, v]) => v !== null && v !== undefined && v !== ''),
        comments: { supported: true, url: `${BOORU}/character/${p.id}` },
        created: p.created_at || '', updated: '', lorebook: lorebookOf(book?.data || book),
    };
}

// Chub's own sort list (its web app's), plus its search filters: tags to include / exclude, a token range, age,
// and "has lorebook / alternate greetings / example dialogues / images / expressions".
const CHUB_SORT_LIST = new Set(['default', 'trending_downloads', 'n_favorites', 'star_count', 'download_count', 'rating', 'rating_count',
    'last_activity_at', 'created_at', 'name', 'n_tokens', 'random']);
const CHUB_FLAGS = { lore: 'require_lore', greetings: 'require_alternate_greetings', examples: 'require_example_dialogues', images: 'require_images', expressions: 'require_expressions' };
const CHUB_SORTS = { popular: 'download_count', stars: 'star_count', latest: 'created_at', updated: 'last_activity_at', trending: 'trending_downloads' };

async function searchChub({ q, sort, page, nsfw, f }, token = '', namespace = 'characters') {
    const lore = namespace === 'lorebooks';
    const include = tagWords(f.tags), exclude = [...new Set(tagWords(f.exclude))];
    let order = CHUB_SORT_LIST.has(sort) ? sort : CHUB_SORTS[sort] || 'default';
    if ((order === 'default' || order === 'trending_downloads') && (q || include.length || exclude.length)) order = 'star_count';   // as Chub does
    const params = new URLSearchParams({
        search: q, first: String(PAGE), page: String(page), sort: order, asc: f.asc ? 'true' : 'false',
        namespace, nsfw: String(!!nsfw), nsfl: String(!!nsfw), include_forks: 'true', venus: 'false',
        min_tokens: tokenCount(f.minTokens) || (lore ? '0' : '50'),
    });
    if (include.length) params.set('topics', include.join(','));
    if (creatorName(f.creator)) params.set('username', creatorName(f.creator));
    if (exclude.length) params.set('excludetopics', exclude.join(','));
    if (tokenCount(f.maxTokens)) params.set('max_tokens', tokenCount(f.maxTokens));
    if (tokenCount(f.days)) params.set('max_days_ago', tokenCount(f.days));
    if (!lore) for (const [key, flag] of Object.entries(CHUB_FLAGS)) if (f[key]) params.set(flag, 'true');
    const response = await siteFetch(`${CHUB_API}/search?${params}`, { headers: { Accept: 'application/json', ...(token ? ACCOUNTS.chub.headers(token) : {}) } });
    const data = await response.json();
    const nodes = data?.data?.nodes || data?.nodes || [];
    const total = Number(data?.data?.count ?? data?.count) || 0;
    return {
        total,
        more: nodes.length >= PAGE,
        items: nodes.filter(node => node?.fullPath).map(node => lore ? chubLoreItem(node) : ({
            id: String(node.fullPath),
            name: clean(node.name) || node.fullPath.split('/').pop(),
            creator: node.fullPath.split('/')[0],
            tagline: clean(node.tagline || node.description),
            tags: (node.topics || []).filter(t => !/^(nsfw|sfw)$/i.test(t)).slice(0, 8),
            tokens: Number(node.nTokens) || 0,
            stats: { stars: node.starCount ?? 0, chats: node.nChats ?? 0 },
            thumb: node.avatar_url || `https://avatars.charhub.io/avatars/${node.fullPath}/avatar.webp`,
            url: `https://chub.ai/characters/${node.fullPath}`,
            nsfw: !!node.nsfw_image || (node.topics || []).some(t => /^nsfw$/i.test(t)),
        })),
    };
}

// ---------------------------------------------------------------- lorebooks --
// Lorebooks (SillyTavern's World Info) from Chub (namespace "lorebooks"; SillyTavern's own URL importer downloads
// them) and Bot Booru (/api/lorebooks; its download.json is already SillyTavern World Info). Same result shape as
// the cards, so the Library's grid, details and bookmarks work for them too.

const chubLorePath = node => String(node.fullPath).replace(/^lorebooks\//, '');
function chubLoreItem(node) {
    const id = chubLorePath(node);
    return {
        id, name: clean(node.name) || id.split('/').pop(), creator: id.split('/')[0],
        tagline: clean(node.tagline || node.description),
        tags: (node.topics || []).filter(t => !/^(nsfw|sfw|lorebook)$/i.test(t)).slice(0, 8),
        tokens: Number(node.nTokens) || 0,
        stats: { stars: node.starCount ?? 0 },
        thumb: node.avatar_url || `https://avatars.charhub.io/avatars/lorebooks/${id}/avatar.webp`,
        url: `https://chub.ai/lorebooks/${id}`,
        nsfw: !!node.nsfw_image || (node.topics || []).some(t => /^nsfw$/i.test(t)),
        lore: true,
    };
}

async function chubLoreDetails(id, token) {
    const response = await siteFetch(`${CHUB_API}/api/lorebooks/${id.split('/').map(encodeURIComponent).join('/')}?full=true`,
        { headers: { Accept: 'application/json', ...(token ? ACCOUNTS.chub.headers(token) : {}) } });
    const node = (await response.json())?.node;
    if (!node?.fullPath) throw new SiteError('Chub has no such lorebook (it may be private or deleted)', 404);
    const def = node.definition || {};
    const book = lorebookOf(def.embedded_lorebook);
    return {
        lore: true, name: text(node.name || def.name), creator: chubLorePath(node).split('/')[0], tagline: clean(node.tagline),
        image: node.avatar_url || '', url: `https://chub.ai/lorebooks/${chubLorePath(node)}`,
        creatorNotes: text(def.description || node.description), alternateGreetings: [],
        tags: (node.topics || []).filter(t => !/^(nsfw|sfw|lorebook)$/i.test(t)), tokens: Number(node.nTokens) || 0, nsfw: !!node.nsfw_image,
        stats: [['Entries', entryTotal(def.embedded_lorebook), 'fa-list'], ['Stars', node.starCount, 'fa-star'], ['Favorites', node.n_favorites, 'fa-heart'],
            ['Forks', node.forksCount, 'fa-code-fork'],
            ['Rating', node.ratingCount ? `${Number(node.rating).toFixed(1)}/5` : null, 'fa-star-half-stroke', node.ratingCount ? `${node.ratingCount} ratings` : '']]
            .filter(([, v]) => v !== null && v !== undefined),
        comments: { supported: true, id: String(node.id), url: `https://chub.ai/lorebooks/${chubLorePath(node)}` },
        created: node.createdAt || '', updated: node.lastActivityAt || '', lorebook: book,
    };
}

const entryTotal = book => Array.isArray(book?.entries) ? book.entries.length : book?.entries && typeof book.entries === 'object' ? Object.keys(book.entries).length : 0;
const BOORU_LORE_SORTS = new Set(['latest', 'downloads', 'favorites', 'views']);
const booruCover = file => file ? `${BOORU}/images/${encodeURIComponent(file)}` : '';

// Bot Booru's lorebook list can't be filtered by uploader: for a creator search the server reads the list
// (100 at a time, up to BOORU_LORE_SCAN) and keeps that uploader's books, then pages through those.
const BOORU_LORE_SCAN = 2000;
async function searchBooruLore({ q, sort, page, nsfw, f = {} }, token) {
    const creator = creatorName(f.creator).toLowerCase();
    const fetchList = async (limit, offset) => {
        const params = new URLSearchParams({ limit: String(limit), offset: String(offset), sort: BOORU_LORE_SORTS.has(sort) ? sort : 'latest' });
        if (q) params.set('q', q);
        if (!nsfw) params.set('sfw_only', 'true');
        const response = await siteFetch(`${BOORU}/api/lorebooks?${params}`, { headers: { Accept: 'application/json', ...(token ? ACCOUNTS.booru.headers(token) : {}) } });
        const data = await response.json();
        return { total: Number(data?.total) || 0, items: Array.isArray(data?.items) ? data.items : [] };
    };
    let total, more, items;
    if (creator) {
        const mine = [];
        for (let offset = 0; offset < BOORU_LORE_SCAN; offset += 100) {
            const chunk = await fetchList(100, offset);
            mine.push(...chunk.items.filter(b => String(b?.uploader_username || '').toLowerCase() === creator));
            if (chunk.items.length < 100 || offset + 100 >= chunk.total) break;
        }
        total = mine.length;
        items = mine.slice((page - 1) * PAGE, page * PAGE);
        more = page * PAGE < total;
    } else {
        const data = await fetchList(PAGE, (page - 1) * PAGE);
        ({ total, items } = data);
        more = items.length >= PAGE;
    }
    return {
        total,
        more,
        items: items.filter(b => b?.number).map(b => ({
            id: String(b.number), name: clean(b.title) || `Lorebook ${b.number}`, creator: clean(b.uploader_username),
            tagline: clean(b.tagline || b.first_entry_snippet),
            tags: [],
            tokens: Number(b.token_estimate) || 0,
            stats: { entries: b.entry_count || 0, downloads: b.downloads || 0, favorites: b.favorite_count || 0 },
            thumb: booruCover(b.cover_image_filename),
            url: `${BOORU}/lorebook/${b.number}`,
            nsfw: b.content_rating && b.content_rating !== 'sfw',
            lore: true,
        })),
    };
}

async function booruLoreDetails(number, token) {
    const response = await siteFetch(`${BOORU}/api/lorebooks/${number}`, { headers: { Accept: 'application/json', ...(token ? ACCOUNTS.booru.headers(token) : {}) } });
    const b = await response.json();
    if (!b?.number) throw new SiteError('Bot Booru has no such lorebook', 404);
    const entries = Array.isArray(b.entries) ? b.entries : [];
    return {
        lore: true, name: text(b.title), creator: text(b.uploader_username), tagline: clean(b.uploader_tagline),
        image: booruCover(b.cover_image_filename), url: `${BOORU}/lorebook/${b.number}`,
        creatorNotes: text(b.description || b.uploader_tagline), alternateGreetings: [], tags: [], tokens: 0,
        nsfw: b.content_rating && b.content_rating !== 'sfw',
        stats: [['Entries', b.entry_count ?? entries.length, 'fa-list'], ['Views', b.views, 'fa-eye'], ['Downloads', b.downloads, 'fa-download'],
            ['Favorites', b.favorite_count, 'fa-heart'], ['Scan depth', b.scan_depth, 'fa-magnifying-glass'], ['Token budget', b.token_budget, 'fa-coins']]
            .filter(([, v]) => v !== null && v !== undefined && v !== ''),
        comments: { supported: false, url: `${BOORU}/lorebook/${b.number}` },
        created: b.created_at || '', updated: '', lorebook: lorebookOf({ name: b.title, entries }),
        source: b.sauce || '',
    };
}

async function getJson(url, options = {}, timeout = 8000) {
    const response = await fetch(url, { ...options, signal: AbortSignal.timeout(timeout) });
    if (!response.ok) throw new Error(`${url.replace(/\?.*/, '')} answered ${response.status}`);
    return response.json();
}

async function llamaModels() {
    const data = await getJson(`${LLAMA}/models`, { headers: KEY ? { Authorization: `Bearer ${KEY}` } : {} });
    return (Array.isArray(data?.data) ? data.data : []).map(model => ({ id: String(model.id), name: String(model.name || model.id) }));
}

// --------------------------------------------------------------- model loading --
// Every open SillyTavern tab (any device) sends a heartbeat: POST /presence {tab, model, want, mode}. `want` is
// worked out in the page from the Model loading setting (Rocsy Addons → Connection): whole site, character chat
// only, active chatting with a timeout, or always. A model a tab wanted is preloaded when a tab starts wanting it,
// and unloaded (that model only, POST /api/models/unload/<model>) once no tab has wanted it for UNLOAD_GRACE, so
// llama-swap frees the GPU when absolutely no one is on SillyTavern. A model SillyTavern never asked for (the Master
// Agent's, say) is never touched, and a model that is busy generating is left until it's done.
const LLAMA_ROOT = LLAMA.replace(/\/v1$/, '');
const TAB_EXPIRY = 150_000;        // background tabs are throttled to one timer a minute; frozen phone tabs stop
const UNLOAD_GRACE = 30_000;       // a reload or a quick tab switch doesn't unload
const LOAD_MODES = new Set(['site', 'chat', 'active', 'always']);
const presence = { tabs: new Map(), claimed: new Map(), mode: 'site', timer: null, log: [] };   // claimed: model -> idle-since (0 = wanted)
const llamaHeaders = () => (KEY ? { Authorization: `Bearer ${KEY}` } : {});
const noteModel = line => { presence.log.unshift({ at: Date.now(), line }); presence.log.length = Math.min(presence.log.length, 8); console.log(`[Rocsy Addons] ${line}`); };

async function runningModels() {
    const data = await getJson(`${LLAMA_ROOT}/running`, { headers: llamaHeaders() }, 5000);
    return (Array.isArray(data?.running) ? data.running : []).map(r => ({ model: String(r.model), state: String(r.state || '') }));
}

async function modelBusy(model) {
    try {
        const slots = await getJson(`${LLAMA_ROOT}/upstream/${encodeURIComponent(model)}/slots`, { headers: llamaHeaders() }, 4000);
        return Array.isArray(slots) && slots.some(slot => slot?.is_processing);
    } catch {
        return false;   // no /slots (another engine): llama-swap itself waits for running requests on unload
    }
}

function preloadModel(model) {
    runningModels().then(running => {
        if (running.some(r => r.model === model)) return;
        noteModel(`loading ${model} (someone is on SillyTavern)`);
        // /upstream/<model>/… starts the model in llama-swap without sending it a prompt.
        return fetch(`${LLAMA_ROOT}/upstream/${encodeURIComponent(model)}/health`, { headers: llamaHeaders(), signal: AbortSignal.timeout(600_000) });
    }).catch(error => console.warn('[Rocsy Addons] preload', model, error.message));
}

const liveTabs = () => {
    const now = Date.now();
    for (const [id, tab] of presence.tabs) if (now - tab.seen > TAB_EXPIRY) presence.tabs.delete(id);
    return [...presence.tabs.values()];
};

async function presenceTick() {
    const tabs = liveTabs();
    const now = Date.now();
    for (const [model, idleSince] of presence.claimed) {
        const wanted = presence.mode === 'always' || tabs.some(tab => tab.want && tab.model === model);
        if (wanted) { presence.claimed.set(model, 0); continue; }
        if (!idleSince) { presence.claimed.set(model, now); continue; }
        if (now - idleSince < UNLOAD_GRACE) continue;
        try {
            const state = (await runningModels()).find(r => r.model === model);
            if (!state) { presence.claimed.delete(model); continue; }        // already gone (llama-swap TTL, a swap)
            if (state.state !== 'ready' || await modelBusy(model)) continue; // loading or generating: next tick
            const response = await fetch(`${LLAMA_ROOT}/api/models/unload/${encodeURIComponent(model)}`,
                { method: 'POST', headers: llamaHeaders(), signal: AbortSignal.timeout(330_000) });
            if (!response.ok && response.status !== 404) throw new Error(`llama-swap answered ${response.status}`);
            presence.claimed.delete(model);
            noteModel(`unloaded ${model} (no one needs it on SillyTavern)`);
        } catch (error) {
            console.warn('[Rocsy Addons] unload', model, error.message);
        }
    }
}

function initModelPresence(router) {
    router.post('/presence', (request, response) => {
        const { tab, model, want, mode } = request.body || {};
        if (typeof tab !== 'string' || !/^[A-Za-z0-9_-]{6,64}$/.test(tab)) return response.status(400).send({ error: 'Expected tab' });
        if (LOAD_MODES.has(mode)) presence.mode = mode;
        const id = typeof model === 'string' && /^[^\0/\\]{1,200}$/.test(model) ? model : '';
        const before = presence.tabs.get(tab);
        const wants = !!(want && id);
        presence.tabs.set(tab, { model: id, want: wants, seen: Date.now() });
        if (wants) {
            const startedWanting = !(before?.want && before.model === id);
            presence.claimed.set(id, 0);
            if (startedWanting) preloadModel(id);
        }
        response.send({ ok: true });
    });
    router.post('/presence/leave', (request, response) => {
        presence.tabs.delete(String(request.body?.tab || ''));
        response.send({ ok: true });
        setTimeout(() => presenceTick().catch(() => {}), 0);
    });
    router.get('/presence', async (_request, response) => {
        const tabs = liveTabs();
        let running = [];
        try { running = await runningModels(); } catch { /* llama-swap down */ }
        const now = Date.now();
        response.send({
            mode: presence.mode, tabs: tabs.length, wanting: tabs.filter(t => t.want).length, running,
            claimed: [...presence.claimed].map(([model, idleSince]) => ({ model, unloadIn: idleSince ? Math.max(0, Math.round((UNLOAD_GRACE - (now - idleSince)) / 1000)) : null })),
            grace: UNLOAD_GRACE / 1000, log: presence.log,
        });
    });
    presence.timer = setInterval(() => presenceTick().catch(error => console.warn('[Rocsy Addons] presence', error.message)), 10_000);
}

// ------------------------------------------------------------ intro voice cache --
// Spoken greetings (a character's first message and its alternate greetings) are kept in
// <user data>/user/rocsy-tts-cache/<character avatar>/<sha256>.mp3, so they are generated once. A character's
// folder goes when the character is deleted (the page asks), and any folder whose character card is gone is
// swept on start and whenever a line is added.
const AVATAR = /^[^/\\\0]{1,200}\.png$/i;
const LINE_KEY = /^[0-9a-f]{64}$/;
const cacheRoot = directories => path.resolve(directories.user, 'rocsy-tts-cache');
const avatarOk = avatar => typeof avatar === 'string' && AVATAR.test(avatar) && !avatar.includes('..') && !avatar.startsWith('.');

function sweepIntroCache(directories) {
    const root = cacheRoot(directories);
    let folders = [];
    try { folders = fs.readdirSync(root); } catch { return; }
    for (const name of folders) {
        if (avatarOk(name) && fs.existsSync(path.join(directories.characters, name))) continue;
        fs.rmSync(path.join(root, name), { recursive: true, force: true });
    }
}

function introCacheStats(directories) {
    let lines = 0, bytes = 0, characters = 0;
    try {
        for (const name of fs.readdirSync(cacheRoot(directories))) {
            const files = fs.readdirSync(path.join(cacheRoot(directories), name)).filter(file => file.endsWith('.mp3'));
            if (files.length) characters++;
            for (const file of files) { lines++; bytes += fs.statSync(path.join(cacheRoot(directories), name, file)).size; }
        }
    } catch { /* no cache yet */ }
    return { lines, bytes, characters };
}

function initIntroCache(router) {
    router.get('/intro-cache', (request, response) => {
        const { avatar, key } = request.query;
        if (!key) return response.send(introCacheStats(request.user.directories));
        if (!avatarOk(avatar) || !LINE_KEY.test(String(key))) return response.status(400).send({ error: 'bad avatar or key' });
        const file = path.join(cacheRoot(request.user.directories), avatar, `${key}.mp3`);
        if (!fs.existsSync(file)) return response.status(204).end();
        response.setHeader('Content-Type', 'audio/mpeg');
        response.setHeader('Cache-Control', 'no-store');
        return response.sendFile(file);
    });

    router.post('/intro-cache', (request, response) => {
        const { avatar, key, audio } = request.body || {};
        if (!avatarOk(avatar) || !LINE_KEY.test(String(key)) || typeof audio !== 'string') return response.status(400).send({ error: 'bad avatar, key or audio' });
        const directories = request.user.directories;
        if (!fs.existsSync(path.join(directories.characters, avatar))) return response.status(404).send({ error: 'no such character' });
        const data = Buffer.from(audio, 'base64');
        if (!data.length || data.length > 40 * 1024 * 1024) return response.status(400).send({ error: 'empty or too large' });
        const folder = path.join(cacheRoot(directories), avatar);
        fs.mkdirSync(folder, { recursive: true });
        const file = path.join(folder, `${key}.mp3`);
        fs.writeFileSync(`${file}.part`, data);
        fs.renameSync(`${file}.part`, file);
        sweepIntroCache(directories);
        return response.send({ ok: true });
    });

    // ?avatar=X: that character's lines; no avatar: the whole cache.
    router.delete('/intro-cache', (request, response) => {
        const { avatar } = request.query;
        const directories = request.user.directories;
        if (avatar !== undefined && !avatarOk(avatar)) return response.status(400).send({ error: 'bad avatar' });
        fs.rmSync(avatar ? path.join(cacheRoot(directories), avatar) : cacheRoot(directories), { recursive: true, force: true });
        return response.send({ ok: true, ...introCacheStats(directories) });
    });
}

export async function init(router) {
    initIntroCache(router);
    initModelPresence(router);
    initImage(router, { llama: LLAMA, key: KEY });          // Rocsy Image: pictures, expressions, A1111 bridge
    initVoiceMaker(router, { llama: LLAMA, key: KEY });     // a Rocsy Voice per character
    initClassifier(router, { llama: LLAMA, key: KEY, hints: EXPRESSIONS });
    initPush(router);                                     // "reply ready" notifications (rpush.mjs)
    initChats(router);                                    // chats page: all chats, branches, backups, stats (rchats.mjs)
    initGeneration(router);                               // replies that survive a dropped connection (rgen.mjs)   // expression classifiers (ONNX, llama-swap, decision models)
    // Rocsy home page: the SillyTavern card's backdrop (plugin.json home.background_url). Every character picture,
    // under the plugin's Rocsy path (?prefix=/SillyTavern); the home page shows one at random per visit.
    router.get('/home-art', (request, response) => {
        const prefix = String(request.query.prefix || '');
        if (!/^\/[A-Za-z0-9_-]{1,60}$/.test(prefix)) return response.status(400).send({ error: 'prefix must be like /SillyTavern' });
        let files = [];
        try { files = fs.readdirSync(request.user.directories.characters); } catch { /* no characters folder yet */ }
        const pictures = files.filter(name => /\.png$/i.test(name) && name !== 'default_Assistant.png' && !name.startsWith('.'))
            .sort().map(name => `${prefix}/characters/${encodeURIComponent(name)}`);
        response.send({ pictures, slideshow: false });
    });
    router.get('/status', (_request, response) => response.send({ ok: true, voice: VOICE, llama: LLAMA, key: !!KEY }));

    // Voice engines (ready or not) and saved voices, for the Voice tab.
    router.get('/voice', async (_request, response) => {
        try {
            const state = await getJson(`${VOICE}/api/state`);
            response.send({
                url: `${VOICE}/v1/audio/speech`,
                engines: (state.engines || []).map(e => ({ id: e.id, name: e.name, ready: !!e.ready, detail: e.detail || '' })),
                voices: (state.voices || []).map(v => ({ id: v.id, name: v.name })),
            });
        } catch (error) {
            response.status(502).send({ error: `Rocsy Voice isn't reachable (${error.message})` });
        }
    });

    // One short line through Voice, returned as audio: the Voice tab's "Test".
    router.post('/voice/test', async (request, response) => {
        const { voice, engine, text } = request.body || {};
        if (!voice || !engine) return response.status(400).send({ error: 'Pick an engine and a voice first' });
        try {
            const upstream = await fetch(`${VOICE}/v1/audio/speech`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: AbortSignal.timeout(900_000),
                body: JSON.stringify({ input: String(text || 'Hi! This is how I sound in Rocsy.').slice(0, 300), voice, model: engine }),
            });
            if (!upstream.ok) return response.status(upstream.status).send({ error: (await upstream.text()).slice(0, 300) });
            response.set('Content-Type', upstream.headers.get('content-type') || 'audio/wav');
            response.send(Buffer.from(await upstream.arrayBuffer()));
        } catch (error) {
            response.status(502).send({ error: `Rocsy Voice didn't answer (${error.message})` });
        }
    });

    router.get('/llama', async (_request, response) => {
        try {
            response.send({ url: LLAMA, models: await llamaModels(), key: !!KEY });
        } catch (error) {
            response.status(502).send({ error: `llama-swap isn't reachable (${error.message})`, url: LLAMA, key: !!KEY });
        }
    });

    // Make sure SillyTavern has a key for llama-swap (Custom OpenAI-compatible): Rocsy's, unless one is set.
    router.post('/llama/connect', async (request, response) => {
        try {
            const directories = request.user.directories;
            const force = !!request.body?.force;
            let wrote = false;
            if (KEY && (force || !readSecret(directories, SECRET_KEYS.CUSTOM))) {
                writeSecret(directories, SECRET_KEYS.CUSTOM, KEY);
                wrote = true;
            }
            let models = [];
            let error = '';
            try { models = await llamaModels(); } catch (e) { error = e.message; }
            response.send({ url: LLAMA, models, wrote, key: !!KEY, error });
        } catch (error) {
            response.status(500).send({ error: error.message });
        }
    });

    // ---------------------------------------------------------- chat names --
    // The Master Agent's session naming, for SillyTavern chats: a short, specific 2-6 word name from the first
    // messages, asked of llama-swap directly (thinking off, a few tokens) so it never shows up in the chat.
    router.post('/name', async (request, response) => {
        const { model, character, messages } = request.body || {};
        if (!model || !Array.isArray(messages) || !messages.length) return response.status(400).send({ error: 'Expected model and messages' });
        const transcript = messages.slice(0, 12).map(m => `${m.user ? 'User' : (String(m.name || character || 'Character'))}: ${String(m.text || '').replace(/\s+/g, ' ').slice(0, 700)}`).join('\n');
        const body = {
            model: String(model), temperature: 0.3, max_tokens: 400, stream: false,
            chat_template_kwargs: { enable_thinking: false }, reasoning_effort: 'none',
            messages: [
                { role: 'system', content: 'You name chat sessions. Reply with only the name: a short, specific name of 2-6 words that says what this chat is about (the situation, place or goal), e.g. Neon rooftop chase. No quotes, emoji, character-name-only titles or trailing punctuation.' },
                { role: 'user', content: `Chat with ${character || 'a character'}:\n${transcript}\n\nName this chat.` },
            ],
        };
        try {
            const upstream = await fetch(`${LLAMA}/chat/completions`, {
                method: 'POST', signal: AbortSignal.timeout(180000),
                headers: { 'Content-Type': 'application/json', ...(KEY ? { Authorization: `Bearer ${KEY}` } : {}) },
                body: JSON.stringify(body),
            });
            if (!upstream.ok) throw new Error(`llama-swap answered ${upstream.status}: ${(await upstream.text()).slice(0, 200)}`);
            const data = await upstream.json();
            const text = String(data?.choices?.[0]?.message?.content || '').replace(/<think>[\s\S]*?<\/think>/gi, '').replace(/^[\s\S]*<\/think>/i, '');
            const line = text.split('\n').map(x => x.trim()).find(Boolean) || '';
            response.send({ title: line });
        } catch (error) {
            response.status(502).send({ error: error.message });
        }
    });

    // ------------------------------------------------------------- autotag --
    // Reads a character card (and its picture, when the model can see) and suggests tags in AO3's style (rating,
    // warnings, category, fandoms, relationships, characters, freeform tropes) and Danbooru's (lowercase_with_
    // underscores: count, looks, clothing, species, setting). A model without vision is asked again without the
    // picture. Thinking is off; the answer is held to a JSON schema.
    // How many requests the model serves at once (llama-server --parallel), so Autotag can tag that many cards
    // together. Asking loads the model, which Autotag is about to use anyway.
    router.get('/autotag/slots', async (request, response) => {
        const model = String(request.query.model || '');
        try {
            const slots = await getJson(`${LLAMA_ROOT}/upstream/${encodeURIComponent(model)}/slots`, { headers: llamaHeaders() }, 240000);
            response.send({ slots: Array.isArray(slots) ? slots.length : 1, busy: Array.isArray(slots) ? slots.filter(x => x.is_processing).length : 0 });
        } catch (error) {
            response.send({ slots: 1, busy: 0, error: error.message });
        }
    });

    router.post('/autotag', async (request, response) => {
        const { model, card = {}, image = '', ao3 = true, danbooru = true, maxAo3 = 12, maxDanbooru = 8 } = request.body || {};
        if (!model || !card?.name) return response.status(400).send({ error: 'Expected model and card' });
        const cut = (value, n) => String(value || '').replace(/\r/g, '').trim().slice(0, n);
        const fields = [
            ['Name', cut(card.name, 200)], ['Creator’s notes', cut(card.creator_notes, 3000)], ['Description', cut(card.description, 9000)],
            ['Personality', cut(card.personality, 2000)], ['Scenario', cut(card.scenario, 3000)], ['First message', cut(card.first_mes, 2500)],
            ['Alternate greetings', (Array.isArray(card.alternate_greetings) ? card.alternate_greetings : []).slice(0, 3).map((g, i) => `#${i + 2}: ${cut(g, 700)}`).join('\n\n')],
            ['Example dialogue', cut(card.mes_example, 1200)], ['System prompt', cut(card.system_prompt, 1000)],
            ['Lorebook entries', cut(card.lorebook, 1500)], ['Tags already on the card', cut((card.tags || []).join(', '), 800)],
        ].filter(([, v]) => v);
        const text = fields.map(([k, v]) => `## ${k}\n${v}`).join('\n\n');
        // A strict grammar instead of a JSON schema: fixed key order, no free whitespace (a schema lets the model
        // loop on blank space until max_tokens) and a cap on every list, so the answer is short and always readable.
        const nAo3 = Math.min(30, Math.max(1, Number(maxAo3) || 12)), nBooru = Math.min(12, Math.max(1, Number(maxDanbooru) || 8));
        const listRule = (item, n) => `"[" (${item} (sep ${item}){0,${n - 1}})? "]"`;
        const keys = [
            ['content', 'content'], ['dead_dove', 'bool'], ['rating', 'rating'], ['warnings', listRule('warning', 4)], ['category', listRule('category', 2)], ['fandoms', listRule('str', 3)],
            ['relationships', listRule('str', 3)], ['characters', listRule('str', 5)], ['freeform', ao3 ? listRule('str', nAo3) : '"[]"'],
            ['danbooru', danbooru ? listRule('tag', nBooru) : '"[]"'],
        ];
        const grammar = [
            `root ::= "{" ${keys.map(([key, rule], i) => `${i ? 'sep ' : ''}"\\"${key}\\"" colon ${rule}`).join(' ')} "}"`,
            'sep ::= "," " "?',
            'colon ::= ":" " "?',
            'rating ::= "\\"General Audiences\\"" | "\\"Teen And Up Audiences\\"" | "\\"Mature\\"" | "\\"Explicit\\""',
            'content ::= "\\"SFW\\"" | "\\"NSFW\\""',
            'bool ::= "true" | "false"',
            'warning ::= "\\"Graphic Depictions Of Violence\\"" | "\\"Major Character Death\\"" | "\\"Rape/Non-Con\\"" | "\\"Underage\\""',
            'category ::= "\\"F/M\\"" | "\\"M/M\\"" | "\\"F/F\\"" | "\\"Gen\\"" | "\\"Multi\\"" | "\\"Other\\""',
            'str ::= "\\"" [^"\\\\\\x00-\\x1f] [^"\\\\\\x00-\\x1f]{0,44} "\\""',
            "tag ::= \"\\\"\" [a-z0-9] [a-z0-9_()'.:!?+-]{0,39} \"\\\"\"",
        ].join('\n');
        const system = [
            'You tag character cards for a roleplay library. Read the whole card (and look at the picture if there is one) and answer with JSON only.',
            ao3 ? `AO3 style (Archive of Our Own), Title Case, AO3's canonical wording where it exists:
- rating: General Audiences, Teen And Up Audiences, Mature or Explicit. Explicit when the card contains or invites sex (NSFW/smut tags, sexual descriptions or picture); Mature for strong violence or dark themes without sex.
- warnings: only AO3's exact warnings that clearly apply: Graphic Depictions Of Violence (graphic violence is part of the card), Major Character Death (a main character dies), Rape/Non-Con (non-consent is part of the card), Underage (ONLY if the card explicitly makes a sexualised character under 18; adults or unstated ages never get it). Usually empty.
- category: usually ONE of F/M, M/M, F/F, Gen, Multi or Other (two only for a card that really mixes them), for the relationship the card sets up with the user (a female character with an unspecified user = F/M is fine only if the card implies it; otherwise Gen or Other).
- fandoms: the source franchise, e.g. "Genshin Impact (Video Game)", "Naruto"; "Original Work" for original characters.
- relationships: only named pairings in the card, "Name/Name" for romantic, "Name & Name" for platonic. Usually empty.
- characters: the named characters the card plays (canon names), plus "Original Female Character(s)" / "Original Male Character(s)" / "Original Characters" for OCs, and "Reader" or "You" if the card is second person.
- freeform: up to ${Number(maxAo3) || 12} DIFFERENT tags that help someone choose this roleplay (one tag per idea, never variations of the same word): who the user plays (Any Reader, Male Reader, Female Reader), the dynamic with the user (Enemies to Lovers, Rivals, Master/Servant, Protective), relationship type (Romance, Friendship, Found Family), tone (Fluff, Angst, Comedy, Dark), genre and setting (Fantasy, Sci-Fi, Modern AU, Post-Apocalypse, School Setting), and content themes (Slow Burn, Hurt/Comfort, Yandere, Smut, Violence). No tags about looks, clothing or art; no sentences.` : 'Leave every AO3 field empty (rating General Audiences).',
            danbooru ? `danbooru: up to ${Number(maxDanbooru) || 8} Danbooru tags, lowercase_with_underscores, ONLY of these kinds: one count/gender tag for who the card is about (1girl, 1boy, 2girls, multiple_boys, 1other; a feminine-looking male is 1boy + otoko_no_ko; futanari when stated), age only when it matters for the roleplay (mature_female, mature_male, old_woman, old_man), what they are if not plain human (monster_girl, elf, demon_girl, vampire, android, kemonomimi_mode, furry, giantess), and the setting (fantasy, science_fiction, modern, school, post-apocalypse, medieval). Never hair, eyes, body, clothing, poses, expressions or art-style tags. Use the picture only to check gender and species.` : 'danbooru: empty.',
            `content: NSFW if the card contains or invites sexual content (sex, nudity, fetishes, NSFW/smut tags, a sexual picture), otherwise SFW.
dead_dove: true only when the card is built around extreme or taboo dark content shown without softening (rape/non-con, graphic gore or torture, abuse, incest, snuff, extreme degradation), or the card itself says "Dead Dove"; ordinary angst, bullying, violence or kinks are false.`,
            'No tag may repeat. Never invent canon details. Write {{user}} as Reader.',
        ].join('\n\n');
        const ask = async withImage => {
            const content = withImage
                ? [{ type: 'text', text: `${text}\n\nThe card's picture is attached.` }, { type: 'image_url', image_url: { url: image } }]
                : text;
            const upstream = await fetch(`${LLAMA}/chat/completions`, {
                method: 'POST', signal: AbortSignal.timeout(300000),
                headers: { 'Content-Type': 'application/json', ...(KEY ? { Authorization: `Bearer ${KEY}` } : {}) },
                body: JSON.stringify({
                    model: String(model), temperature: 0.2, max_tokens: 900, stream: false, cache_prompt: true,
                    dry_multiplier: 0.8, dry_base: 1.75, dry_allowed_length: 3,
                    chat_template_kwargs: { enable_thinking: false }, reasoning_effort: 'none', grammar,
                    messages: [{ role: 'system', content: system }, { role: 'user', content }],
                }),
            });
            const body = await upstream.text();
            if (!upstream.ok) { const error = new Error(`llama-swap answered ${upstream.status}: ${body.slice(0, 300)}`); error.status = upstream.status; error.body = body; throw error; }
            return JSON.parse(body);
        };
        const wantImage = /^data:image\/(jpeg|png|webp);base64,/.test(String(image)) && String(image).length < 8_000_000;
        try {
            let vision = wantImage, data;
            try { data = await ask(wantImage); }
            catch (error) {
                if (!wantImage || !/image|mmproj|multimodal|vision/i.test(String(error.body || error.message))) throw error;
                vision = false;          // this model can't see: tag from the text alone
                data = await ask(false);
            }
            const raw = String(data?.choices?.[0]?.message?.content || '').replace(/<think>[\s\S]*?<\/think>/gi, '').replace(/^[\s\S]*<\/think>/i, '');
            let json;
            try { json = JSON.parse(raw.slice(raw.indexOf('{'), raw.lastIndexOf('}') + 1)); }
            catch (error) { console.log(`[Rocsy Addons] autotag unreadable (${data?.choices?.[0]?.finish_reason}, ${data?.timings?.predicted_n} tokens): ${raw.slice(0, 400)} ... ${raw.slice(-300)}`); throw error; }
            // Count tags must agree: keep the first girls tag and the first boys tag, no_humans only on its own.
            const countFix = tags => {
                const girls = /^(\d\+?girls?|multiple_girls)$/, boys = /^(\d\+?boys?|multiple_boys)$/;
                if (tags.includes('otoko_no_ko')) tags = tags.filter(t => !girls.test(t));
                const g = tags.find(t => girls.test(t)), b = tags.find(t => boys.test(t));
                return tags.filter(t => (girls.test(t) ? t === g : boys.test(t) ? t === b : t === 'no_humans' ? !g && !b : true));
            };
            // Roleplay basics only (the user's rule): Danbooru tags must be gender/count, age, species/creature or
            // setting; animal ears/tails become kemonomimi_mode. AO3 tags about hair/eyes/clothes are dropped.
            const BOORU_OK = new Set(['otoko_no_ko', 'futanari', 'androgynous', 'transgender', 'genderswap', 'trap',
                'mature_female', 'mature_male', 'old_woman', 'old_man', 'old', 'elderly', 'milf', 'dilf', 'aged_up',
                'elf', 'dark_elf', 'demon', 'angel', 'fallen_angel', 'vampire', 'werewolf', 'android', 'robot', 'cyborg', 'ghost', 'dragon', 'kitsune', 'succubus',
                'incubus', 'mermaid', 'furry', 'kemonomimi_mode', 'monster', 'alien', 'fairy', 'orc', 'goblin', 'lamia', 'harpy', 'centaur', 'giantess', 'giant',
                'minigirl', 'undead', 'zombie', 'witch', 'oni', 'yokai', 'youkai', 'artificial_intelligence', 'goddess', 'god', 'spirit', 'shapeshifter', 'slime',
                'fantasy', 'dark_fantasy', 'science_fiction', 'cyberpunk', 'steampunk', 'modern', 'contemporary', 'school', 'high_school', 'college', 'office',
                'medieval', 'post-apocalypse', 'historical', 'space', 'military', 'western', 'horror', 'isekai', 'dungeon', 'castle', 'village', 'city', 'forest']);
            const booruOk = t => /^(\d\+?(girl|boy|other)s?|multiple_(girls|boys|others)|no_humans)$/.test(t) || BOORU_OK.has(t)
                || (/_(girl|boy)$/.test(t) && !/^(school|office|shrine|magical|bad|good|cool|cute|tall|short|cat_ear)_/.test(t));
            const booruSpecies = tags => {
                const out = tags.map(t => (/_(ears|tail)$/.test(t) && !/^(pointy|elf)_ears$/.test(t) ? 'kemonomimi_mode' : t === 'sci-fi' || t === 'scifi' ? 'science_fiction' : t === 'pointy_ears' ? 'elf' : t));
                return [...new Set(out)].filter(booruOk);
            };
            const looks = /\b(hair|eyes?|eyed|haired|skin|breasts?|outfit|dress|uniform|clothes|clothing|glasses|tattoos?|piercings?)\b/i;
            // Drop near-duplicates: a tag containing an earlier one (Sadistic Teasing… after Sadistic Tease) or sharing its stem.
            const distinct = tags => {
                const kept = [];
                const stem = t => t.toLowerCase().replace(/[^a-z0-9 ]/g, '').split(' ').map(w => w.slice(0, 6)).join(' ');
                for (const tag of tags) {
                    const low = tag.toLowerCase();
                    if (kept.some(k => low.includes(k.toLowerCase()) && low !== k.toLowerCase() && low.length > k.length + 3 || stem(k) === stem(tag))) continue;
                    kept.push(tag);
                }
                return kept;
            };
            const clean = (value, n = 40) => [...new Set((Array.isArray(value) ? value : []).map(x => String(x || '').replace(/\s+/g, ' ').trim().slice(0, 80)).filter(Boolean))].slice(0, n);
            const titleCase = t => (t === t.toLowerCase() ? t.replace(/(^|[\s/&(-])([a-z])/g, (m, a, b) => a + b.toUpperCase()) : t);
            const nsfw = json.content === 'NSFW' || json.rating === 'Explicit';
            const deadDove = json.dead_dove === true || /dead ?dove/i.test(text);
            response.send({
                content: [nsfw ? 'NSFW' : 'SFW', ...(deadDove ? ['Dead Dove: Do Not Eat'] : [])],
                vision, rating: String(json.rating || ''), warnings: clean(json.warnings, 4).filter(w => w !== 'Underage' || /\b(minor|under ?age|1[0-7][ -]?(years?|y\.?o)|child|loli|shota)\b/i.test(text)), category: clean(json.category, 2), fandoms: clean(json.fandoms, 4),
                relationships: clean(json.relationships, 4).map(t => t.replace(/\{\{user\}\}/gi, 'Reader')), characters: clean(json.characters, 8).map(t => t.replace(/\{\{user\}\}/gi, 'Reader')), freeform: distinct(clean(json.freeform, Number(maxAo3) || 12)).filter(t => !looks.test(t) && !t.includes('_') && !/dead ?dove|^n?sfw$|^(ai|female|male)$/i.test(t)).map(titleCase),
                timings: { ...(data?.timings ? { promptTokens: data.timings.prompt_n, promptMs: Math.round(data.timings.prompt_ms), outTokens: data.timings.predicted_n, outMs: Math.round(data.timings.predicted_ms), cached: data.timings.cache_n } : {}), usage: data?.usage },
                danbooru: countFix(booruSpecies(clean(json.danbooru, nBooru).map(t => t.toLowerCase().replace(/\s+/g, '_'))))
            });
        } catch (error) {
            response.status(502).send({ error: error instanceof SyntaxError ? 'The model didn’t answer with tags (unreadable JSON); try another model.' : error.message });
        }
    });

    // ------------------------------------------------------------- library --

    router.get('/library/search', async (request, response) => {
        const site = String(request.query.site || '');
        const query = {
            q: String(request.query.q || '').slice(0, 200).trim(),
            sort: String(request.query.sort || ''),
            page: Math.min(Math.max(parseInt(String(request.query.page), 10) || 1, 1), 500),
            nsfw: request.query.nsfw === '1' || request.query.nsfw === 'true',
            f: (() => { try { const v = JSON.parse(String(request.query.f || '{}')); return v && typeof v === 'object' ? v : {}; } catch { return {}; } })(),
        };
        try {
            if (site === 'booru') {
                const token = booruToken(request);
                try { return response.send({ ...(await searchBooru(query, token)), signedIn: !!token }); }
                catch (error) {
                    if (!token || error.status !== 403) throw error;
                    // The saved sign-in stopped working: search signed out and say so.
                    return response.send({ ...(await searchBooru(query, '')), signedIn: false, accountError: 'Bot Booru no longer accepts your saved sign-in. Reconnect the account.' });
                }
            }
            if (site === 'chub') return response.send({ ...(await searchChub(query, siteToken(request, 'chub'))), signedIn: !!siteToken(request, 'chub') });
            if (site === 'chub-lore') return response.send({ ...(await searchChub(query, siteToken(request, 'chub'), 'lorebooks')), signedIn: !!siteToken(request, 'chub') });
            if (site === 'booru-lore') {
                const token = booruToken(request);
                try { return response.send({ ...(await searchBooruLore(query, token)), signedIn: !!token }); }
                catch (error) {
                    if (!token || error.status !== 403) throw error;
                    return response.send({ ...(await searchBooruLore(query, '')), signedIn: false, accountError: 'Bot Booru no longer accepts your saved sign-in. Reconnect the account.' });
                }
            }
            response.status(400).send({ error: `Unknown site “${site}”` });
        } catch (error) {
            response.status(error.status || 500).send({ error: error.message });
        }
    });

    // Bot Booru's most used tags, for the tag suggestions.
    let booruTopTags = null;
    router.get('/library/booru-tags', async (_request, response) => {
        try {
            if (!booruTopTags || booruTopTags.at < Date.now() - 6 * 3600e3) {
                const list = await (await siteFetch(`${BOORU}/tags/top/`, { headers: { Accept: 'application/json' } })).json();
                booruTopTags = { at: Date.now(), tags: (Array.isArray(list) ? list : []).filter(t => t?.name && t.category !== 'Auto').map(t => ({ name: t.name, category: t.category, count: t.count })) };
            }
            response.send({ tags: booruTopTags.tags });
        } catch (error) { response.status(error.status || 500).send({ error: error.message }); }
    });

    // A card's comments: Bot Booru (paged by the site) and Chub (reviews + replies, by numeric project id).
    router.get('/library/comments', async (request, response) => {
        const id = String(request.query.id || '');
        const offset = Math.max(0, parseInt(String(request.query.offset), 10) || 0);
        if (!['booru', 'chub'].includes(request.query.site) || !/^\d{1,12}$/.test(id)) return response.status(400).send({ error: 'Comments: Bot Booru or Chub, by numeric id' });
        if (request.query.site === 'chub') {
            try {
                const all = await chubComments(id);
                return response.send({ total: all.length, comments: all.slice(offset, offset + 20) });
            } catch (error) {
                return response.status(error.status || 500).send({ error: error.message });
            }
        }
        try {
            const data = await (await siteFetch(`${BOORU}/interactions/${id}/comments?limit=20&offset=${offset}`, { headers: { Accept: 'application/json' } })).json();
            response.send({
                total: Number(data.total) || 0,
                comments: (data.comments || []).map(c => ({ author: text(c.username) || 'someone', text: text(c.content).slice(0, 4000), at: c.created_at || '', score: Number(c.score) || 0 })),
            });
        } catch (error) {
            response.status(error.status || 500).send({ error: error.message });
        }
    });

    router.get('/library/details', async (request, response) => {
        const site = String(request.query.site || '');
        const id = String(request.query.id || '');
        try {
            if (site === 'chub' && chubPath(id)) return response.send(await chubDetails(id, siteToken(request, 'chub')));
            if (site === 'booru' && /^\d{1,12}$/.test(id)) return response.send(await booruDetails(id, siteToken(request, 'booru')));
            if (site === 'chub-lore' && chubPath(id)) return response.send(await chubLoreDetails(id, siteToken(request, 'chub')));
            if (site === 'booru-lore' && /^\d{1,12}$/.test(id)) return response.send(await booruLoreDetails(id, siteToken(request, 'booru')));
            response.status(400).send({ error: 'Expected site=chub|booru and its card id' });
        } catch (error) {
            response.status(error.status || 500).send({ error: error.message });
        }
    });

    // A Bot Booru lorebook as SillyTavern World Info JSON (the site's own download.json). Chub lorebooks go through
    // SillyTavern's /api/content/importURL instead.
    router.get('/library/lorebook', async (request, response) => {
        const id = String(request.query.id || '');
        if (request.query.site !== 'booru-lore' || !/^\d{1,12}$/.test(id)) return response.status(400).send({ error: 'Expected site=booru-lore and a lorebook number' });
        try {
            const token = booruToken(request);
            const upstream = await siteFetch(`${BOORU}/api/lorebooks/${id}/download.json`, { headers: { Accept: 'application/json', ...(token ? ACCOUNTS.booru.headers(token) : {}) } }, 120000);
            const book = await upstream.json().catch(() => null);
            if (!book || typeof book !== 'object' || !book.entries) throw new SiteError('Bot Booru didn’t send a lorebook file');
            response.send(book);
        } catch (error) {
            response.status(error.status || 500).send({ error: error.message });
        }
    });

    // A Bot Booru card as the PNG the site serves (character data embedded).
    router.get('/library/card', async (request, response) => {
        const id = String(request.query.id || '');
        if (request.query.site !== 'booru' || !/^\d{1,12}$/.test(id)) return response.status(400).send({ error: 'Expected site=booru and a numeric id' });
        try {
            const token = booruToken(request);
            await booruDetails(id, token);   // checks the card exists before downloading
            const upstream = await siteFetch(`${BOORU}/download/png/${id}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} }, 120000);
            const type = upstream.headers.get('content-type') || '';
            if (!/png|octet-stream/.test(type)) throw new SiteError(`Bot Booru sent ${type || 'something'} instead of a PNG card`);
            response.set('Content-Type', 'image/png');
            response.set('Content-Disposition', `attachment; filename="botbooru-${id}.png"`);
            response.send(Buffer.from(await upstream.arrayBuffer()));
        } catch (error) {
            response.status(error.status || 500).send({ error: error.message });
        }
    });

    // Connected site accounts (Bot Booru, Chub): who is signed in, connect (checked first), disconnect.
    router.get('/library/account', async (request, response) => {
        const site = String(request.query.site || '');
        if (!isAccount(site)) return response.status(400).send({ error: 'Unknown site' });
        const token = siteToken(request, site);
        if (!token) return response.send({ connected: false });
        try { response.send({ connected: true, user: await whoAmI(site, token) }); }
        catch (error) { response.send({ connected: true, user: '', error: `Saved, but the site didn’t accept it just now: ${error.message}` }); }
    });
    router.post('/library/account', async (request, response) => {
        const site = String(request.body?.site || '');
        const token = String(request.body?.token || '').trim().replace(/^"|"$/g, '');
        if (!isAccount(site)) return response.status(400).send({ error: 'Unknown site' });
        try {
            if (!token) {
                deleteSecret(request.user.directories, ACCOUNTS[site].secret);
                return response.send({ connected: false });
            }
            let user;
            try { user = await whoAmI(site, token); }
            catch (error) { return response.status(400).send({ error: `That sign-in didn’t work (${error.message}). Sign in on the site again and reconnect.` }); }
            if (request.body?.check) return response.send({ connected: false, user });   // look only: the page confirms first
            writeSecret(request.user.directories, ACCOUNTS[site].secret, token);
            response.send({ connected: true, user });
        } catch (error) {
            response.status(500).send({ error: error.message });
        }
    });
}

export async function exit() {
    clearInterval(presence.timer);
    exitImage();
    exitGeneration();
}
