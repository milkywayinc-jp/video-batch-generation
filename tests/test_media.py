import array
import copy
import math
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from videobatch.common import BatchError, RATE, read_json
from videobatch.media import (adjust_audio, pcm, save_pcm, silence_intervals, validate_batches,
                              ffmpeg, prepare_media, render_media, mix_pcm, probe)
from videobatch.sheets import analyze_sheet, edit_template
from videobatch.rules import cut_rules, Timebase
from test_sheets import fixture, put
from support import resolved, fonts


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required for real media processing')
class AudioTests(unittest.TestCase):
    def test_mix_refuses_irreversible_pcm_clipping(self):
        with self.assertRaisesRegex(BatchError, 'clip'):
            mix_pcm(array.array('h', [30000]).tobytes(), array.array('h', [30000]).tobytes(), 0)
        self.assertEqual(array.array('h', mix_pcm(array.array('h', [1000]).tobytes(),
                                               array.array('h', [500]).tobytes(), -20))[0], 1050)

    def test_sheet_speed_and_pause_change_actual_audio_and_provenance(self):
        snap = fixture()
        put(snap, 6, 8, {})
        raw_plan = analyze_sheet(snap, snap['sheets'][0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tone = array.array('h', (int(7000 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(RATE)))
            raw = bytes(RATE // 5 * 2) + tone.tobytes() + bytes(RATE * 2) + tone.tobytes() + bytes(RATE // 2 * 2)
            save_pcm(root / 'raw.wav', raw)
            lengths = []
            for index, speed in enumerate((1.0, 1.5)):
                plan = resolved(raw_plan, speed=speed)
                result = adjust_audio(plan, 'batch-001', root / 'raw.wav', root / f'adjusted-{index}')
                path = Path(result['audio_path'])
                count = len(pcm(path)) // 2
                lengths.append(count)
                self.assertEqual(silence_intervals(path, 0.5), [])
                artifact = read_json(result['artifact_path'])
                self.assertEqual(artifact['speed'], speed)
                self.assertEqual(sum(i['output_end'] - i['output_start'] for i in artifact['kept_intervals']), count)
                edit = {'batches': [{'id': 'batch-001', 'path': str(path), 'boundaries': [0, count], 'verified': True}]}
                rows, _ = validate_batches(plan, edit)
                self.assertEqual(rows[4], pcm(path))
                other = resolved(raw_plan, speed=1.2)
                with self.assertRaisesRegex(BatchError, 'provenance'):
                    validate_batches(other, edit)
            self.assertGreater(lengths[0], lengths[1] * 1.4)


@unittest.skipUnless(os.environ.get('VIDEO_BATCH_TEST_FONT'), 'VIDEO_BATCH_TEST_FONT required')
class VideoTests(unittest.TestCase):
    def raw_source_plan(self, bgm=False):
        snap = fixture()
        for row in (4, 5, 6):
            put(snap, row, 9, {'stringValue': '動画の音声をそのまま使用'})
            put(snap, row, 1, {'stringValue': '工程\n検証'})
            if bgm:
                put(snap, row, 10, {'stringValue': 'bgm.wav'})
        return analyze_sheet(snap, snap['sheets'][0])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_sheet_dimensions_fps_tail_styles_and_loudness_drive_real_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / 'analytic.mp4'
            ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=30:duration=6',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=6',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', video])
            outputs = []
            for index, params in enumerate((dict(width=320, height=320, fps='30', tail=1, target=-18),
                                            dict(width=240, height=426, fps='24', tail=0, target=-21,
                                                 size=20, fill='yellow'))):
                plan = resolved(self.raw_source_plan(), **params)
                edit = edit_template(plan)
                edit['fonts'] = fonts(os.environ['VIDEO_BATCH_TEST_FONT'])
                for item, start in zip(edit['cuts'], (0, 1, 2)):
                    item.update(video_path=str(video), source_verified=True,
                                source_intervals=[[start + 0.01, start + 0.49]])
                result = prepare_media(plan, edit, root / f'prepared-{index}')
                prepared = read_json(result['prepared'])
                tb = Timebase(params['fps'])
                signal = array.array('h', pcm(prepared['audio_path']))
                self.assertEqual(len(signal), tb.sample(prepared['total_frames']))
                if prepared['tail_frames']:
                    tail_samples = tb.samples(prepared['total_frames'] - prepared['tail_frames'], prepared['tail_frames'])
                    self.assertTrue(all(s == 0 for s in signal[-tail_samples:]))
                else:
                    self.assertTrue(any(signal[-1000:]))
                output = render_media(plan, result['prepared'], root / f'render-{index}', local_only=True)
                spec = probe(output['video_path'])['streams'][0]
                self.assertEqual((spec['width'], spec['height']), (params['width'], params['height']))
                self.assertEqual(spec['r_frame_rate'], tb.text)
                self.assertLessEqual(abs(float(output['loudness']['input_i']) - params['target']), 0.3)
                self.assertLessEqual(float(output['loudness']['input_tp']), -1)
                self.assertFalse(output['production_complete'])
                outputs.append(output)
            self.assertNotEqual(outputs[0]['resolved_rules_hash'], outputs[1]['resolved_rules_hash'])
            self.assertNotEqual(outputs[0]['tail_frames'], outputs[1]['tail_frames'])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_fractional_fps_and_continuous_bgm_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video, bgm = root / 'analytic.mp4', root / 'bgm.wav'
            ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=30:duration=5',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=5',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', video])
            save_pcm(bgm, array.array('h', ((i // 1000) % 100 for i in range(4 * RATE))).tobytes())
            plan = resolved(self.raw_source_plan(bgm=True), fps='30000/1001', tail=0.2)
            edit = edit_template(plan)
            edit['fonts'] = fonts(os.environ['VIDEO_BATCH_TEST_FONT'])
            for item, start in zip(edit['cuts'], (0, 1, 2)):
                item.update(video_path=str(video), source_verified=True, source_intervals=[[start, start + 0.45]], bgm_path=str(bgm))
            result = prepare_media(plan, edit, root / 'prepared')
            prepared = read_json(result['prepared'])
            self.assertEqual(prepared['cuts'][1]['bgm_start_sample'], prepared['cuts'][0]['speech_samples'])
            tb = Timebase('30000/1001')
            self.assertEqual(len(pcm(prepared['audio_path'])) // 2, tb.sample(prepared['total_frames']))
            output = render_media(plan, result['prepared'], root / 'render', local_only=True)
            self.assertEqual(output['video_spec']['fps'], '30000/1001')
            short = root / 'short.wav'
            save_pcm(short, bytes(RATE // 10 * 2))
            edit['cuts'][0]['bgm_path'] = str(short)
            with self.assertRaisesRegex(BatchError, 'too short'):
                prepare_media(plan, edit, root / 'rejected')

    def test_caption_block_height_stays_fixed_while_width_is_compressed(self):
        from PIL import Image
        from videobatch.typography import overlay, load_font, text_image
        plan = resolved(self.raw_source_plan())
        cr = cut_rules(plan['resolved_rules'], 4)
        with tempfile.TemporaryDirectory() as directory:
            boxes = []
            for i, text in enumerate(('文文', '文' * 30, '文\n文\n文')):
                path = Path(directory) / f'{i}.png'
                overlay({'caption': text, 'note1': '', 'note2': ''}, cr, fonts(os.environ['VIDEO_BATCH_TEST_FONT']), path)
                boxes.append(Image.open(path).getbbox())
            self.assertEqual(boxes[0][3] - boxes[0][1], boxes[1][3] - boxes[1][1])
            for box in boxes:
                self.assertLessEqual(abs((box[1] + box[3]) / 2 - cr['styles']['caption']['y']), 2)
            regular = text_image('文', load_font(os.environ['VIDEO_BATCH_TEST_FONT'], 24, 400), 'white', 'black', 0, 0, 'left')
            black = text_image('文', load_font(os.environ['VIDEO_BATCH_TEST_FONT'], 24, 900), 'white', 'black', 0, 0, 'left')
            self.assertGreater(sum(black.getchannel('A').getdata()), sum(regular.getchannel('A').getdata()))

    def test_individual_full_width_band_and_glow_are_visible(self):
        from PIL import Image
        from videobatch.typography import overlay
        cr = cut_rules(resolved(self.raw_source_plan())['resolved_rules'], 4)
        style = cr['styles']['caption']
        style.update(band=dict(fill='#202020',opacity=0.65,pad_x=0,pad_y=8,full_width=True),
                     glow=dict(color='red',radius=6,opacity=0.7),
                     shadow=dict(color='black',radius=2,opacity=0.4,dx=0,dy=2))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'effects.png'
            overlay({'caption':'文','note1':'','note2':''},cr,fonts(os.environ['VIDEO_BATCH_TEST_FONT']),path)
            im = Image.open(path)
            self.assertEqual(im.getpixel((0,round(style['y']))), (32,32,32,166))
            self.assertTrue(any(r > g + 30 and a > 20 for r,g,b,a in im.getdata()))


if __name__ == '__main__':
    unittest.main()
