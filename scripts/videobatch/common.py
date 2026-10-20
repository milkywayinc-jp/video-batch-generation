from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
import wave
from pathlib import Path

FPS = 30
RATE = 48000
SPF = RATE // FPS


class BatchError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise BatchError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(args):
    result = subprocess.run([str(a) for a in args], capture_output=True)
    if result.returncode:
        # Never log shell-expanded commands or credential environment variables.
        raise BatchError(f"{Path(args[0]).name} failed: " +
                         result.stderr.decode(errors="replace")[-3000:])
    return result


def positive(value, label):
    require(isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0, f"{label}: positive finite number required")
    return value


def new_directory(path):
    path = Path(path).resolve()
    require(not path.exists(), f"Output already exists: {path}")
    path.mkdir(parents=True)
    return path


def validate_prepared(plan, prepared):
    require(prepared.get("status") == "timing_ready" and prepared.get("schema_version") == 1,
            "Use a measured prepared.json produced by prepare; no provisional timing writes")
    require(prepared.get("plan_hash") == digest(plan), "Plan changed after preparation")
    require(prepared.get("integrity") == digest({k: v for k, v in prepared.items() if k != "integrity"}),
            "Prepared manifest changed; prepare again")
    require(file_hash(prepared["audio_path"]) == prepared["audio_hash"], "Prepared audio changed")
    with wave.open(prepared["audio_path"], "rb") as f:
        require(f.getframerate() == RATE and f.getnchannels() == 1 and f.getsampwidth() == 2
                and f.getnframes() == prepared["total_frames"] * SPF and prepared["tail_frames"] == 60,
                "Prepared audio/frame duration mismatch")
