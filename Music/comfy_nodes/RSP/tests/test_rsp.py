"""No GPU and no models: settings, audio arithmetic and the expanded graph.

Run: ComfyUI/.venv/bin/python -m pytest custom_nodes/RSP/tests  (from ComfyUI's folder)
"""
import math
import os
import sys

import numpy as np
import pytest
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from rsp import dsp, fast_singing, lyrics, nodes, settings, voices  # noqa: E402

fast_singing.install = lambda: None     # graph tests have no YuE2 pack loaded


def audio(wave, rate=48000):
    return {"waveform": wave, "sample_rate": rate}


def sine(seconds, rate=48000, level=0.5, channels=2):
    t = torch.arange(int(seconds * rate)) / rate
    return (level * torch.sin(2 * math.pi * 220 * t)).expand(1, channels, -1).clone()


@pytest.mark.parametrize("value,mode", [(100, "full"), (70, "full"), (69, "melody"), (35, "melody"), (34, "off"), (0, "off")])
def test_audio_bands(value, mode):
    assert settings.audio_mode(value) == mode


def test_fifty_percent_is_yue2_default():
    assert settings.cfg_scale(50) == 0.0
    assert settings.sampling(50) == {"temperature": 1.0, "top_p": 0.95, "top_k": 100, "repetition_penalty": 1.2}


def test_sliders_rise_and_clamp():
    assert settings.cfg_scale(0) == 0.5 and settings.cfg_scale(100) == 3.0
    assert settings.sampling(-5) == settings.sampling(0)
    assert settings.sampling(0)["temperature"] < settings.sampling(100)["temperature"]


def test_options_rules():
    off = settings.yue2_options(10, 50, 50, transpose=3)
    assert off["cot"] == "off" and off["transpose"] == 0     # YuE2 refuses transpose with no score
    assert off["download"] == "off"
    assert settings.yue2_options(90, 50, 50, allow_downloads=True)["download"] == "auto"


def test_gender_goes_in_the_style():
    assert settings.style_text("jazz", "female") == "jazz, female vocal"
    assert settings.style_text("jazz", "auto") == "jazz"


def test_band_plus_vocals_is_the_mix():
    mix, vocals = audio(sine(2)), audio(sine(2, level=0.2))
    band = dsp.band_of(mix, vocals)
    assert torch.allclose(band["waveform"] + vocals["waveform"], mix["waveform"], atol=1e-6)


def test_mix_matches_rate_channels_length_and_level():
    band = audio(torch.zeros(1, 2, 96000))
    original = audio(sine(2, level=0.4))
    converted = audio(sine(1.5, rate=44100, level=0.05, channels=1), 44100)
    out = dsp.mix_cover(band, converted, original)
    assert out["sample_rate"] == 48000 and out["waveform"].shape == (1, 2, 96000)
    sung = out["waveform"][..., :60000]
    assert sung.pow(2).mean().sqrt().item() == pytest.approx(original["waveform"].pow(2).mean().sqrt().item(), rel=0.1)


def test_voice_strength_ends():
    band = audio(torch.zeros(1, 2, 48000))
    original = audio(sine(1, level=0.3))
    converted = audio(-sine(1, level=0.3))
    assert torch.allclose(dsp.mix_cover(band, converted, original, 0)["waveform"], original["waveform"], atol=1e-5)
    assert torch.allclose(dsp.mix_cover(band, converted, original, 100)["waveform"], converted["waveform"], atol=1e-3)


def test_mix_never_clips():
    out = dsp.mix_cover(audio(sine(1, level=0.9)), audio(sine(1, level=0.9)), audio(sine(1, level=0.9)))
    assert out["waveform"].abs().max() <= 0.99 + 1e-6


def test_reference_finds_the_singing():
    wave = torch.zeros(1, 2, 48000 * 60)
    wave[..., 48000 * 30:48000 * 50] = sine(20)
    picked = dsp.pick_reference(audio(wave), 10)
    assert picked["waveform"].shape[-1] == 48000 * 10
    assert picked["waveform"].abs().mean(dim=(0, 1)).min() >= 0 and picked["waveform"].pow(2).mean() > 0.1


def test_reference_refuses_silence():
    with pytest.raises(dsp.NoSinging):
        dsp.pick_reference(audio(torch.zeros(1, 2, 48000 * 30)))


class FakeNode:
    def __init__(self, cls, inputs):
        self.cls, self.inputs = cls, inputs

    def out(self, index):
        return (self, index)


class FakeGraph:
    def __init__(self):
        self.nodes = []

    def node(self, cls, **inputs):
        self.nodes.append(FakeNode(cls, inputs))
        return self.nodes[-1]

    def finalize(self):
        return {}

    def find(self, cls):
        return [node for node in self.nodes if node.cls == cls]


def cover(graph, source="SONG", lyrics="", influence=75, voice="source singer", voice_audio=None):
    return nodes.build_cover(graph, source, "jazz", lyrics, 1, influence, 50, 50, "auto", voice, 100, False,
                             voice_audio, None, 0, 0, 40, 0.0, False)


def test_default_cover_clones_the_source_singer():
    graph = FakeGraph()
    result = cover(graph)["result"]
    assert graph.find("YuE2Transcribe")[0].inputs["mode"] == "full"
    assert graph.find("YuE2Transcribe")[0].inputs["lyrics_auto_recognition"] is True
    assert graph.find("YuE2GenerateSong")[0].inputs["score_abc"][1] == 0
    assert graph.find("RSPVocals")[1].inputs["audio"] == "SONG"
    assert result[0][0].cls == "RSPMix" and result[1][0].cls == "YuE2GenerateSong"


def test_low_influence_with_lyrics_skips_the_score():
    graph = FakeGraph()
    cover(graph, lyrics="words", influence=10)
    assert not graph.find("YuE2Transcribe")
    assert "score_abc" not in graph.find("YuE2GenerateSong")[0].inputs


def test_low_influence_without_lyrics_only_takes_the_words():
    graph = FakeGraph()
    cover(graph, influence=10)
    song = graph.find("YuE2GenerateSong")[0].inputs
    assert "score_abc" not in song and song["lyrics"][1] == 1


def test_voice_off_returns_the_take():
    graph = FakeGraph()
    result = cover(graph, voice="off")["result"]
    assert not graph.find("RSPVoiceConvert") and result[0][0].cls == "YuE2GenerateSong"


def test_real_graph_builder_expands():
    from comfy_execution.graph_utils import GraphBuilder

    song = audio(sine(1))
    expanded = cover(GraphBuilder(), source=song)
    classes = {node["class_type"] for node in expanded["expand"].values()}
    assert classes == {"YuE2Options", "YuE2Transcribe", "YuE2GenerateSong", "RSPVocals", "RSPSplitBand",
                       "RSPCleanVoice", "RSPPickVoiceReference", "RSPVoiceConvert", "RSPMix"}
    assert len(expanded["result"]) == len(nodes.RSPSunoCover.RETURN_TYPES)
    for node in expanded["expand"].values():
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in expanded["expand"]


def test_sound_facts():
    facts = dsp.sound_facts(audio(sine(5, level=0.5)))
    assert facts["seconds"] == 5.0
    assert facts["brightness_hz"] == pytest.approx(220, abs=30)
    assert facts["loudness_db"] == pytest.approx(-9, abs=1.5)


def test_youtube_details_are_optional():
    assert nodes._youtube_facts("") == {}


SCORE = """X:1
Q:1/4=120
K:Dm
% intro
V: Vocal
"Dm"z16|z16|
V: Ins
Z2|
% verse
V: Vocal
"Dm"a4g4f4e4|a4g4f4e4|
% chorus
V: Vocal
a4g4f4e4|a4g4f4e4|
"""


def test_sections_from_score():
    assert lyrics.score_sections(SCORE) == [{"tag": "intro", "bars": 2, "sung": False}, {"tag": "verse", "bars": 2, "sung": True},
                                            {"tag": "chorus", "bars": 2, "sung": True}]


def test_words_land_in_their_sections():
    segments = [{"start": 3, "end": 4, "text": "first"}, {"start": 8, "end": 9, "text": "second"},
                {"start": 1, "end": 2, "text": "ghost", "no_speech_prob": 0.9}]
    assert lyrics.lay_words(SCORE, segments, 12.0) == "[Verse]\nfirst\n\n[Chorus]\nsecond"


def test_words_over_an_instrumental_go_to_the_nearest_sung_section():
    assert lyrics.lay_words(SCORE, [{"start": 0, "end": 1, "text": "early"}], 12.0) == "[Verse]\nearly"


def test_no_sections_still_gives_lyrics():
    assert lyrics.lay_words("", [{"start": 0, "end": 1, "text": "hi"}], 5.0) == "[Verse]\nhi"
    assert lyrics.lay_words(SCORE, [], 12.0) == ""


def test_whisper_cover_uses_the_whisper_node():
    graph = FakeGraph()
    nodes.build_cover(graph, "SONG", "jazz", "", 1, 75, 50, 50, "auto", "off", 100, False, None, None, 0, 0, 40, 0.0, False, True)
    assert graph.find("YuE2Transcribe")[0].inputs["lyrics_auto_recognition"] is False
    assert graph.find("YuE2GenerateSong")[0].inputs["lyrics"][0].cls == "RSPWhisperLyrics"


def test_voice_library(tmp_path):
    base = str(tmp_path)
    item = voices.add("  My   Singer ", "take.MP3", b"data", base)
    assert item["name"] == "My Singer" and voices.path_of(item["id"], base).endswith("voice.mp3")
    assert [row["id"] for row in voices.listing(base)] == [item["id"]]
    voices.remove(item["id"], base)
    assert voices.listing(base) == []


@pytest.mark.parametrize("name,filename,data", [("", "a.mp3", b"x"), ("a", "a.exe", b"x"), ("a", "a.mp3", b"")])
def test_voice_library_refuses(tmp_path, name, filename, data):
    with pytest.raises(ValueError):
        voices.add(name, filename, data, str(tmp_path))


@pytest.mark.parametrize("bad", ["../../etc", "", "ABC", "a" * 15])
def test_voice_ids_cannot_leave_the_library(tmp_path, bad):
    with pytest.raises(ValueError):
        voices.remove(bad, str(tmp_path))
    with pytest.raises(ValueError):
        voices.path_of(bad, str(tmp_path))


def test_other_voice_audio():
    graph = FakeGraph()
    cover(graph, voice="voice audio input", voice_audio="OTHER")
    assert graph.find("RSPVocals")[1].inputs["audio"] == "OTHER"


def test_voiced_regions_skip_silence_and_keep_short_rests():
    from rsp import seedvc_fast as fast

    second = fast.SR
    wave = np.zeros(second * 10, dtype=np.float32)
    wave[second:second * 2] = wave[int(second * 2.5):second * 3] = 0.5      # a 0.5 s rest: stays inside
    wave[second * 7:second * 8] = 0.5                                       # after 4 s of silence: its own region
    regions = fast.voiced_regions(wave, len(wave) // fast.HOP, gap_seconds=0.75)
    assert len(regions) == 2
    per_second = fast.SR / fast.HOP
    assert regions[0][0] < per_second and 3 * per_second <= regions[0][1] < 3.5 * per_second
    assert 6.5 * per_second < regions[1][0] < 7 * per_second
    assert fast.voiced_regions(np.zeros(second, dtype=np.float32), 80) == []


def test_chunks_cover_the_region_with_overlap():
    from rsp import seedvc_fast as fast

    parts = fast.chunks(100, 5000, 1719)
    assert parts[0][0] == 100 and parts[-1][1] == 5000
    assert all(after[0] == before[1] - fast.OVERLAP_FRAMES for before, after in zip(parts, parts[1:]))
    assert fast.chunks(10, 20, 1719) == [(10, 20)]


def test_place_crossfades_and_stays_in_bounds():
    from rsp import seedvc_fast as fast

    out = np.ones(100, dtype=np.float32)
    fast.place(out, np.zeros(200, dtype=np.float32), 50, fade=10)
    assert out[49] == 1.0 and out[-1] == 0.0 and 0.0 < out[55] < 1.0


def test_loudest_stretch():
    from rsp import seedvc_fast as fast

    wave = np.zeros(fast.SR * 25, dtype=np.float32)
    wave[fast.SR * 12:fast.SR * 22] = 0.5
    assert fast.loudest_stretch(wave, 10).min() == 0.5


def test_clean_voice_keeps_a_clean_recording():
    rate = 8000
    voice = torch.sin(torch.linspace(0, 2000, rate * 2)).reshape(1, 1, -1) * 0.5
    band = torch.sin(torch.linspace(0, 700, rate * 2)).reshape(1, 1, -1) * 0.3
    clean = {"waveform": voice, "sample_rate": rate}
    separated = {"waveform": voice * 0.99, "sample_rate": rate}
    assert dsp.clean_voice(clean, separated) is clean
    with_band = {"waveform": voice + band, "sample_rate": rate}
    assert dsp.clean_voice(with_band, clean) is clean


def test_octave_semitones_keeps_the_key():
    from rsp import seedvc_fast as fast

    female, male = np.full(50, 440.0), np.full(50, 220.0)
    assert fast.octave_semitones(female, male) == -12
    assert fast.octave_semitones(male, female) == 12
    assert fast.octave_semitones(female, np.full(50, 330.0)) == 0      # a fifth apart: no octave nearer
    assert fast.octave_semitones(np.zeros(50), male) == 0


def test_yue2_uses_a_quick_attention():
    assert settings.yue2_options(90, 50, 50)["attention_backend"] in ("flash", "fast")


def test_short_rests_do_not_split_by_default():
    from rsp import seedvc_fast as fast

    wave = np.zeros(fast.SR * 40, dtype=np.float32)
    wave[:fast.SR] = wave[fast.SR * 6:fast.SR * 7] = wave[fast.SR * 30:fast.SR * 31] = 0.5
    assert len(fast.voiced_regions(wave, len(wave) // fast.HOP)) == 2


@pytest.mark.parametrize("frames", [1, 100, 215, 216, 300, 1719])
def test_vocoder_pieces_share_one_shape_and_cover_everything(frames):
    from rsp import seedvc_fast as fast

    shapes = set()

    def vocoder(mel):
        shapes.add(mel.shape)
        return torch.ones(1, 1, mel.size(2) * fast.HOP)

    wave = fast.vocode(vocoder, torch.zeros(1, 128, frames))
    assert shapes == {(1, 128, fast.VOCODER_FRAMES)}
    assert len(wave) == frames * fast.HOP and np.allclose(wave, 1.0)


def test_sung_lines_with_high_no_speech_prob_stay():
    sung = {"start": 0, "end": 1, "text": "sung", "no_speech_prob": 0.87, "avg_logprob": -0.4}
    noise = {"start": 1, "end": 2, "text": "noise", "no_speech_prob": 0.87, "avg_logprob": -1.4}
    assert lyrics.clean_lines([sung, noise]) == [sung]


def _heard(*words):
    return [{"text": " ".join(w for w, _, _ in words), "words": [{"word": w, "start": s, "end": e} for w, s, e in words]}]


def test_align_follows_written_lines_and_skips_tags():
    timed = lyrics.align_words("[Verse]\nHello, big world\n\n[Chorus] sing it out",
                               _heard(("hello", 1, 2), ("big", 2, 3), ("world", 3, 4), ("sing", 20, 21), ("it", 21, 22), ("out", 22, 23)), 30)
    assert timed == [[[1, 2], [2, 3], [3, 4]], [[20, 21], [21, 22], [22, 23]]]


def test_align_missed_word_stays_by_its_neighbour_across_a_break():
    timed = lyrics.align_words("one two three\nfour five six", _heard(("one", 1, 2), ("two", 2, 3), ("three", 3, 4), ("five", 60, 61), ("six", 61, 62)), 90)
    assert timed[1][0] == [59.4, 60]    # led into "five", not spread over the 56 s gap
    assert timed[1][1] == [60, 61]


def test_align_refuses_lyrics_it_cannot_hear():
    with pytest.raises(RuntimeError, match="too few"):
        lyrics.align_words("nothing here matches at all today", _heard(("other", 1, 2)), 10)


def test_align_times_never_run_backwards():
    timed = lyrics.align_words("a b c d", _heard(("a", 5, 6), ("b", 4, 5), ("c", 7, 8), ("d", 8, 9)), 10)
    flat = [word[0] for line in timed for word in line]
    assert flat == sorted(flat)


def test_whisper_cover_passes_the_chosen_model():
    from rsp import nodes as rsp_nodes
    import inspect
    assert inspect.signature(rsp_nodes.build_cover).parameters["whisper_model"].default == "large-v3"
    assert rsp_nodes.RSPLyricTimings.INPUT_TYPES()["optional"]["model"][0] == rsp_nodes.WHISPER_MODELS
    assert rsp_nodes.RSPWhisperLyrics.INPUT_TYPES()["optional"]["model"][0] == rsp_nodes.WHISPER_MODELS
