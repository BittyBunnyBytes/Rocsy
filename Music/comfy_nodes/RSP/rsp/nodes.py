"""RSP nodes. RSP Suno Cover is one node with Suno's controls; it expands into the others plus YuE2's own."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
import wave

import numpy as np
import torch

from . import dsp, fast_singing, fast_vocals, lyrics as lyric_layout, settings, voices

voices.register_routes()

CATEGORY = "RSP"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
RUNNER = os.path.join(ROOT, "rsp", "seedvc_runner.py")
VOICE_SOURCES = ("source singer", "voice audio input", "off")
MAX_CONVERT_SECONDS = 3600

SLIDER = {"min": 0.0, "max": 100.0, "step": 1.0, "display": "slider"}
AUDIO_TIP = "How closely the cover follows the song. 70+: melody and chords. 35-69: melody only. Under 35: its lyrics and your style, no tune."
STYLE_TIP = "How closely it follows your style text, Loose to Strong. 50 = YuE2's released default."
WEIRD_TIP = "Safe to Chaos. 50 = the normal expected result."


def _write_wav(path: str, audio: dict) -> None:
    """Mono 16-bit, Seed-VC's input is mono anyway."""
    mono = audio["waveform"][0].float().mean(dim=0).clamp(-1.0, 1.0).cpu().numpy()
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(int(audio["sample_rate"]))
        out.writeframes((mono * 32767.0).astype(np.int16).tobytes())


def _write_wav_44k_stereo(path: str, audio: dict) -> None:
    """What the vocal separator behind Bunny Transcribe reads."""
    like = {"waveform": torch.zeros(1, 2, int(audio["waveform"].shape[-1] * 44100 / int(audio["sample_rate"]))), "sample_rate": 44100}
    pcm = dsp.match(audio, like)[0].clamp(-1.0, 1.0).cpu().numpy().T
    with wave.open(path, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44100)
        out.writeframes((pcm * 32767.0).astype(np.int16).tobytes())


def _run_seedvc(source: dict, reference: dict, steps: int, cfg_rate: float, semitones: int, auto_f0: bool,
                match_range: bool = False) -> dict:
    import comfy.model_management as management
    import folder_paths

    if not os.path.exists(PYTHON):
        raise RuntimeError(f"RSP's Seed-VC isn't installed. Run {os.path.join(ROOT, 'install.sh')} once.")
    with tempfile.TemporaryDirectory(prefix=f"rsp_{uuid.uuid4().hex[:6]}_", dir=folder_paths.get_temp_directory()) as work:
        src, ref, out = (os.path.join(work, name) for name in ("source.wav", "reference.wav", "out.npy"))
        _write_wav(src, source)
        _write_wav(ref, reference)
        # Nothing else runs while this node does: give Seed-VC the whole GPU.
        management.unload_all_models()
        management.soft_empty_cache()
        command = [PYTHON, RUNNER, "--source", src, "--target", ref, "--output", out, "--steps", str(steps),
                   "--cfg-rate", str(cfg_rate), "--semitones", str(semitones)] + (["--auto-f0"] if auto_f0 else []) \
            + (["--match-range"] if match_range else [])
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONUNBUFFERED="1")
        log = os.path.join(work, "log.txt")
        with open(log, "wb") as sink:
            process = subprocess.Popen(command, env=env, stdout=sink, stderr=subprocess.STDOUT)
            waited = 0.0
            try:
                while process.poll() is None:
                    management.throw_exception_if_processing_interrupted()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        waited += 0.5
                    if waited > MAX_CONVERT_SECONDS:
                        raise TimeoutError("Seed-VC ran for over an hour.")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        if process.returncode != 0 or not os.path.isfile(out):
            with open(log, errors="replace") as handle:
                raise RuntimeError(f"Seed-VC failed ({process.returncode}): {handle.read()[-1500:]}")
        with open(log, errors="replace") as handle:
            for line in handle:
                if line.startswith("[RSP]"):
                    print(line.rstrip())
        with open(out + ".rate") as handle:
            rate = int(handle.read())
        result = torch.from_numpy(np.load(out, allow_pickle=False).copy()).float()
    return {"waveform": result.reshape(1, 1, -1), "sample_rate": rate}


class RSPVocals:
    DESCRIPTION = "A recording's voice: YuE2 Vocals Only's model and weights, run in batches at half precision (about 5x faster)."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",)}, "optional": {"options": ("YUE2_OPTIONS",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("vocals",)
    FUNCTION = "separate"
    CATEGORY = CATEGORY

    def separate(self, audio, options=None, unique_id=None):
        return (fast_vocals.voice_of(audio, options, unique_id),)


class RSPCleanVoice:
    DESCRIPTION = "The voice recording itself when separating it removed next to nothing (already a clean vocal), else its separated vocals."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"original": ("AUDIO",), "vocals": ("AUDIO",)}}

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("vocals",)
    FUNCTION = "pick"
    CATEGORY = CATEGORY

    def pick(self, original, vocals):
        return (dsp.clean_voice(original, vocals),)


class RSPPickVoiceReference:
    DESCRIPTION = "Finds the stretch of a separated vocal with the most singing, for RSP Voice Convert (Seed-VC reads 25 s at most)."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"vocals": ("AUDIO",),
                             "seconds": ("FLOAT", {"default": 25.0, "min": 5.0, "max": 25.0, "step": 1.0})}}

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("reference",)
    FUNCTION = "pick"
    CATEGORY = CATEGORY

    def pick(self, vocals, seconds):
        return (dsp.pick_reference(vocals, seconds),)


class RSPVoiceConvert:
    DESCRIPTION = "Sings a vocal again in the reference's voice, on the GPU, keeping its notes and timing (Seed-VC singing model). No training needed."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "vocals": ("AUDIO", {"tooltip": "The vocal to change: separated, no band."}),
            "reference": ("AUDIO", {"tooltip": "Clean singing in the wanted voice."}),
            "steps": ("INT", {"default": 40, "min": 4, "max": 100, "tooltip": "Seed-VC recommends 30-50 for singing."}),
            "cfg_rate": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.05}),
            "semitones": ("INT", {"default": 0, "min": -24, "max": 24, "tooltip": "Moves the converted voice only, for a singer whose range differs."}),
            "auto_pitch": ("BOOLEAN", {"default": False, "tooltip": "Moves the vocal to the reference's pitch level. Usually off for singing."}),
        }, "optional": {
            "match_range": ("BOOLEAN", {"default": True, "tooltip": "With semitones 0, moves the vocal by whole octaves to the "
                                        "reference's range, so it stays in key. Off: the notes stay where the song sang them."}),
        }}

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("converted",)
    FUNCTION = "convert"
    CATEGORY = CATEGORY

    def convert(self, vocals, reference, steps, cfg_rate, semitones, auto_pitch, match_range=True):
        return (_run_seedvc(vocals, reference, steps, cfg_rate, semitones, auto_pitch, match_range),)


class RSPSplitBand:
    DESCRIPTION = "The band of a song: the mix minus its separated voice."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"mix": ("AUDIO",), "vocals": ("AUDIO",)}}

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("band",)
    FUNCTION = "split"
    CATEGORY = CATEGORY

    def split(self, mix, vocals):
        return (dsp.band_of(mix, vocals),)


class RSPMix:
    DESCRIPTION = "Puts the converted voice over the band at the level the original vocal had."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "band": ("AUDIO",), "converted": ("AUDIO",), "original_vocals": ("AUDIO",),
            "voice_strength": ("FLOAT", {**SLIDER, "default": 100.0, "tooltip": "100 = only the converted voice, 0 = only YuE2's."}),
            "vocal_gain_db": ("FLOAT", {"default": 0.0, "min": -12.0, "max": 12.0, "step": 0.5}),
        }}

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("cover",)
    FUNCTION = "mix"
    CATEGORY = CATEGORY

    def mix(self, band, converted, original_vocals, voice_strength, vocal_gain_db):
        return (dsp.mix_cover(band, converted, original_vocals, voice_strength, vocal_gain_db),)


class RSPCustomVoice:
    DESCRIPTION = "A voice from RSP's library (models/RSP/voices), for 'voice_audio' on RSP Suno Cover or RSP Apply Voice."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"voice": ([item["id"] for item in voices.listing()], {"tooltip": "The voice's id. Music picks these by name."})}}

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("voice_audio",)
    FUNCTION = "load"
    CATEGORY = CATEGORY

    @classmethod
    def VALIDATE_INPUTS(cls, voice):
        try:
            return os.path.isfile(voices.path_of(voice)) or "That voice's file is gone."
        except (ValueError, OSError, KeyError):
            return "That voice is no longer in the library."

    def load(self, voice):
        from comfy_extras.nodes_audio import load as load_audio

        waveform, rate = load_audio(voices.path_of(voice))
        return ({"waveform": waveform.unsqueeze(0), "sample_rate": rate},)


class RSPApplyVoice:
    DESCRIPTION = "Replaces the voice in any finished song with another voice: splits it, converts the vocal, mixes it back."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "song": ("AUDIO",), "voice_audio": ("AUDIO", {"tooltip": "Singing in the wanted voice. Any band behind it is removed."}),
            "voice_strength": ("FLOAT", {**SLIDER, "default": 100.0, "tooltip": "100 = only this voice, 0 = only the song's."}),
            "voice_semitones": ("INT", {"default": 0, "min": -24, "max": 24}),
            "conversion_steps": ("INT", {"default": 40, "min": 4, "max": 100}),
        }}

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("song",)
    FUNCTION = "expand"
    CATEGORY = CATEGORY

    def expand(self, song, voice_audio, voice_strength, voice_semitones, conversion_steps):
        from comfy_execution.graph_utils import GraphBuilder

        graph = GraphBuilder()
        return {"result": (_voice_stages(graph, song, voice_audio, None, voice_strength, voice_semitones, conversion_steps)[0],), "expand": graph.finalize()}


def _voice_stages(graph, song, voice_audio, options, voice_strength, voice_semitones, conversion_steps):
    """(mix, converted vocal, band) of a song sung again in a voice."""
    extra = {"options": options} if options is not None else {}
    take_vocals = graph.node("RSPVocals", audio=song, **extra)
    band = graph.node("RSPSplitBand", mix=song, vocals=take_vocals.out(0))
    voice = graph.node("RSPVocals", audio=voice_audio, **extra)
    clean = graph.node("RSPCleanVoice", original=voice_audio, vocals=voice.out(0))
    reference = graph.node("RSPPickVoiceReference", vocals=clean.out(0), seconds=25.0)
    changed = graph.node("RSPVoiceConvert", vocals=take_vocals.out(0), reference=reference.out(0), steps=conversion_steps,
                         cfg_rate=0.7, semitones=voice_semitones, auto_pitch=False, match_range=True)
    mixed = graph.node("RSPMix", band=band.out(0), converted=changed.out(0), original_vocals=take_vocals.out(0),
                       voice_strength=voice_strength, vocal_gain_db=0.0)
    return mixed.out(0), changed.out(0), band.out(0)


WHISPER_MODELS = ["large-v3", "turbo", "medium", "small", "base", "tiny"]


def _whisper_segments(audio, word_timestamps, model="large-v3"):
    """Whisper on the recording's vocals, on the GPU."""
    import folder_paths
    import nodes as comfy_nodes

    transcriber = comfy_nodes.NODE_CLASS_MAPPINGS.get("BunnyTranscribe")
    if transcriber is None:
        raise RuntimeError("RSP's Whisper option needs the BunnyTranscribe custom node.")
    name = f"rsp_{uuid.uuid4().hex[:12]}"
    folder = os.path.join(folder_paths.get_input_directory(), "RSPRefs")
    os.makedirs(folder, exist_ok=True)
    source = os.path.join(folder, name + ".wav")
    result = os.path.join(folder_paths.get_output_directory(), "agentic-transcripts", name + ".json")
    try:
        _write_wav_44k_stereo(source, audio)
        transcriber().run(f"RSPRefs/{name}.wav", name, "auto", word_timestamps=word_timestamps, isolate_vocals=True, model=model)
        with open(result, encoding="utf-8") as handle:
            return json.load(handle).get("segments") or []
    finally:
        for path in (source, result):
            if os.path.exists(path):
                os.remove(path)


class RSPLyricTimings:
    DESCRIPTION = "When each written lyric word is sung: Whisper's timed words (GPU, vocals split first) matched against the lyrics, as JSON text."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",), "lyrics": ("STRING", {"multiline": True, "default": ""})},
                "optional": {"model": (WHISPER_MODELS, {"default": "large-v3"})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("timings",)
    FUNCTION = "align"
    CATEGORY = CATEGORY

    def align(self, audio, lyrics, model="large-v3"):
        segments = _whisper_segments(audio, word_timestamps=True, model=model)
        seconds = audio["waveform"].shape[-1] / float(audio["sample_rate"])
        return (json.dumps({"model": model, "lines": lyric_layout.align_words(lyrics, segments, seconds)}, separators=(",", ":")),)


class RSPWhisperLyrics:
    DESCRIPTION = "The song's words from Whisper large-v3 on the GPU (vocals split first, through Bunny Transcribe), laid under the score's section tags."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",), "score_abc": ("STRING", {"forceInput": True})},
                "optional": {"model": (WHISPER_MODELS, {"default": "large-v3"})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("lyrics",)
    FUNCTION = "transcribe"
    CATEGORY = CATEGORY

    def transcribe(self, audio, score_abc, model="large-v3"):
        segments = _whisper_segments(audio, word_timestamps=False, model=model)
        seconds = audio["waveform"].shape[-1] / float(audio["sample_rate"])
        words = lyric_layout.lay_words(score_abc, segments, seconds)
        if not words:
            raise RuntimeError("Whisper heard no words in this recording. Is it an instrumental?")
        return (words,)


class RSPSongFacts:
    DESCRIPTION = "What can be told about a recording without hearing it: its YouTube title and channel, length, loudness and brightness, as JSON text."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",)}, "optional": {"youtube_url": ("STRING", {"default": ""})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("facts",)
    FUNCTION = "facts"
    CATEGORY = CATEGORY

    def facts(self, audio, youtube_url=""):
        return (json.dumps({**dsp.sound_facts(audio), **_youtube_facts(youtube_url)}, ensure_ascii=False),)


def _youtube_facts(url: str) -> dict:
    """Title etc. from YouTube. A failure only costs the song name; the rest of the facts still work."""
    url = (url or "").strip()
    if not url:
        return {}
    try:
        import yt_dlp

        options = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "socket_timeout": 20}
        with yt_dlp.YoutubeDL(options) as client:
            info = client.extract_info(url, download=False) or {}
    except Exception as error:
        print(f"[RSP] couldn't read the YouTube details: {error}")
        return {}
    found = {"youtube_title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
             "track": info.get("track"), "artist": info.get("artist"), "album": info.get("album"),
             "release_year": info.get("release_year"), "tags": (info.get("tags") or [])[:12],
             "description": (info.get("description") or "")[:400]}
    return {key: value for key, value in found.items() if value}


class RSPSunoCover:
    DESCRIPTION = (
        "A Suno-style cover of a recording: YuE2 sings it again in your style, then the song's own singer "
        "(or the voice audio input) replaces YuE2's voice. Leave lyrics empty to use the song's own words. "
        "Save 'cover'; 'yue2_take' is the one YuE2 Edit Track can edit."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio": ("AUDIO", {"tooltip": "The song to cover."}),
                "style": ("STRING", {"multiline": True, "default": "", "tooltip": "Style of Music."}),
                "lyrics": ("STRING", {"multiline": True, "default": "", "tooltip": "Empty = recognise the song's words. Check them: recognition isn't exact."}),
                "seed": ("INT", {"default": 831001, "min": 0, "max": (1 << 63) - 1, "control_after_generate": True}),
                "audio_influence": ("FLOAT", {**SLIDER, "default": 75.0, "tooltip": AUDIO_TIP}),
                "style_influence": ("FLOAT", {**SLIDER, "default": 50.0, "tooltip": STYLE_TIP}),
                "weirdness": ("FLOAT", {**SLIDER, "default": 50.0, "tooltip": WEIRD_TIP}),
                "vocal_gender": (list(settings.GENDERS), {"default": "auto"}),
                "voice_source": (list(VOICE_SOURCES), {"default": VOICE_SOURCES[0]}),
                "voice_strength": ("FLOAT", {**SLIDER, "default": 100.0, "tooltip": "100 = only the kept voice, 0 = only YuE2's."}),
                "allow_downloads": ("BOOLEAN", {"default": False, "tooltip": "Let YuE2 fetch missing weights (several GB) during this run."}),
            },
            "optional": {
                "voice_audio": ("AUDIO", {"tooltip": "With 'voice audio input': a song or recording whose voice to use. Its band is removed."}),
                "lora": ("YUE2_LORA",),
                "transpose": ("INT", {"default": 0, "min": -12, "max": 12, "tooltip": "Moves the tune before singing: a new take in another key."}),
                "voice_semitones": ("INT", {"default": 0, "min": -24, "max": 24, "tooltip": "Moves the kept voice only."}),
                "conversion_steps": ("INT", {"default": 40, "min": 4, "max": 100}),
                "max_seconds": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 900.0, "step": 1.0, "tooltip": "0 = YuE2 sizes it."}),
                "low_vram": ("BOOLEAN", {"default": False}),
                "whisper": ("BOOLEAN", {"default": False, "tooltip": "With empty lyrics: Whisper large-v3 writes the song's words, instead of YuE2's own recognition."}),
                "whisper_model": (WHISPER_MODELS, {"default": "large-v3", "tooltip": "The Whisper model the whisper option uses."}),
            },
        }

    RETURN_TYPES = ("AUDIO", "AUDIO", "AUDIO", "AUDIO", "STRING", "STRING")
    RETURN_NAMES = ("cover", "yue2_take", "converted_vocal", "band", "score_abc", "lyrics")
    FUNCTION = "expand"
    CATEGORY = CATEGORY

    def expand(self, audio, style, lyrics, seed, audio_influence, style_influence, weirdness, vocal_gender, voice_source,
               voice_strength, allow_downloads, voice_audio=None, lora=None, transpose=0, voice_semitones=0,
               conversion_steps=40, max_seconds=0.0, low_vram=False, whisper=False, whisper_model="large-v3"):
        from comfy_execution.graph_utils import GraphBuilder

        if voice_source == "voice audio input" and voice_audio is None:
            raise ValueError("voice_source is 'voice audio input', but nothing is connected to voice_audio.")
        graph = GraphBuilder()
        return build_cover(graph, audio, style, lyrics, seed, audio_influence, style_influence, weirdness, vocal_gender,
                           voice_source, voice_strength, allow_downloads, voice_audio, lora, transpose, voice_semitones,
                           conversion_steps, max_seconds, low_vram, whisper, whisper_model)


def build_cover(graph, audio, style, lyrics, seed, audio_influence, style_influence, weirdness, vocal_gender, voice_source,
                voice_strength, allow_downloads, voice_audio, lora, transpose, voice_semitones, conversion_steps,
                max_seconds, low_vram, whisper=False, whisper_model="large-v3") -> dict:
    """The cover as a graph, so ComfyUI schedules, caches and cancels each stage on its own."""
    fast_singing.install()
    choices = settings.yue2_options(audio_influence, style_influence, weirdness, transpose=transpose, max_seconds=max_seconds,
                                    allow_downloads=allow_downloads, low_vram=low_vram)
    options = graph.node("YuE2Options", **choices)
    mode = choices["cot"]
    words = (lyrics or "").strip()
    score, sung_words = "", words
    if mode != "off" or not words:
        # The score's mode must equal cot. With cot off only the words are used.
        heard = graph.node("YuE2Transcribe", audio=audio, mode="melody" if mode == "off" else mode,
                           lyrics_auto_recognition=not words and not whisper, model="auto", seed=seed, options=options.out(0))
        if mode != "off":
            score = heard.out(0)
        if not words and whisper:
            sung_words = graph.node("RSPWhisperLyrics", audio=audio, score_abc=heard.out(0), model=whisper_model).out(0)
        elif not words:
            sung_words = heard.out(1)
    song_inputs = {"style": settings.style_text(style, vocal_gender), "lyrics": sung_words, "seed": seed, "options": options.out(0)}
    if score:
        song_inputs["score_abc"] = score
    if lora is not None:
        song_inputs["lora"] = lora
    song = graph.node("YuE2GenerateSong", **song_inputs)
    if voice_source == "off":
        take_vocals = graph.node("RSPVocals", audio=song.out(0), options=options.out(0))
        band = graph.node("RSPSplitBand", mix=song.out(0), vocals=take_vocals.out(0)).out(0)
        cover, converted = song.out(0), take_vocals.out(0)
    else:
        kept = voice_audio if voice_source == "voice audio input" else audio
        cover, converted, band = _voice_stages(graph, song.out(0), kept, options.out(0), voice_strength, voice_semitones, conversion_steps)
    return {"result": (cover, song.out(0), converted, band, song.out(1), sung_words), "expand": graph.finalize()}


NODE_CLASS_MAPPINGS = {
    "RSPSunoCover": RSPSunoCover,
    "RSPVocals": RSPVocals,
    "RSPPickVoiceReference": RSPPickVoiceReference,
    "RSPCleanVoice": RSPCleanVoice,
    "RSPVoiceConvert": RSPVoiceConvert,
    "RSPSplitBand": RSPSplitBand,
    "RSPMix": RSPMix,
    "RSPSongFacts": RSPSongFacts,
    "RSPCustomVoice": RSPCustomVoice,
    "RSPApplyVoice": RSPApplyVoice,
    "RSPWhisperLyrics": RSPWhisperLyrics,
    "RSPLyricTimings": RSPLyricTimings,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "RSPSunoCover": "RSP Suno Cover",
    "RSPVocals": "RSP Vocals (GPU)",
    "RSPPickVoiceReference": "RSP Pick Voice Reference",
    "RSPCleanVoice": "RSP Clean Voice",
    "RSPVoiceConvert": "RSP Voice Convert (GPU)",
    "RSPSplitBand": "RSP Split Band",
    "RSPMix": "RSP Mix",
    "RSPSongFacts": "RSP Song Facts",
    "RSPCustomVoice": "RSP Custom Voice",
    "RSPApplyVoice": "RSP Apply Voice",
    "RSPWhisperLyrics": "RSP Whisper Lyrics (GPU)",
    "RSPLyricTimings": "RSP Lyric Timings (GPU)",
}
