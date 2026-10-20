import array
import math
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from videobatch.common import BatchError, RATE, digest, read_json
from videobatch.media import (adjust_audio, pcm, save_pcm, silence_intervals, validate_batches,
                              ffmpeg, prepare_media, render_media, mix_pcm)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for actual audio-processing tests")
class AudioTests(unittest.TestCase):
    def test_mix_refuses_irreversible_pcm_clipping(self):
        with self.assertRaisesRegex(BatchError, "clip"):
            mix_pcm(array.array("h", [30000]).tobytes(), array.array("h", [30000]).tobytes(), 0)
        self.assertEqual(array.array("h", mix_pcm(array.array("h", [1000]).tobytes(),
                                               array.array("h", [500]).tobytes(), -20))[0], 1050)

    def test_speed_silence_edit_and_boundary_coverage(self):
        # Analytic signal tests transformations; it is not a claimed speech alignment test.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tone = array.array("h", (int(7000 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(RATE)))
            raw = bytes(RATE // 5 * 2) + tone.tobytes() + bytes(RATE * 2) + tone.tobytes() + bytes(RATE // 2 * 2)
            save_pcm(root / "raw.wav", raw)
            plan = {"batches": [{"id": "batch-001", "text_hash": digest("synthetic"), "rows": [4, 5]}]}
            result = adjust_audio(plan, "batch-001", root / "raw.wav", root / "adjusted")
            path = Path(result["audio_path"])
            count = len(pcm(path)) // 2
            self.assertGreater(count, RATE)
            self.assertLess(count, 2 * RATE + RATE // 2)
            self.assertEqual(silence_intervals(path, 0.5), [])
            artifact = read_json(result["artifact_path"])
            self.assertEqual(sum(i["output_end"] - i["output_start"] for i in artifact["kept_intervals"]), count)
            edit = {"batches": [{"id": "batch-001", "path": str(path),
                                  "boundaries": [0, count // 2, count], "verified": True}]}
            rows, _ = validate_batches(plan, edit)
            self.assertEqual(rows[4] + rows[5], pcm(path))
            edit["batches"][0]["boundaries"][0] = 1
            with self.assertRaisesRegex(BatchError, "partition"):
                validate_batches(plan, edit)


@unittest.skipUnless(os.environ.get("VIDEO_BATCH_TEST_FONT"), "Set VIDEO_BATCH_TEST_FONT to a local Noto Sans JP variable font")
class TypographyTests(unittest.TestCase):
    def test_height_and_block_center_survive_horizontal_compression(self):
        from PIL import Image
        from videobatch.typography import overlay, load_font, text_image
        font = os.environ["VIDEO_BATCH_TEST_FONT"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, caption in [("short", "文" * 4), ("wide", "文" * 30), ("three", "文\n文\n文")]:
                overlay({"caption": caption, "note1": "", "note2": ""}, {}, font, root / f"{name}.png")
            boxes = {name: Image.open(root / f"{name}.png").getbbox() for name in ("short", "wide", "three")}
            self.assertEqual(boxes["short"][3] - boxes["short"][1], boxes["wide"][3] - boxes["wide"][1])
            self.assertLessEqual(boxes["wide"][2] - boxes["wide"][0], 1024)
            for box in boxes.values():
                self.assertLessEqual(abs((box[1] + box[3]) / 2 - 810), 2)
            regular = text_image("文", load_font(font, 85, 400), "white", "black", 0, 0, "left")
            black = text_image("文", load_font(font, 85, 900), "white", "black", 0, 0, "left")
            self.assertGreater(sum(black.getchannel("A").getdata()), sum(regular.getchannel("A").getdata()))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_linked_source_edit_final_tail_and_aac_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "analytic.mp4"
            bgm = root / "bgm.wav"
            save_pcm(bgm, array.array("h", ((i // 1000) % 100 for i in range(2 * RATE))).tobytes())
            ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", video])
            cuts = [{"row": r, "caption": "工程\n検証", "speech": "", "material": "",
                     "audio_mode": "source", "note1": "", "note2": "", "bgm": "bgm.wav",
                     "directives": {"caption": "", "cut": "", "audio": "動画の音声をそのまま使用"}}
                    for r in (4, 5)]
            plan = {"schema_version": 1, "source_hash": digest("analytic-fixture"),
                    "sheet_id": 123, "sheet_name": "★analytic", "cuts": cuts, "batches": []}
            edit = {"source_hash": plan["source_hash"], "settings_reviewed": True,
                    "font_path": os.environ["VIDEO_BATCH_TEST_FONT"], "batches": [],
                    "cuts": [{"row": r, "directives_reviewed": True, "source_verified": True,
                              "source_intervals": [[start + (0.01 if r == 5 else 0), start + 0.5 - (0.01 if r == 5 else 0)]],
                              "video_path": str(video), "bgm_path": str(bgm), "bgm_gain_db": -20}
                             for r, start in [(4, 0), (5, 1)]]}
            result = prepare_media(plan, edit, root / "prepared")
            prepared = read_json(result["prepared"])
            self.assertEqual([c["frames"] for c in prepared["cuts"]], [15, 75])
            self.assertEqual(prepared["total_frames"], 90)
            self.assertEqual([c["bgm_start_sample"] for c in prepared["cuts"]], [0, RATE // 2])
            original = array.array("h", pcm(root / "prepared/row-5-level.wav"))
            mixed = array.array("h", pcm(root / "prepared/row-5-mix.wav"))
            expected_bgm = array.array("h", pcm(root / "prepared/bgm-0.wav"))[RATE // 2:RATE]
            self.assertEqual(list(mixed), [round(a + b * 0.1) for a, b in zip(original, expected_bgm)])
            self.assertEqual(prepared["cuts"][1]["video_intervals"],
                             [{"start": 1, "frames": 15}, {"start": 1.5, "frames": 60, "silent_tail": True}])
            signal = array.array("h", pcm(prepared["audio_path"]))
            self.assertTrue(all(s == 0 for s in signal[-2 * RATE:]))
            crossings = sum(a * b < 0 for a, b in zip(signal[:RATE * 2 // 5], signal[1:RATE * 2 // 5]))
            self.assertLessEqual(abs(crossings - 352), 5)  # Source voice is not accelerated to 1.1x.
            output = render_media(plan, result["prepared"], root / "render", local_only=True)
            self.assertEqual(output["total_frames"], 90)
            self.assertFalse(output["production_complete"])
            self.assertEqual(output["writeback"], "not_applied")
            self.assertLessEqual(float(output["loudness"]["input_tp"]), -1)
            self.assertGreaterEqual(output["decoded_aac_samples"], 90 * 1600)

            short_bgm = root / "short.m4a"
            ffmpeg(["-f", "lavfi", "-i", "sine=frequency=200:duration=0.2", "-c:a", "aac", short_bgm])
            edit["cuts"][0]["bgm_path"] = str(short_bgm)
            with self.assertRaisesRegex(BatchError, "too short"):
                prepare_media(plan, edit, root / "rejected-short-bgm")

    def test_caption_override_moves_center_and_keeps_color(self):
        from PIL import Image
        from videobatch.typography import overlay
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "override.png"
            overlay({"caption": "文", "note1": "", "note2": ""},
                    {"caption_style": {"center_y": 600, "fill": "yellow"}},
                    os.environ["VIDEO_BATCH_TEST_FONT"], path)
            im = Image.open(path)
            box = im.getbbox()
            self.assertLess(abs((box[1] + box[3]) / 2 - 600), 2)
            self.assertTrue(any(r > 240 and g > 240 and b < 20 and a > 240 for r, g, b, a in im.getdata()))


if __name__ == "__main__":
    unittest.main()
