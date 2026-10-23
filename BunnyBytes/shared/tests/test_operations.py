from __future__ import annotations

import math
import subprocess
import tempfile
import unittest
from pathlib import Path

from operations import MediaError, _crop_rect, _window, probe, transform


class MediaOperationsTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)
        self.video = self.root / "input.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2", "-c:v", "mpeg4",
            "-c:a", "aac", str(self.video),
        ], check=True)

    def tearDown(self):
        self.scratch.cleanup()

    def test_probe_trim_crop_extract_and_thumbnail(self):
        info = probe(self.video)
        self.assertAlmostEqual(info["duration"], 2, places=1)
        self.assertEqual({s["codec_type"] for s in info["streams"]}, {"video", "audio"})

        trimmed = self.root / "trimmed.mp4"
        transform(self.video, trimmed, "trim", start=0.3, end=1.3)
        self.assertAlmostEqual(probe(trimmed)["duration"], 1, delta=0.2)

        cropped = self.root / "cropped.mp4"
        transform(self.video, cropped, "crop", x=0.25, y=0.25, width=0.5, height=0.5)
        cropped_video = next(s for s in probe(cropped)["streams"] if s["codec_type"] == "video")
        self.assertEqual((cropped_video["width"], cropped_video["height"]), (32, 24))

        audio = self.root / "audio.m4a"
        transform(self.video, audio, "extract-audio")
        self.assertEqual({s["codec_type"] for s in probe(audio)["streams"]}, {"audio"})

        thumb = self.root / "thumb.jpg"
        transform(self.video, thumb, "thumbnail", at=0.5)
        self.assertGreater(thumb.stat().st_size, 0)

    def test_invalid_windows_and_crops(self):
        for start, end in ((-1, 1), (2, 1), (0, math.inf), (0, 9999)):
            with self.subTest(start=start, end=end), self.assertRaises(MediaError):
                _window(start, end, 2)
        for rect in ((0, 0, 2, 1), (0, 0, 0, 1), (math.nan, 0, 1, 1)):
            with self.subTest(rect=rect), self.assertRaises(MediaError):
                _crop_rect(*rect, 64, 48)

    def test_image_crop(self):
        image = self.root / "input.png"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=blue:s=64x48", "-frames:v", "1", str(image)], check=True)
        output = self.root / "cropped.png"
        transform(image, output, "crop", x=0.25, y=0.25, width=0.5, height=0.5)
        video = next(s for s in probe(output)["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (32, 24))


if __name__ == "__main__":
    unittest.main()
