from __future__ import annotations

import array
import json
import math
import re
import wave
from pathlib import Path

from .common import (BatchError, RATE, digest, file_hash, new_directory,
                     positive, read_json, require, run, save_json, validate_prepared)
from .rules import Timebase, cut_rules, validate_rules


def check_live(plan):
    if plan.get("source_kind") == "live-gws":
        from .sheets import find_sheet, live_snapshot, require_plan_unchanged
        require_plan_unchanged(plan, find_sheet(live_snapshot(plan["spreadsheet_id"], plan["sheet_id"]), plan["sheet_id"]))


def ffmpeg(args, verbose=False):
    return run(["ffmpeg", "-hide_banner", "-loglevel", "info" if verbose else "error",
                "-nostdin", "-n", *args])


def probe(path, count=False):
    args = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json"]
    if count:
        args.append("-count_frames")
    return json.loads(run([*args, path]).stdout)


def video_duration(info):
    stream = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    require(stream is not None, "No video stream")
    duration = float(stream.get("duration", info.get("format", {}).get("duration", 0)))
    positive(duration, "video duration")
    return duration


def pcm(path):
    with wave.open(str(path), "rb") as f:
        require(f.getframerate() == RATE and f.getnchannels() == 1 and f.getsampwidth() == 2,
                "Expected 48kHz mono signed 16-bit PCM WAV")
        data = f.readframes(f.getnframes())
    return data


def save_pcm(path, data):
    require(len(data) % 2 == 0, "PCM byte length must be even")
    with wave.open(str(path), "wb") as f:
        f.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        f.writeframes(data)


def silence_intervals(path, minimum=0.01):
    result = ffmpeg(["-i", path, "-af", f"silencedetect=n=-50dB:d={minimum}",
                     "-f", "null", "-"], verbose=True)
    duration = len(pcm(path)) // 2
    events = re.findall(r"silence_(start|end):\s*([0-9.eE+-]+)", result.stderr.decode(errors="replace"))
    intervals = []
    start = None
    for kind, raw in events:
        sample = min(duration, max(0, round(float(raw) * RATE)))
        if kind == "start":
            start = sample
        elif start is not None:
            intervals.append((start, sample))
            start = None
    if start is not None:
        intervals.append((start, duration))
    return intervals


def adjust_audio(plan, batch_id, input_path, out_dir):
    rules = validate_rules(plan)
    check_live(plan)
    batch = next((b for b in plan["batches"] if b["id"] == batch_id), None)
    require(batch is not None, "Unknown narration batch")
    audio = batch["audio_rules"]
    source = Path(input_path).expanduser().resolve(strict=True)
    out = new_directory(out_dir)
    tempo = out / "tempo.wav"
    ffmpeg(["-i", source, "-vn", "-af", f"atempo={audio['speed']}", "-ac", "1", "-ar", str(RATE),
            "-c:a", "pcm_s16le", tempo])
    raw = pcm(tempo)
    length = len(raw) // 2
    require(length > 0, "Decoded narration is empty")
    remove = []
    for start, end in silence_intervals(tempo):
        if start < int(0.01 * RATE) and audio["trim_edges"]:
            remove.append((0, end))
        elif end >= length - int(0.01 * RATE) and audio["trim_edges"]:
            remove.append((start, length))
        elif audio["max_pause_seconds"] is not None and end - start >= audio["max_pause_seconds"] * RATE:
            keep = round(audio["retained_pause_seconds"] * RATE)
            remove.append((start + keep // 2, end - (keep - keep // 2)))
    kept, cursor, output_cursor = [], 0, 0
    parts = []
    for start, end in sorted(remove) + [(length, length)]:
        if start > cursor:
            parts.append(raw[cursor * 2:start * 2])
            kept.append({"tempo_start": cursor, "tempo_end": start,
                         "output_start": output_cursor, "output_end": output_cursor + start - cursor})
            output_cursor += start - cursor
        cursor = max(cursor, end)
    adjusted = b"".join(parts)
    require(adjusted, "Narration is entirely below the silence threshold")
    path = out / "adjusted.wav"
    save_pcm(path, adjusted)
    require(audio["max_pause_seconds"] is None or not silence_intervals(path, audio["max_pause_seconds"]),
            "Adjusted batch exceeds the sheet's maximum pause")
    artifact = {"schema_version": 2, "batch_id": batch_id, "text_hash": batch["text_hash"],
                "resolved_rules_hash": rules["integrity"], "audio_rules": audio,
                "raw_path": str(source), "raw_hash": file_hash(source), "speed": audio["speed"],
                "tempo_hash": file_hash(tempo), "tempo_samples": length,
                "kept_intervals": kept, "removed_tempo_intervals": remove,
                "adjusted_hash": file_hash(path), "samples": len(adjusted) // 2,
                "sample_rate": RATE, "boundary_verification": "required-on-adjusted-audio"}
    save_json(out / "adjusted.audio.json", artifact)
    return {"audio_path": str(path), "artifact_path": str(out / "adjusted.audio.json"),
            "samples": len(adjusted) // 2, "next": "Listen and record verified row boundaries in the edit manifest"}


def validate_batches(plan, edit):
    rules = validate_rules(plan)
    expected = {b["id"]: b for b in plan["batches"]}
    require(len(edit["batches"]) == len(expected) and
            len({b["id"] for b in edit["batches"]}) == len(expected), "Batch IDs differ from plan")
    rows, dependencies = {}, {}
    for decision in edit["batches"]:
        require(decision["id"] in expected, "Unknown batch in edit decisions")
        batch = expected[decision["id"]]
        require(decision["verified"] is True, f"{batch['id']}: row boundaries need listening verification")
        path = Path(decision["path"]).expanduser().resolve(strict=True)
        artifact_path = path.with_suffix(".audio.json")
        artifact = read_json(artifact_path)
        data = pcm(path)
        require(artifact["batch_id"] == batch["id"] and artifact["text_hash"] == batch["text_hash"]
                and artifact.get("schema_version") == 2 and artifact["audio_rules"] == batch["audio_rules"]
                and artifact["resolved_rules_hash"] == rules["integrity"]
                and artifact["adjusted_hash"] == file_hash(path)
                and artifact["samples"] == len(data) // 2, "Adjusted audio provenance mismatch")
        boundaries = decision["boundaries"]
        require(len(boundaries) == len(batch["rows"]) + 1 and boundaries[0] == 0
                and boundaries[-1] == len(data) // 2
                and all(isinstance(x, int) and not isinstance(x, bool) for x in boundaries)
                and all(a < b for a, b in zip(boundaries, boundaries[1:])),
                "Boundaries must partition all adjusted samples exactly once, in script order")
        for row, start, end in zip(batch["rows"], boundaries, boundaries[1:]):
            rows[row] = data[start * 2:end * 2]
        dependencies[str(path)] = file_hash(path)
        dependencies[str(artifact_path)] = file_hash(artifact_path)
    return rows, dependencies


def motion_start(path, seconds, source_duration):
    require(source_duration + 1e-6 >= seconds, f"Insufficient video: need {seconds:.3f}s, have {source_duration:.3f}s")
    result = ffmpeg(["-i", path, "-vf", "fps=4,scale=32:32,format=gray", "-an", "-f", "rawvideo", "-"])
    frames = [result.stdout[i:i + 1024] for i in range(0, len(result.stdout) - 1023, 1024)]
    if len(frames) < 2:
        return 0.0
    scores = [sum(abs(a - b) for a, b in zip(x, y)) / 1024 for x, y in zip(frames, frames[1:])]
    window = max(1, math.ceil(seconds * 4))
    prefix = [0.0]
    for score in scores:
        prefix.append(prefix[-1] + score)
    max_index = max(0, math.floor((source_duration - seconds) * 4))
    choices = [(prefix[min(len(scores), i + window)] - prefix[min(i, len(scores))], i)
               for i in range(min(max_index + 1, len(scores)))]
    return max(choices, key=lambda x: (x[0], -x[1]))[1] / 4 if choices else 0.0


def resolve_video(cut, decision, edit):
    explicit = decision.get("video_path")
    if explicit:
        path = Path(explicit).expanduser()
    else:
        require(cut["material"] and edit.get("material_root"), f"Row {cut['row']}: material not assigned yet")
        path = Path(edit["material_root"]).expanduser() / cut["material"]
    return path.resolve(strict=True)


def level_pcm(path, out, audio):
    data = pcm(path)
    samples = array.array("h", data)
    active = [s for s in samples if abs(s) > 104]  # about -50dBFS
    if not active or not audio["level_match"]:
        save_pcm(out, data)
        return {"gain_db": 0, "silent": not bool(active), "level_match": audio["level_match"]}
    rms = math.sqrt(sum(s * s for s in active) / len(active)) / 32768
    peak = max(abs(s) for s in samples) / 32768
    gain = min(audio["loudness_lufs"] - 20 * math.log10(rms),
               audio["peak_dbtp"] - 2 - 20 * math.log10(peak))
    ffmpeg(["-i", path, "-af", f"volume={gain:.6f}dB", "-c:a", "pcm_s16le", out])
    require(len(pcm(out)) == len(data), "Level matching changed duration")
    return {"gain_db": gain, "silent": False, "method": "active-rms-with-peak-headroom; listening-QA-required"}


def loudness(path):
    # These parameters only request EBU measurements; this command writes no media.
    result = ffmpeg(["-i", path, "-vn", "-af", "loudnorm=I=-24:TP=-1:LRA=7:print_format=json",
                     "-f", "null", "-"], verbose=True)
    matches = re.findall(r'\{\s*"input_i".*?\}', result.stderr.decode(errors="replace"), re.S)
    require(matches, "Unable to measure integrated loudness")
    return json.loads(matches[-1])


def normalize_timeline(path, out, frames, tb, tail_frames, audio):
    measure = loudness(path)
    silent = measure["input_i"] == "-inf"
    attempts = []
    exact = tb.sample(frames) * 2
    tail_bytes = tb.samples(frames - tail_frames, tail_frames) * 2
    target, peak = audio["loudness_lufs"], max(-9, audio["peak_dbtp"] - 2)

    def finalize(candidate):
        data = pcm(candidate)
        require(abs(len(data) - exact) <= math.ceil(RATE / tb.fps) * 2, "Normalization duration drift exceeds one frame")
        data = data[:exact].ljust(exact, b"\0")
        # Tail is owned by the timeline. Zero tail must not slice away audio.
        if tail_bytes:
            data = data[:-tail_bytes] + bytes(tail_bytes)
        save_pcm(candidate, data)
        return loudness(candidate)

    def acceptable(m):
        return math.isfinite(float(m["input_i"])) and abs(float(m["input_i"]) - target) <= 0.2 \
            and float(m["input_tp"]) <= peak + 0.4

    if silent:
        save_pcm(out, pcm(path))
        after = finalize(out)
    else:
        candidate = out.with_name(out.stem + ".initial.wav")
        filt = (f"loudnorm=I={target}:TP={peak}:LRA=7:linear=false:"
                f"measured_I={measure['input_i']}:measured_LRA={measure['input_lra']}:"
                f"measured_TP={measure['input_tp']}:measured_thresh={measure['input_thresh']}:"
                f"offset={measure['target_offset']},aresample={RATE}")
        ffmpeg(["-i", path, "-af", filt, "-ar", str(RATE), "-c:a", "pcm_s16le", candidate])
        after = finalize(candidate)
        attempts.append({"method": "two-pass-loudnorm", "measurement": after})
        if not acceptable(after):
            # A high requested loudness with peak headroom may need compression.
            compressed = out.with_name(out.stem + ".compressed.wav")
            ffmpeg(["-i", path, "-af", "acompressor=threshold=0.01:ratio=10:attack=0.01:release=5:detection=peak:knee=1",
                    "-ar", str(RATE), "-c:a", "pcm_s16le", compressed])
            candidate = compressed
            for index in range(6):
                current = loudness(candidate)
                require(math.isfinite(float(current["input_i"])), "Compression produced no measurable audio")
                gain = target - float(current["input_i"])
                limited = out.with_name(out.stem + f".limited-{index}.wav")
                ffmpeg(["-i", candidate, "-af",
                        f"aresample=192000,volume={gain:.6f}dB,alimiter=limit={10 ** (peak / 20)}:level=0:latency=1:attack=0.1:release=1,aresample={RATE}",
                        "-ar", str(RATE), "-c:a", "pcm_s16le", limited])
                candidate = limited
                after = finalize(candidate)
                attempts.append({"method": "compression-and-lookahead-limiting", "gain_db": gain, "measurement": after})
                if acceptable(after):
                    break
        require(acceptable(after), "Unable to reach loudness/peak target; inspect mastering attempts")
        save_pcm(out, pcm(candidate))
    return {"before": measure, "after": after, "silent": silent, "attempts": attempts}


def prepare_media(plan, edit, out_dir):
    rules = validate_rules(plan)
    check_live(plan)
    require(edit.get("schema_version") == 2 and edit["source_hash"] == plan["source_hash"]
            and edit["resolved_rules_hash"] == rules["integrity"], "Edit decisions refer to different rules")
    require([c["row"] for c in edit["cuts"]] == [c["row"] for c in plan["cuts"]], "Edit cut rows differ from plan")
    # Rule values may only come from resolved-rules, never edit overrides.
    allowed = {"row", "video_path", "video_start", "source_intervals", "source_verified", "silent_frames", "bgm_path"}
    require(all(set(c) <= allowed for c in edit["cuts"]), "Remove production rule overrides from edit")
    from .typography import load_font, font_file
    fonts = edit["fonts"]
    narration, dependencies = validate_batches(plan, edit)
    tb = Timebase(rules["values"]["video"]["fps"])
    tail = tb.frames(rules["values"]["audio"]["tail_seconds"])
    errors, resolved = [], []
    bgm_previous, bgm_offset, frame_cursor = None, 0, 0
    for cut, decision in zip(plan["cuts"], edit["cuts"]):
        try:
            cr = cut_rules(rules, cut["row"])
            for field in ("caption", "note1", "note2"):
                if cut[field]:
                    style = cr["styles"][field]
                    font = Path(font_file(fonts, style)).expanduser().resolve(strict=True)
                    load_font(font, style["size"], style["weight"])
                    dependencies[str(font)] = file_hash(font)
            path = resolve_video(cut, decision, edit)
            info = probe(path)
            duration = video_duration(info)
            dependencies[str(path)] = file_hash(path)
            if cr["mode"] == "source":
                require(decision["source_verified"] is True and decision["source_intervals"],
                        "Verify source speech and linked edit intervals")
                require(any(s["codec_type"] == "audio" for s in info["streams"]), "Source audio stream missing")
                intervals, previous_end = [], -1.0
                for start, end in decision["source_intervals"]:
                    require(isinstance(start, (int, float)) and not isinstance(start, bool)
                            and math.isfinite(start) and start >= 0, "Invalid source start")
                    positive(end - start, "source interval duration")
                    first = math.floor(start * tb.fps + 1e-7)
                    last = math.ceil(end * tb.fps - 1e-7)
                    qs, qe = tb.seconds(first), tb.seconds(last)
                    require(last > first and qs >= previous_end and qe <= duration + 1e-6,
                            "Source intervals overlap, are out of order, or exceed video")
                    intervals.append({"start": qs, "frames": last - first})
                    previous_end = qe
                speech_frames = sum(i["frames"] for i in intervals)
            elif cr["mode"] == "narration":
                speech_frames = tb.cover_samples(frame_cursor, len(narration[cut["row"]]) // 2)
                intervals = None
            else:
                speech_frames = decision["silent_frames"]
                require(isinstance(speech_frames, int) and not isinstance(speech_frames, bool) and speech_frames > 0,
                        "Silent cut needs an explicit measured silent_frames")
                intervals = None
            row_tail = tail if cut is plan["cuts"][-1] else 0
            frames = speech_frames + row_tail
            if intervals is None:
                start = decision.get("video_start")
                if start is None:
                    require(cr["material"]["selection"] == "motion", "Sheet requires manual video start")
                    start = motion_start(path, tb.seconds(frames), duration)
                require(isinstance(start, (int, float)) and not isinstance(start, bool) and math.isfinite(start)
                        and start >= 0 and start + tb.seconds(frames) <= duration + 1e-6,
                        "Video is too short for its complete measured cut")
                intervals = [{"start": start, "frames": frames}]
            elif row_tail:
                tail_start = intervals[-1]["start"] + tb.seconds(intervals[-1]["frames"])
                require(tail_start + tb.seconds(row_tail) <= duration + 1e-6,
                        "Final source video lacks the sheet's requested tail")
                intervals.append({"start": tail_start, "frames": row_tail, "silent_tail": True})
            bgm = decision.get("bgm_path", "")
            require(bool(cut["bgm"]) == bool(bgm), "BGM assignment must match the sheet")
            if bgm:
                bgm = str(Path(bgm).expanduser().resolve(strict=True))
                if bgm != bgm_previous:
                    bgm_offset = 0
                require(any(s["codec_type"] == "audio" for s in probe(bgm)["streams"]), "BGM has no audio")
                dependencies[bgm] = file_hash(bgm)
            speech_samples = tb.samples(frame_cursor, speech_frames)
            resolved.append({"row": cut["row"], "frames": frames, "first_frame": frame_cursor,
                             "speech_frames": speech_frames, "speech_samples": speech_samples,
                             "video_path": str(path), "video_intervals": intervals,
                             "bgm_path": bgm, "bgm_start_sample": bgm_offset if bgm else 0})
            bgm_previous = bgm or None
            bgm_offset = bgm_offset + speech_samples if bgm else 0
            frame_cursor += frames
        except (BatchError, OSError, ValueError, KeyError) as exc:
            errors.append(f"Row {cut['row']}: {exc}")
    require(not errors, "Material/decision errors:\n" + "\n".join(errors))
    out = new_directory(out_dir)
    bgm_cache = {}
    for index, path in enumerate(dict.fromkeys(i["bgm_path"] for i in resolved if i["bgm_path"])):
        decoded = out / f"bgm-{index}.wav"
        ffmpeg(["-i", path, "-vn", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", decoded])
        bgm_cache[path] = pcm(decoded)
    short = [i["row"] for i in resolved if i["bgm_path"] and
             len(bgm_cache[i["bgm_path"]]) // 2 < i["bgm_start_sample"] + i["speech_samples"]]
    require(not short, f"Decoded BGM is too short at rows {short}; no implicit padding/looping")
    timeline = bytearray()
    for cut, item in zip(plan["cuts"], resolved):
        row = cut["row"]
        cr = cut_rules(rules, row)
        if cr["mode"] == "narration":
            data = narration[row]
        elif cr["mode"] == "source":
            pieces, interval_cursor = [], item["first_frame"]
            for index, interval in enumerate(item["video_intervals"]):
                if interval.get("silent_tail"):
                    continue
                audio_path = out / f"row-{row}-source-{index}.wav"
                ffmpeg(["-ss", str(interval["start"]), "-i", item["video_path"], "-vn", "-t",
                        str(tb.seconds(interval["frames"])), "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", audio_path])
                part = pcm(audio_path)
                expected = tb.samples(interval_cursor, interval["frames"]) * 2
                require(abs(len(part) - expected) <= math.ceil(RATE / tb.fps) * 2,
                        "Source audio is shorter than linked video")
                pieces.append(part[:expected].ljust(expected, b"\0"))
                interval_cursor += interval["frames"]
            data = b"".join(pieces)
        else:
            data = bytes(item["speech_samples"] * 2)
        exact_length = item["speech_samples"] * 2
        require(len(data) <= exact_length, "Row audio exceeds measured frame duration")
        item["quantization_samples"] = (exact_length - len(data)) // 2
        data = data.ljust(exact_length, b"\0")
        unit = out / f"row-{row}.wav"
        save_pcm(unit, data)
        pause = cr["audio"]["max_pause_seconds"]
        if cr["mode"] != "silence" and pause is not None:
            require(not silence_intervals(unit, pause), f"Row {row}: audio exceeds sheet maximum pause")
        level = out / f"row-{row}-level.wav"
        item["level_match"] = level_pcm(unit, level, cr["audio"])
        data = pcm(level)
        if item["bgm_path"]:
            start = item["bgm_start_sample"] * 2
            data = mix_pcm(data, bgm_cache[item["bgm_path"]][start:start + exact_length], cr["bgm_gain_db"])
            save_pcm(out / f"row-{row}-mix.wav", data)
        if item is resolved[-1]:
            data += bytes(tb.samples(item["first_frame"] + item["speech_frames"], tail) * 2)
        require(len(data) // 2 == tb.samples(item["first_frame"], item["frames"]), "Row sample/frame mismatch")
        timeline.extend(data)
    total = sum(c["frames"] for c in resolved)
    save_pcm(out / "timeline.wav", timeline)
    normalization = normalize_timeline(out / "timeline.wav", out / "timeline-normalized.wav", total,
                                       tb, tail, rules["values"]["audio"])
    prepared = {"schema_version": 2, "source_hash": plan["source_hash"], "plan_hash": digest(plan),
                "resolved_rules_hash": rules["integrity"], "fps": tb.text,
                "sheet_id": plan["sheet_id"], "sheet_name": plan["sheet_name"],
                "cuts": resolved, "total_frames": total, "sample_rate": RATE,
                "tail_frames": tail, "audio_path": str(out / "timeline-normalized.wav"),
                "audio_hash": file_hash(out / "timeline-normalized.wav"),
                "fonts": fonts, "dependencies": dependencies, "normalization": normalization,
                "status": "timing_ready", "production_complete": False}
    prepared["integrity"] = digest(prepared)
    save_json(out / "prepared.json", prepared)
    return {"prepared": str(out / "prepared.json"), "total_frames": total,
            "seconds": tb.seconds(total), "sheet_writes": 0}


def frame_count(path):
    stream = next(s for s in probe(path, count=True)["streams"] if s["codec_type"] == "video")
    return int(stream["nb_read_frames"])


def concat_file(path, names):
    # Filenames are generated numeric names, never arbitrary script text.
    require(all(re.fullmatch(r"[A-Za-z0-9_.-]+", n) for n in names), "Unsafe intermediate filename")
    path.write_text("".join(f"file '{name}'\n" for name in names), encoding="utf-8")


def render_media(plan, prepared_path, out_dir, receipt=None, local_only=False):
    rules = validate_rules(plan)
    video_rules, audio_rules = rules["values"]["video"], rules["values"]["audio"]
    tb = Timebase(video_rules["fps"])
    tail = tb.frames(audio_rules["tail_seconds"])
    prepared = read_json(prepared_path)
    validate_prepared(plan, prepared)
    require(prepared["source_hash"] == plan["source_hash"] and prepared["status"] == "timing_ready",
            "Prepared media does not match plan")
    require([c["row"] for c in prepared["cuts"]] == [c["row"] for c in plan["cuts"]]
            and prepared["total_frames"] == sum(c["frames"] for c in prepared["cuts"])
            and prepared["tail_frames"] == tail, "Prepared timeline was modified inconsistently")
    require(file_hash(prepared["audio_path"]) == prepared["audio_hash"], "Prepared audio changed")
    require(all(Path(p).is_file() and file_hash(p) == h for p, h in prepared["dependencies"].items()),
            "A media/font/adjustment dependency changed; prepare again")
    if local_only:
        check_live(plan)
    else:
        require(receipt, "Confirmed writeback receipt or explicit --local-only is required")
        journal = read_json(receipt)
        require(journal["status"] == "confirmed" and journal["source_hash"] == plan["source_hash"]
                and journal["prepared_hash"] == file_hash(prepared_path), "Writeback receipt does not match timing")
        from .sheets import entered, entered_cells, find_sheet, live_snapshot, restore_entered, require_plan_unchanged
        current = find_sheet(live_snapshot(plan["spreadsheet_id"], plan["sheet_id"]), plan["sheet_id"])
        current_cells = entered_cells(current)
        require(all(entered(current_cells, *map(int, k.split(":"))) == v for k, v in journal["writes"].items()),
                "Sheet timing changed after writeback")
        restore_entered(current, journal["before"])
        require_plan_unchanged(plan, current)
    out = new_directory(out_dir)
    from .typography import overlay
    cut_files = []
    for cut, item in zip(plan["cuts"], prepared["cuts"]):
        row = cut["row"]
        png = out / f"row-{row}.png"
        overlay(cut, cut_rules(rules, row), prepared["fonts"], png)
        pieces = []
        for index, interval in enumerate(item["video_intervals"]):
            piece = out / f"row-{row}-{index}.mp4"
            w, h = video_rules["width"], video_rules["height"]
            from PIL import ImageColor
            rgb = ImageColor.getrgb(video_rules["background"])[:3]
            color = "0x" + "".join(f"{c:02x}" for c in rgb)
            fit = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}" if video_rules["fit"] == "cover"
                   else f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:{color}")
            ffmpeg(["-ss", str(interval["start"]), "-i", item["video_path"],
                    "-vf", f"{fit},fps={tb.text},setsar=1",
                    "-frames:v", str(interval["frames"]), "-an", "-c:v", "libx264", "-preset", "fast",
                    "-crf", "18", "-pix_fmt", "yuv420p", piece])
            require(frame_count(piece) == interval["frames"], f"Row {row}: decoded video is shorter than needed; no looping")
            pieces.append(piece.name)
        listing = out / f"row-{row}.concat.txt"
        concat_file(listing, pieces)
        chunk = out / f"row-{row}-caption.mp4"
        ffmpeg(["-f", "concat", "-safe", "1", "-i", listing, "-i", png,
                "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto[v]", "-map", "[v]",
                "-an", "-frames:v", str(item["frames"]), "-c:v", "libx264", "-preset", "fast",
                "-crf", "18", "-pix_fmt", "yuv420p", chunk])
        require(frame_count(chunk) == item["frames"], "Caption composition changed frame count")
        cut_files.append(chunk.name)
    listing = out / "all.concat.txt"
    concat_file(listing, cut_files)
    name = plan["sheet_name"] if rules["values"]["naming"]["video"] == "sheet" else plan["sheet_name"].replace("★", "")
    require(name not in (".", "..") and not any(c in name for c in ("/", "\\", "\0")), "Unsafe sheet filename")
    partial = out / "validation.partial.mp4"
    ffmpeg(["-f", "concat", "-safe", "1", "-i", listing, "-i", prepared["audio_path"],
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-ar", str(RATE), "-movflags", "+faststart", partial])
    info = probe(partial, count=True)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    require((video["width"], video["height"], video["r_frame_rate"], video["codec_name"], audio["codec_name"])
            == (video_rules["width"], video_rules["height"], tb.text, "h264", "aac"), "Export specification mismatch")
    require(int(video["nb_read_frames"]) == prepared["total_frames"], "Final video differs from H column frame total")
    require(abs(float(info["format"]["duration"]) - tb.seconds(prepared["total_frames"])) <= tb.seconds(1) + 2048 / RATE,
            "Container duration differs beyond frame/AAC tolerance")
    measure = loudness(partial)
    if not prepared["normalization"]["silent"]:
        require(math.isfinite(float(measure["input_i"])) and abs(float(measure["input_i"]) - audio_rules["loudness_lufs"]) <= 0.3,
                f"AAC loudness target missed: {measure['input_i']} LUFS (target {audio_rules['loudness_lufs']} ±0.3)")
        require(float(measure["input_tp"]) <= audio_rules["peak_dbtp"], f"AAC true peak exceeds sheet limit: {measure['input_tp']}")
    decoded = out / "decoded-aac.wav"
    ffmpeg(["-i", partial, "-vn", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", decoded])
    decoded_data = pcm(decoded)
    expected_samples = tb.sample(prepared["total_frames"])
    require(expected_samples <= len(decoded_data) // 2 <= expected_samples + 2048,
            "Decoded AAC does not cover the complete measured timeline")
    tail_start = tb.sample(prepared["total_frames"] - tail + min(1, tail))
    tail_data = array.array("h", decoded_data[tail_start * 2:expected_samples * 2])
    require(len(tail_data) == expected_samples - tail_start, "Decoded tail duration mismatch")
    if tail > 1:
        require(tail_data and max(abs(s) for s in tail_data) <= 33,
                "AAC tail is not silent below -60dBFS after one-frame encoder tolerance")
    ffmpeg(["-i", partial, "-f", "null", "-"])
    final = out / f"{name}.mp4"
    partial.rename(final)
    report = {"status": "local_validation_passed" if local_only else "media_qa_passed",
              "video_path": str(final), "total_frames": prepared["total_frames"],
              "seconds": tb.seconds(prepared["total_frames"]), "loudness": measure,
              "video_spec": video_rules, "resolved_rules_hash": rules["integrity"],
              "tail_frames": tail, "tail_encoder_tolerance_frames": min(1, tail),
              "aac_tail_samples_checked": len(tail_data),
              "decoded_aac_samples": len(decoded_data) // 2,
              "writeback": "not_applied" if local_only else "confirmed_before_render",
              "requires_visual_and_listening_review": True, "production_complete": False,
              "source_hash": plan["source_hash"], "file_hash": file_hash(final)}
    save_json(out / "validation.json", report)
    return report


def mix_pcm(voice, bgm, gain_db):
    require(len(voice) == len(bgm), "BGM must cover the exact row sample range")
    gain = 10 ** (gain_db / 20)
    require(math.isfinite(gain), "Invalid BGM gain")
    samples = [a + b * gain for a, b in zip(array.array("h", voice), array.array("h", bgm))]
    require(all(-32768 <= s <= 32767 for s in samples),
            "BGM mix would clip before mastering; reduce bgm_gain_db")
    return array.array("h", (round(s) for s in samples)).tobytes()
