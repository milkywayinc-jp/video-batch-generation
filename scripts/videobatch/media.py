from __future__ import annotations

import array
import json
import math
import re
import wave
from pathlib import Path

from .common import (BatchError, FPS, RATE, SPF, digest, file_hash, new_directory,
                     positive, read_json, require, run, save_json, validate_prepared)


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
    batch = next((b for b in plan["batches"] if b["id"] == batch_id), None)
    require(batch is not None, "Unknown narration batch")
    source = Path(input_path).expanduser().resolve(strict=True)
    out = new_directory(out_dir)
    tempo = out / "tempo.wav"
    ffmpeg(["-i", source, "-vn", "-af", "atempo=1.1", "-ac", "1", "-ar", str(RATE),
            "-c:a", "pcm_s16le", tempo])
    raw = pcm(tempo)
    length = len(raw) // 2
    require(length > 0, "Decoded narration is empty")
    remove = []
    for start, end in silence_intervals(tempo):
        if start < int(0.01 * RATE):
            remove.append((0, end))
        elif end >= length - int(0.01 * RATE):
            remove.append((start, length))
        elif end - start >= RATE // 2:
            # Keep 100ms of a long internal pause, with 50ms on either edge.
            remove.append((start + RATE // 20, end - RATE // 20))
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
    require(not silence_intervals(path, 0.5), "Adjusted batch still has a pause of at least 0.5 seconds")
    artifact = {"schema_version": 1, "batch_id": batch_id, "text_hash": batch["text_hash"],
                "raw_path": str(source), "raw_hash": file_hash(source), "speed": 1.1,
                "tempo_hash": file_hash(tempo), "tempo_samples": length,
                "kept_intervals": kept, "removed_tempo_intervals": remove,
                "adjusted_hash": file_hash(path), "samples": len(adjusted) // 2,
                "sample_rate": RATE, "boundary_verification": "required-on-adjusted-audio"}
    save_json(out / "adjusted.audio.json", artifact)
    return {"audio_path": str(path), "artifact_path": str(out / "adjusted.audio.json"),
            "samples": len(adjusted) // 2, "next": "Listen and record verified row boundaries in the edit manifest"}


def validate_batches(plan, edit):
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
                and artifact["speed"] == 1.1 and artifact["adjusted_hash"] == file_hash(path)
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


def level_pcm(path, out):
    data = pcm(path)
    samples = array.array("h", data)
    active = [s for s in samples if abs(s) > 104]  # about -50dBFS
    if not active:
        save_pcm(out, data)
        return {"gain_db": 0, "silent": True}
    rms = math.sqrt(sum(s * s for s in active) / len(active)) / 32768
    peak = max(abs(s) for s in samples) / 32768
    gain = min(-20 - 20 * math.log10(rms), -3 - 20 * math.log10(peak))
    ffmpeg(["-i", path, "-af", f"volume={gain:.6f}dB", "-c:a", "pcm_s16le", out])
    require(len(pcm(out)) == len(data), "Level matching changed duration")
    return {"gain_db": gain, "silent": False, "method": "active-rms-with-peak-headroom; listening-QA-required"}


def loudness(path):
    result = ffmpeg(["-i", path, "-vn", "-af", "loudnorm=I=-7.2:TP=-3:LRA=7:print_format=json",
                     "-f", "null", "-"], verbose=True)
    matches = re.findall(r'\{\s*"input_i".*?\}', result.stderr.decode(errors="replace"), re.S)
    require(matches, "Unable to measure integrated loudness")
    return json.loads(matches[-1])


def normalize_timeline(path, out, frames):
    measure = loudness(path)
    silent = measure["input_i"] == "-inf"
    attempts = []
    exact = frames * SPF * 2

    def finalize(candidate):
        data = pcm(candidate)
        require(abs(len(data) - exact) <= SPF * 2, "Normalization duration drift exceeds one frame")
        data = data[:exact].ljust(exact, b"\0")
        # The 60 tail frames are already owned by the timeline, not added here.
        data = data[:-60 * SPF * 2] + bytes(60 * SPF * 2)
        save_pcm(candidate, data)
        return loudness(candidate)

    def acceptable(m):
        return math.isfinite(float(m["input_i"])) and abs(float(m["input_i"]) + 7.2) <= 0.2 \
            and float(m["input_tp"]) <= -2.6

    if silent:
        save_pcm(out, pcm(path))
        after = finalize(out)
    else:
        candidate = out.with_name(out.stem + ".initial.wav")
        filt = ("loudnorm=I=-7.2:TP=-3:LRA=7:linear=false:"
                f"measured_I={measure['input_i']}:measured_LRA={measure['input_lra']}:"
                f"measured_TP={measure['input_tp']}:measured_thresh={measure['input_thresh']}:"
                f"offset={measure['target_offset']},aresample={RATE}")
        ffmpeg(["-i", path, "-af", filt, "-ar", str(RATE), "-c:a", "pcm_s16le", candidate])
        after = finalize(candidate)
        attempts.append({"method": "two-pass-loudnorm", "measurement": after})
        if not acceptable(after):
            # A -7.2 LUFS target with peak headroom can require reducing crest factor.
            compressed = out.with_name(out.stem + ".compressed.wav")
            ffmpeg(["-i", path, "-af", "acompressor=threshold=0.01:ratio=10:attack=0.01:release=5:detection=peak:knee=1",
                    "-ar", str(RATE), "-c:a", "pcm_s16le", compressed])
            candidate = compressed
            for index in range(6):
                current = loudness(candidate)
                require(math.isfinite(float(current["input_i"])), "Compression produced no measurable audio")
                gain = -7.2 - float(current["input_i"])
                limited = out.with_name(out.stem + f".limited-{index}.wav")
                ffmpeg(["-i", candidate, "-af",
                        f"aresample=192000,volume={gain:.6f}dB,alimiter=limit={10 ** (-3 / 20)}:level=0:latency=1:attack=0.1:release=1,aresample=48000",
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
    require(edit["source_hash"] == plan["source_hash"], "Edit decisions refer to another sheet snapshot")
    require(edit["settings_reviewed"] is True, "Read common settings and confirm settings_reviewed in edit manifest")
    require([c["row"] for c in edit["cuts"]] == [c["row"] for c in plan["cuts"]], "Edit cut rows differ from plan")
    font = Path(edit["font_path"]).expanduser().resolve(strict=True)
    from .typography import load_font
    load_font(font, 28, 400)
    load_font(font, 85, 900)
    narration, dependencies = validate_batches(plan, edit)
    dependencies[str(font)] = file_hash(font)
    errors, resolved = [], []
    bgm_previous, bgm_offset = None, 0.0
    for cut, decision in zip(plan["cuts"], edit["cuts"]):
        try:
            require(decision["directives_reviewed"] is True, f"Row {cut['row']}: individual directives unresolved")
            path = resolve_video(cut, decision, edit)
            info = probe(path)
            duration = video_duration(info)
            dependencies[str(path)] = file_hash(path)
            if cut["audio_mode"] == "source":
                require(decision["source_verified"] is True and decision["source_intervals"],
                        f"Row {cut['row']}: verify the source speech and its linked edit intervals")
                require(any(s["codec_type"] == "audio" for s in info["streams"]), "Source audio stream missing")
                intervals = []
                previous_end = -1.0
                for start, end in decision["source_intervals"]:
                    require(isinstance(start, (int, float)) and math.isfinite(start) and start >= 0,
                            "Source interval start must be finite and nonnegative")
                    positive(end - start, "source interval duration")
                    first_frame = math.floor(start * FPS + 1e-7)
                    last_frame = math.ceil(end * FPS - 1e-7)
                    quantized_start, quantized_end = first_frame / FPS, last_frame / FPS
                    count = last_frame - first_frame
                    require(count > 0 and quantized_start >= previous_end and quantized_end <= duration + 1e-6,
                            "Source intervals overlap, are out of order, or exceed the video")
                    intervals.append({"start": quantized_start, "frames": count})
                    previous_end = quantized_end
                speech_frames = sum(i["frames"] for i in intervals)
            elif cut["audio_mode"] == "narration":
                speech_frames = math.ceil((len(narration[cut["row"]]) // 2) / SPF)
                intervals = None
            else:
                speech_frames = decision["silent_frames"]
                require(isinstance(speech_frames, int) and not isinstance(speech_frames, bool) and speech_frames > 0,
                        f"Row {cut['row']}: silent cut needs explicit silent_frames; no default 2 seconds")
                intervals = None
            frames = speech_frames + (60 if cut is plan["cuts"][-1] else 0)
            if intervals is None:
                start = decision.get("video_start")
                if start is None:
                    start = motion_start(path, frames / FPS, duration)
                require(isinstance(start, (int, float)) and math.isfinite(start) and start >= 0
                        and start + frames / FPS <= duration + 1e-6,
                        f"Row {cut['row']}: video is too short for its complete measured cut")
                intervals = [{"start": start, "frames": frames}]
            elif cut is plan["cuts"][-1]:
                tail_start = intervals[-1]["start"] + intervals[-1]["frames"] / FPS
                require(tail_start + 2 <= duration + 1e-6, "Final source video lacks 2 seconds after the speech")
                intervals.append({"start": tail_start, "frames": 60, "silent_tail": True})
            bgm = decision.get("bgm_path", "")
            require(not cut["bgm"] or bgm, f"Row {cut['row']}: resolve the BGM specified in sheet")
            require(not bgm or cut["bgm"], "BGM is blank in sheet; do not silently add a soundtrack")
            if bgm:
                bgm = str(Path(bgm).expanduser().resolve(strict=True))
                if bgm != bgm_previous:
                    bgm_offset = 0.0
                bgm_info = probe(bgm)
                bgm_stream = next((s for s in bgm_info["streams"] if s["codec_type"] == "audio"), None)
                require(bgm_stream is not None, "BGM has no audio stream")
                if bgm_stream.get("duration"):
                    require(float(bgm_stream["duration"]) >= bgm_offset + speech_frames / FPS,
                            "BGM audio stream too short; no implicit looping")
                require(isinstance(decision.get("bgm_gain_db"), (int, float)) and
                        math.isfinite(decision["bgm_gain_db"]), "BGM gain must be explicitly specified")
                dependencies[bgm] = file_hash(bgm)
            resolved.append({"row": cut["row"], "frames": frames, "speech_frames": speech_frames,
                             "video_path": str(path), "video_intervals": intervals,
                             "decision": decision, "bgm_path": bgm,
                             "bgm_start_sample": round(bgm_offset * RATE) if bgm else 0})
            bgm_previous = bgm or None
            bgm_offset = bgm_offset + speech_frames / FPS if bgm else 0.0
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
             len(bgm_cache[i["bgm_path"]]) // 2 < i["bgm_start_sample"] + i["speech_frames"] * SPF]
    require(not short, f"Decoded BGM is too short at rows {short}; no implicit padding/looping")
    timeline = bytearray()
    for cut, item in zip(plan["cuts"], resolved):
        row = cut["row"]
        if cut["audio_mode"] == "narration":
            data = narration[row]
        elif cut["audio_mode"] == "source":
            pieces = []
            for index, interval in enumerate(item["video_intervals"]):
                if interval.get("silent_tail"):
                    continue
                audio_path = out / f"row-{row}-source-{index}.wav"
                ffmpeg(["-ss", str(interval["start"]), "-i", item["video_path"], "-vn", "-t",
                        str(interval["frames"] / FPS), "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", audio_path])
                part = pcm(audio_path)
                expected = interval["frames"] * SPF * 2
                require(abs(len(part) - expected) <= SPF * 2, "Source audio is shorter than linked video")
                pieces.append(part[:expected].ljust(expected, b"\0"))
            data = b"".join(pieces)
        else:
            data = bytes(item["speech_frames"] * SPF * 2)
        exact_length = item["speech_frames"] * SPF * 2
        require(len(data) <= exact_length, "Row audio exceeds measured frame duration")
        item["quantization_samples"] = (exact_length - len(data)) // 2
        data = data.ljust(exact_length, b"\0")
        unit = out / f"row-{row}.wav"
        save_pcm(unit, data)
        if cut["audio_mode"] != "silence":
            require(not silence_intervals(unit, 0.5), f"Row {row}: refine source/row intervals; pause is at least 0.5 seconds")
        level = out / f"row-{row}-level.wav"
        item["level_match"] = level_pcm(unit, level)
        data = pcm(level)
        if item["bgm_path"]:
            mixed = out / f"row-{row}-mix.wav"
            gain = item["decision"]["bgm_gain_db"]
            start = item["bgm_start_sample"] * 2
            bgm_part = bgm_cache[item["bgm_path"]][start:start + exact_length]
            data = mix_pcm(data, bgm_part, gain)
            save_pcm(mixed, data)
        if item is resolved[-1]:
            data += bytes(60 * SPF * 2)
        require(len(data) // 2 == item["frames"] * SPF, "Row sample/frame count mismatch")
        timeline.extend(data)
    total = sum(c["frames"] for c in resolved)
    save_pcm(out / "timeline.wav", timeline)
    normalization = normalize_timeline(out / "timeline.wav", out / "timeline-normalized.wav", total)
    prepared = {"schema_version": 1, "source_hash": plan["source_hash"],
                "plan_hash": digest(plan),
                "sheet_id": plan["sheet_id"], "sheet_name": plan["sheet_name"],
                "cuts": resolved, "total_frames": total, "sample_rate": RATE,
                "tail_frames": 60, "audio_path": str(out / "timeline-normalized.wav"),
                "audio_hash": file_hash(out / "timeline-normalized.wav"),
                "font_path": str(font), "dependencies": dependencies, "normalization": normalization,
                "status": "timing_ready", "production_complete": False}
    prepared["integrity"] = digest(prepared)
    save_json(out / "prepared.json", prepared)
    return {"prepared": str(out / "prepared.json"), "total_frames": total,
            "seconds": total / FPS, "sheet_writes": 0}


def frame_count(path):
    stream = next(s for s in probe(path, count=True)["streams"] if s["codec_type"] == "video")
    return int(stream["nb_read_frames"])


def concat_file(path, names):
    # Filenames are generated numeric names, never arbitrary script text.
    require(all(re.fullmatch(r"[A-Za-z0-9_.-]+", n) for n in names), "Unsafe intermediate filename")
    path.write_text("".join(f"file '{name}'\n" for name in names), encoding="utf-8")


def render_media(plan, prepared_path, out_dir, receipt=None, local_only=False):
    prepared = read_json(prepared_path)
    validate_prepared(plan, prepared)
    require(prepared["source_hash"] == plan["source_hash"] and prepared["status"] == "timing_ready",
            "Prepared media does not match plan")
    require([c["row"] for c in prepared["cuts"]] == [c["row"] for c in plan["cuts"]]
            and prepared["total_frames"] == sum(c["frames"] for c in prepared["cuts"])
            and prepared["tail_frames"] == 60, "Prepared timeline was modified inconsistently")
    require(file_hash(prepared["audio_path"]) == prepared["audio_hash"], "Prepared audio changed")
    require(all(Path(p).is_file() and file_hash(p) == h for p, h in prepared["dependencies"].items()),
            "A media/font/adjustment dependency changed; prepare again")
    if not local_only:
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
        overlay(cut, item["decision"], prepared["font_path"], png)
        pieces = []
        for index, interval in enumerate(item["video_intervals"]):
            piece = out / f"row-{row}-{index}.mp4"
            ffmpeg(["-ss", str(interval["start"]), "-i", item["video_path"],
                    "-vf", "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080,fps=30,setsar=1",
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
    name = plan["sheet_name"]
    require(name not in (".", "..") and not any(c in name for c in ("/", "\\", "\0")), "Unsafe sheet filename")
    partial = out / "validation.partial.mp4"
    ffmpeg(["-f", "concat", "-safe", "1", "-i", listing, "-i", prepared["audio_path"],
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-ar", str(RATE), "-movflags", "+faststart", partial])
    info = probe(partial, count=True)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    require((video["width"], video["height"], video["r_frame_rate"], video["codec_name"], audio["codec_name"])
            == (1080, 1080, "30/1", "h264", "aac"), "Export specification mismatch")
    require(int(video["nb_read_frames"]) == prepared["total_frames"], "Final video differs from H column frame total")
    require(abs(float(info["format"]["duration"]) - prepared["total_frames"] / FPS) <= 1 / FPS + 2048 / RATE,
            "Container duration differs beyond frame/AAC tolerance")
    measure = loudness(partial)
    if not prepared["normalization"]["silent"]:
        require(math.isfinite(float(measure["input_i"])) and abs(float(measure["input_i"]) + 7.2) <= 0.3,
                f"AAC loudness target missed: {measure['input_i']} LUFS (target -7.2 ±0.3)")
        require(float(measure["input_tp"]) <= -1.0, f"AAC true peak exceeds -1 dBTP: {measure['input_tp']}")
    decoded = out / "decoded-aac.wav"
    ffmpeg(["-i", partial, "-vn", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", decoded])
    decoded_data = pcm(decoded)
    expected_samples = prepared["total_frames"] * SPF
    require(expected_samples <= len(decoded_data) // 2 <= expected_samples + 2048,
            "Decoded AAC does not cover the complete measured timeline")
    tail_data = array.array("h", decoded_data[(expected_samples - 60 * SPF + SPF) * 2:expected_samples * 2])
    require(len(tail_data) == 59 * SPF, "Decoded tail duration mismatch")
    require(tail_data and max(abs(s) for s in tail_data) <= 33,
            "AAC tail is not silent below -60dBFS after one-frame encoder tolerance")
    ffmpeg(["-i", partial, "-f", "null", "-"])
    final = out / f"{name}.mp4"
    partial.rename(final)
    report = {"status": "local_validation_passed" if local_only else "media_qa_passed",
              "video_path": str(final), "total_frames": prepared["total_frames"],
              "seconds": prepared["total_frames"] / FPS, "loudness": measure,
              "tail_frames": 60, "tail_encoder_tolerance_frames": 1,
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
