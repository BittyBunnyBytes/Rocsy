"""Text-to-speech for the Post-Edit tab: Microsoft Edge neural voices (edge-tts,
free, no key) and TikTok's voices through a third-party route.

TikTok has no public TTS API. Three unofficial routes are supported:
- "public" (default): LazyPy's free TTS service (lazypy.ro), which relays
  TikTok's voices with its own account - no login needed. 300 bytes/request.
- "direct": TikTok's own internal endpoint, authenticated with the `sessionid`
  cookie of a logged-in TikTok account (pasted on the Post-Edit tab and stored
  server-side only, like the DeepSeek key).
- "proxy": a self-hosted/community proxy speaking Weilbyte's {text, voice} ->
  base64 API. The public Weilbyte ones are dead as of 2026-09-26 (weilnet
  redirects to a host that refuses connections; countik is disabled).
If "direct"/"proxy" fail and tiktok_fallback is on, the clip is retried
through "public". All of these may break without notice and may violate
TikTok's terms.

Every synthesis returns MP3 bytes, SRT captions and (Edge only) per-word
timings for the TikTok-style "pop" captions. TikTok gets one cue per request
chunk, timed with ffprobe; its word timings are estimated at render time.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("h3machine.tts")

STATE_KEY = "tts_settings"
DEFAULTS: dict[str, Any] = {
    "tiktok_mode": "public",
    "tiktok_fallback": True,
    "tiktok_public_url": "https://lazypy.ro/tts/request_tts.php",
    "tiktok_proxy_url": "https://tiktok-tts.weilnet.workers.dev",
    "tiktok_api_base": "https://api16-normal-v6.tiktokv.com",
}
DEAD_PROXIES = {"https://tiktok-tts.weilnet.workers.dev", "https://tiktok-tts.weilbyte.dev", "https://ottsy.weilbyte.dev"}
TIKTOK_MODES = ("public", "direct", "proxy")
PUBLIC_BYTES = 280  # LazyPy's TikTok limit is 300 bytes
MAX_TEXT = 5000
TIKTOK_CHUNK = 200  # the internal endpoint rejects much longer requests
TIKTOK_USER_AGENT = (
    "com.zhiliaoapp.musically/2022600030 (Linux; U; Android 7.1.2; es_ES; SM-G988N; Build/NRD90M;tt-ok/3.12.13.1)"
)

TIKTOK_VOICES: tuple[tuple[str, str, str], ...] = (
    # Generated 2026-09-26 from LazyPy's TikTok voice list (the set that works through it).
    ('en_us_001', 'Jessie (female)', 'English'),
    ('en_uk_001', 'Narrator (Chris)', 'English'),
    ('en_uk_003', 'UK Male 2 (male)', 'English'),
    ('en_female_emotional', 'Peaceful (female)', 'English'),
    ('en_au_001', 'Metro (Eddie)', 'English'),
    ('en_au_002', 'Smooth (Alex)', 'English'),
    ('en_us_002', 'Jessie (female)', 'English'),
    ('en_us_006', 'Joey (male)', 'English'),
    ('en_us_007', 'Professor (male)', 'English'),
    ('en_us_009', 'Scientist (male)', 'English'),
    ('en_us_010', 'Confidence (male)', 'English'),
    ('en_female_samc', 'Empathetic (female)', 'English'),
    ('en_male_cody', 'Serious (male)', 'English'),
    ('en_male_narration', 'Story Teller (male)', 'English'),
    ('en_male_jarvis', 'Alfred (male)', 'English'),
    ('en_male_funny', 'Wacky (male)', 'English characters'),
    ('en_male_ashmagic', 'Ash Magic (male)', 'English characters'),
    ('en_male_santa_narration', 'Author (male)', 'English characters'),
    ('en_female_betty', 'Bae (female)', 'English characters'),
    ('en_female_makeup', 'Beauty Guru (female)', 'English characters'),
    ('en_female_richgirl', 'Bestie (female)', 'English characters'),
    ('en_female_amie', 'Billy (female)', 'English characters'),
    ('en_male_jason', 'Captain (male)', 'English characters'),
    ('en_male_chris', 'Chris (male)', 'English characters'),
    ('en_male_miki', 'Comedian (male)', 'English characters'),
    ('en_male_cupid', 'Cupid', 'English characters'),
    ('en_female_shenna', 'Debutante (female)', 'English characters'),
    ('en_male_whitney', 'Designer (male)', 'English characters'),
    ('en_female_doll', 'Doll (female)', 'English characters'),
    ('en_male_adam_elf', 'Elf', 'English characters'),
    ('en_male_adrian', 'Foodie (male)', 'English characters'),
    ('en_male_jomboy', 'Game On (male)', 'English characters'),
    ('en_female_ghost', 'Ghost', 'English characters'),
    ('en_male_ghosthost', 'Ghost Host', 'English characters'),
    ('en_male_david_gingerman', 'GingerChime (male)', 'English characters'),
    ('en_female_grandma', 'Grandma (female)', 'English characters'),
    ('en_male_ukneighbor', 'Lord Cringe (male)', 'English characters'),
    ('en_male_wizard', 'Magician', 'English characters'),
    ('en_male_trevor', 'Marty (male)', 'English characters'),
    ('en_male_deadpool', 'Mr. GoodGuy (Deadpool)', 'English characters'),
    ('en_male_ukbutler', 'Mr. Meticulous (male)', 'English characters'),
    ('en_male_olantekkers', 'Olantekkers (male)', 'English characters'),
    ('en_male_petercullen', 'Optimus Prime', 'English characters'),
    ('en_male_pirate', 'Pirate', 'English characters'),
    ('en_male_santa', 'Santa', 'English characters'),
    ('en_male_santa_effect', 'Santa (w/ effect)', 'English characters'),
    ('en_male_corey_santa', 'Santa (Corey)', 'English characters'),
    ('en_male_maxwell', 'Stylist (male)', 'English characters'),
    ('en_female_pansino', 'Varsity (female)', 'English characters'),
    ('en_female_erika', 'Victory (female)', 'English characters'),
    ('en_female_werewolf', 'Werewolf', 'English characters'),
    ('en_female_witch', 'Witch', 'English characters'),
    ('en_female_zombie', 'Zombie', 'English characters'),
    ('en_male_grinch', 'Trickster (Grinch)', 'English characters'),
    ('en_us_ghostface', 'Ghostface (Scream)', 'English characters'),
    ('en_us_chewbacca', 'Chewbacca (Star Wars)', 'English characters'),
    ('en_us_c3po', 'C-3PO (Star Wars)', 'English characters'),
    ('en_us_stormtrooper', 'Stormtrooper (Star Wars)', 'English characters'),
    ('en_us_stitch', 'Stitch (Lilo & Stitch)', 'English characters'),
    ('en_us_rocket', 'Rocket (Guardians of the Galaxy)', 'English characters'),
    ('en_female_madam_leota', 'Madame Leota (Haunted Mansion)', 'English characters'),
    ('en_male_sing_deep_jingle', 'Song: Caroler (male)', 'Singing'),
    ('en_male_m03_classical', 'Song: Classic Electric (male)', 'Singing'),
    ('en_female_f08_salut_damour', "Song: Cottagecore (Salut d'Amour)", 'Singing'),
    ('en_male_m2_xhxs_m03_christmas', 'Song: Cozy (male)', 'Singing'),
    ('en_female_f08_warmy_breeze', 'Song: Open Mic (Warmy Breeze)', 'Singing'),
    ('en_female_ht_f08_halloween', 'Song: Opera (Halloween)', 'Singing'),
    ('en_female_ht_f08_glorious', 'Song: Euphoric (Glorious)', 'Singing'),
    ('en_male_sing_funny_it_goes_up', 'Song: Hypetrain (It Goes Up)', 'Singing'),
    ('en_male_m03_lobby', 'Song: Jingle (Lobby)', 'Singing'),
    ('en_female_ht_f08_wonderful_world', 'Song: Melodrama (Wonderful World)', 'Singing'),
    ('en_female_ht_f08_newyear', 'Song: NYE 2023 (female)', 'Singing'),
    ('en_male_sing_funny_thanksgiving', 'Song: Thanksgiving (male)', 'Singing'),
    ('en_male_m03_sunshine_soon', 'Song: Toon Beat (Sunshine Soon)', 'Singing'),
    ('en_female_f08_twinkle', 'Song: Pop Lullaby (female)', 'Singing'),
    ('en_male_m2_xhxs_m03_silly', 'Song: Quirky Time (male)', 'Singing'),
    ('fr_001', 'French Male 1 (male)', 'French'),
    ('fr_002', 'French Male 2 (male)', 'French'),
    ('de_001', 'German Female (female)', 'German'),
    ('de_002', 'German Male (male)', 'German'),
    ('id_male_darma', 'Darma (male)', 'Indonesian'),
    ('id_female_icha', 'Icha (female)', 'Indonesian'),
    ('id_female_noor', 'Noor (female)', 'Indonesian'),
    ('id_male_putra', 'Putra (male)', 'Indonesian'),
    ('it_male_m18', 'Italian Male (male)', 'Italian'),
    ('jp_001', 'Miho (美穂)', 'Japanese'),
    ('jp_003', 'Keiko (恵子)', 'Japanese'),
    ('jp_005', 'Sakura (さくら)', 'Japanese'),
    ('jp_006', 'Naoki (直樹)', 'Japanese'),
    ('jp_male_osada', 'モリスケ (Morisuke)', 'Japanese'),
    ('jp_male_matsuo', 'モジャオ (Matsuo)', 'Japanese'),
    ('jp_female_machikoriiita', 'まちこりーた (Machikoriiita)', 'Japanese'),
    ('jp_male_matsudake', 'マツダ家の日常 (Matsudake)', 'Japanese'),
    ('jp_male_shuichiro', '修一朗 (Shuichiro)', 'Japanese'),
    ('jp_female_rei', '丸山礼 (Maruyama Rei)', 'Japanese'),
    ('jp_male_hikakin', 'ヒカキン (Hikakin)', 'Japanese'),
    ('jp_female_yagishaki', '八木沙季 (Yagi Saki)', 'Japanese'),
    ('kr_002', 'Korean Male 1 (male)', 'Korean'),
    ('kr_004', 'Korean Male 2 (male)', 'Korean'),
    ('kr_003', 'Korean Female (female)', 'Korean'),
    ('br_003', 'Júlia (female)', 'Portuguese'),
    ('br_004', 'Ana (female)', 'Portuguese'),
    ('br_005', 'Lucas (male)', 'Portuguese'),
    ('pt_female_lhays', 'Lhays Macedo (female)', 'Portuguese'),
    ('pt_female_laizza', 'Laizza (female)', 'Portuguese'),
    ('pt_male_transformer', 'Optimus Prime (Portuguese)', 'Portuguese'),
    ('es_002', 'Spanish Male (male)', 'Spanish'),
    ('es_male_m3', 'Julio (male)', 'Spanish'),
    ('es_female_f6', 'Alejandra (female)', 'Spanish'),
    ('es_female_fp1', 'Mariana (female)', 'Spanish'),
    ('es_mx_002', 'Álex (Warm)', 'Spanish'),
    ('es_mx_male_transformer', 'Optimus Prime (Mexican)', 'Spanish'),
    ('es_mx_female_supermom', 'Super Mamá', 'Spanish'),
)
TIKTOK_VOICE_CODES = {code for code, _label, _group in TIKTOK_VOICES}


class TTSError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ settings --

def load(store: Any) -> dict[str, Any]:
    settings = {**DEFAULTS, **(store.get_state(STATE_KEY) or {})}
    # Saved before the public route existed: a dead Weilbyte proxy, or Direct
    # with no session, can't work - use the public route instead.
    if settings["tiktok_mode"] == "proxy" and settings["tiktok_proxy_url"].rstrip("/") in DEAD_PROXIES:
        settings["tiktok_mode"] = "public"
    if settings["tiktok_mode"] == "direct" and not settings.get("tiktok_session_id"):
        settings["tiktok_mode"] = "public"
    if settings["tiktok_mode"] not in TIKTOK_MODES:
        settings["tiktok_mode"] = "public"
    return settings


def public(settings: dict[str, Any]) -> dict[str, Any]:
    """Everything except the session id itself (only whether one is saved)."""
    session = settings.get("tiktok_session_id") or ""
    result = {key: value for key, value in settings.items() if key != "tiktok_session_id"}
    result.update(tiktok_session_set=bool(session), tiktok_session_hint=session[-4:] if len(session) >= 8 else "")
    return result


def update(store: Any, changes: dict[str, Any]) -> dict[str, Any]:
    settings = load(store)
    if changes.get("tiktok_mode") in TIKTOK_MODES:
        settings["tiktok_mode"] = changes["tiktok_mode"]
    if "tiktok_fallback" in changes:
        settings["tiktok_fallback"] = bool(changes["tiktok_fallback"])
    for key in ("tiktok_proxy_url", "tiktok_api_base", "tiktok_public_url"):
        value = changes.get(key)
        if isinstance(value, str) and value.strip():
            url = value.strip().rstrip("/")
            if not re.match(r"^https?://", url):
                raise ValueError("URLs must start with https://")
            settings[key] = url
    if "tiktok_session_id" in changes:
        session = (changes.get("tiktok_session_id") or "").strip()
        if session.lower().startswith("sessionid="):
            session = session.split("=", 1)[1]
        if session and not re.fullmatch(r"[A-Za-z0-9_\-]{8,200}", session):
            raise ValueError("That doesn't look like a TikTok sessionid cookie value.")
        if session:
            settings["tiktok_session_id"] = session
        else:
            settings.pop("tiktok_session_id", None)
    store.set_state(STATE_KEY, settings)
    return settings


# -------------------------------------------------------------------- voices --

_edge_voice_cache: tuple[float, list[dict[str, Any]]] | None = None


async def edge_voices() -> list[dict[str, Any]]:
    global _edge_voice_cache
    if _edge_voice_cache and time.monotonic() - _edge_voice_cache[0] < 24 * 3600:
        return _edge_voice_cache[1]
    import edge_tts

    try:
        raw = await edge_tts.list_voices()
    except Exception as exc:  # noqa: BLE001 - network/service failure
        raise TTSError(f"Couldn't load the Edge voice list: {exc}") from exc
    voices = []
    for voice in raw:
        tag = voice.get("VoiceTag") or {}
        voices.append({
            "id": voice["ShortName"],
            "label": voice["ShortName"].split("-", 2)[-1].replace("Neural", "").replace("Multilingual", " Multilingual"),
            "locale": voice.get("Locale", ""),
            "gender": voice.get("Gender", ""),
            "traits": ", ".join(tag.get("VoicePersonalities") or []),
        })
    voices.sort(key=lambda item: (item["locale"], item["id"]))
    _edge_voice_cache = (time.monotonic(), voices)
    return voices


def tiktok_voices() -> list[dict[str, Any]]:
    return [{"id": code, "label": label, "group": group} for code, label, group in TIKTOK_VOICES]


# ------------------------------------------------------------------- helpers --

def _srt_time(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(cues: list[tuple[float, float, str]]) -> str:
    return "".join(
        f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{text.strip()}\n\n"
        for index, (start, end, text) in enumerate(cues, 1) if text.strip()
    )


def split_text(text: str, limit: int, measure: Any = len) -> list[str]:
    """Split at sentence ends, then commas, then spaces, keeping each chunk's
    measure (characters, or UTF-8 bytes for LazyPy) <= limit."""
    text = re.sub(r"\s+", " ", text).strip()
    chunks: list[str] = []
    for sentence in re.split(r"(?<=[.!?…。！？])\s*", text):
        while measure(sentence) > limit:
            fit = len(sentence)
            while fit > 1 and measure(sentence[:fit]) > limit:
                fit -= 1
            cut = max(sentence.rfind(", ", 0, fit), sentence.rfind(" ", 0, fit))
            cut = cut if cut > fit // 3 else fit - 1
            chunks.append(sentence[:cut + 1].strip())
            sentence = sentence[cut + 1:].strip()
        if sentence:
            if chunks and measure(f"{chunks[-1]} {sentence}") <= limit:
                chunks[-1] = f"{chunks[-1]} {sentence}"
            else:
                chunks.append(sentence)
    return chunks


async def _run(*args: str) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    return process.returncode or 0, stdout, stderr


async def _duration(path: Path) -> float:
    code, stdout, _ = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path))
    try:
        return float(stdout.decode().strip()) if code == 0 else 0.0
    except ValueError:
        return 0.0


def _atempo_chain(speed: float) -> str:
    """atempo only takes 0.5-2.0 per instance; chain for anything outside."""
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.4f}")
    return ",".join(parts)


# ----------------------------------------------------------------- synthesis --

def align_words(text: str, words: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Edge's word boundaries carry bare words; map each back onto the script so
    it keeps its punctuation ("him." not "him"), which the captions show and
    sentence grouping needs."""
    aligned, position = [], 0
    for start, end, word in words:
        index = text.find(word, position)
        if index < 0 or index - position > 40:
            aligned.append((start, end, word))
            continue
        stop = index + len(word)
        while stop < len(text) and not text[stop].isspace():
            stop += 1
        token = text[index:stop]
        position = stop
        aligned.append((start, end, token))
    return aligned


def words_to_srt(words: list[tuple[float, float, str]]) -> str:
    """Sentence cues from word timings: break after . ! ? … or a pause over 0.6s."""
    cues: list[tuple[float, float, str]] = []
    current: list[tuple[float, float, str]] = []
    for index, word in enumerate(words):
        current.append(word)
        pause = words[index + 1][0] - word[1] if index + 1 < len(words) else 0
        if re.search(r"[.!?…]['\")]*$", word[2]) or pause > 0.6 or len(current) >= 18:
            cues.append((current[0][0], current[-1][1], " ".join(w[2] for w in current)))
            current = []
    if current:
        cues.append((current[0][0], current[-1][1], " ".join(w[2] for w in current)))
    return build_srt(cues)


async def synthesize_edge(text: str, voice: str, rate: int, pitch: int, volume: int
                          ) -> tuple[bytes, str, list[tuple[float, float, str]]]:
    import edge_tts

    try:  # the constructor validates the voice name (ValueError: a 500 before)
        communicate = edge_tts.Communicate(
            text, voice, rate=f"{rate:+d}%", pitch=f"{pitch:+d}Hz", volume=f"{volume:+d}%", boundary="WordBoundary",
        )
    except ValueError as exc:
        raise TTSError(f"Edge TTS can't use that voice: {exc}", 422) from exc
    words: list[tuple[float, float, str]] = []
    audio = bytearray()
    try:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 10_000_000
                words.append((round(start, 3), round(start + chunk["duration"] / 10_000_000, 3), chunk["text"]))
    except Exception as exc:  # noqa: BLE001 - edge_tts raises several network error types
        raise TTSError(f"Edge TTS failed: {exc}") from exc
    if not audio:
        raise TTSError("Edge TTS returned no audio (unknown voice, or text with nothing speakable).", 422)
    words = align_words(re.sub(r"\s+", " ", text), words)
    return bytes(audio), words_to_srt(words), words


async def _tiktok_chunk(client: httpx.AsyncClient, settings: dict[str, Any], text: str, voice: str) -> bytes:
    if settings["tiktok_mode"] == "public":
        try:
            response = await client.post(
                settings["tiktok_public_url"], data={"service": "TikTok", "voice": voice, "text": text},
                headers={"User-Agent": "Mozilla/5.0 (H3 Machine)"},
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TTSError(f"The public TikTok TTS service (LazyPy) didn't answer properly ({type(exc).__name__}).") from exc
        if not payload.get("success") or not payload.get("audio_url"):
            raise TTSError(f"Public TikTok TTS error: {payload.get('error_msg') or 'no audio returned'}")
        try:
            audio = await client.get(payload["audio_url"], headers={"User-Agent": "Mozilla/5.0 (H3 Machine)"})
            audio.raise_for_status()
        except httpx.HTTPError as exc:
            raise TTSError(f"Couldn't fetch the generated TikTok audio ({type(exc).__name__}).") from exc
        return audio.content

    if settings["tiktok_mode"] == "proxy":
        url = f"{settings['tiktok_proxy_url']}/api/generation"
        try:
            response = await client.post(url, json={"text": text, "voice": voice})
        except httpx.HTTPError as exc:
            raise TTSError(f"The TikTok TTS proxy didn't answer ({type(exc).__name__}). Public proxies go down often; "
                           "try Direct mode with a session id, or another proxy URL.") from exc
        try:
            payload = response.json()
        except ValueError:
            raise TTSError(f"The TikTok TTS proxy returned HTTP {response.status_code}, not JSON.") from None
        data = payload.get("data")
        if not payload.get("success") or not isinstance(data, str):
            raise TTSError(f"TikTok TTS proxy error: {payload.get('error') or payload.get('message') or response.status_code}")
        return base64.b64decode(data)

    session = settings.get("tiktok_session_id")
    if not session:
        raise TTSError("TikTok Direct mode needs your TikTok sessionid cookie. Add it under TikTok settings.", 422)
    clean = text.replace("+", " plus ").replace("&", " and ")
    query = urllib.parse.urlencode({"text_speaker": voice, "req_text": clean, "speaker_map_type": 0, "aid": 1233})
    url = f"{settings['tiktok_api_base']}/media/api/text/speech/invoke/?{query}"
    try:
        response = await client.post(url, headers={"User-Agent": TIKTOK_USER_AGENT, "Cookie": f"sessionid={session}"})
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise TTSError(f"TikTok's TTS endpoint failed: {exc}") from exc
    status = payload.get("status_code")
    if status != 0:
        hints = {1: "the sessionid is invalid or expired - copy a fresh one from a logged-in browser",
                 2: "the text is too long", 4: "that voice code isn't available"}
        raise TTSError(f"TikTok TTS error {status}: {hints.get(status, payload.get('status_msg') or 'unknown')}.")
    encoded = ((payload.get("data") or {}).get("v_str")) or ""
    if not encoded:
        raise TTSError("TikTok returned no audio for that text.")
    return base64.b64decode(encoded)


async def synthesize_tiktok(text: str, voice: str, rate: int, settings: dict[str, Any]) -> tuple[bytes, str, None]:
    if voice not in TIKTOK_VOICE_CODES:
        raise TTSError("Unknown TikTok voice.", 422)
    async def fetch(route: dict[str, Any]) -> tuple[list[str], list[bytes]]:
        public = route["tiktok_mode"] == "public"
        parts = split_text(text, PUBLIC_BYTES if public else TIKTOK_CHUNK, (lambda s: len(s.encode())) if public else len)
        if not parts:
            raise TTSError("Nothing to say.", 422)
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            return parts, [await _tiktok_chunk(client, route, part, voice) for part in parts]

    try:
        chunks, audios = await fetch(settings)
    except TTSError as exc:
        if exc.status == 422 or settings["tiktok_mode"] == "public" or not settings.get("tiktok_fallback", True):
            raise
        log.warning("TikTok %s route failed (%s); retrying through the public service", settings["tiktok_mode"], exc)
        try:
            chunks, audios = await fetch({**settings, "tiktok_mode": "public"})
        except TTSError as public_exc:
            raise TTSError(f"{exc} The public fallback failed too: {public_exc}") from public_exc
    speed = max(0.5, min(3.0, 1 + rate / 100))
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        cues: list[tuple[float, float, str]] = []
        cursor = 0.0
        for index, (chunk, audio) in enumerate(zip(chunks, audios)):
            part = folder / f"{index:03d}.mp3"
            part.write_bytes(audio)
            length = await _duration(part) / speed
            cues.append((cursor, cursor + length, chunk))
            cursor += length
        (folder / "list.txt").write_text("".join(f"file '{index:03d}.mp3'\n" for index in range(len(chunks))))
        output = folder / "out.mp3"
        args = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(folder / "list.txt")]
        if abs(speed - 1) > 0.01:
            args += ["-af", _atempo_chain(speed)]
        args += ["-c:a", "libmp3lame", "-b:a", "192k", str(output)]
        code, _, stderr = await _run(*args)
        if code != 0 or not output.is_file():
            raise TTSError(f"Couldn't join the TikTok audio chunks: {stderr.decode(errors='replace')[-300:]}")
        return output.read_bytes(), build_srt(cues), None  # no word timings: captions estimate them


async def synthesize(provider: str, text: str, voice: str, rate: int, pitch: int, volume: int,
                     settings: dict[str, Any]) -> tuple[bytes, str, list[tuple[float, float, str]] | None]:
    text = text.strip()
    if not text:
        raise TTSError("Type something to say.", 422)
    if len(text) > MAX_TEXT:
        raise TTSError(f"At most {MAX_TEXT} characters per clip.", 422)
    if provider == "edge":
        return await synthesize_edge(text, voice, rate, pitch, volume)
    if provider == "tiktok":
        return await synthesize_tiktok(text, voice, rate, settings)
    raise TTSError("Unknown TTS provider.", 422)
