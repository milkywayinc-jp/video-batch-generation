"""Explicit test-sheet preferences, never imported by production code."""
import copy
from videobatch.rules import leaf_paths, resolve_plan


def interpretation(plan, width=320, height=320, fps="30/1", tail=2, speed=1.1,
                   target=-18, size=24, fill="white", limit=140):
    def style(weight, x, y, anchor):
        return {"font": "Test JP", "weight": weight, "size": size, "fill": fill,
                "stroke_fill": "black", "stroke": 2, "spacing": 3,
                "align": "center", "x": x, "y": y, "anchor": anchor,
                "max_width": width - 32, "overflow": "compress-width"}
    values = {"video": {"width": width, "height": height, "fps": fps, "codec": "h264", "audio_codec": "aac",
                        "fit": "cover", "background": "black"},
              "audio": {"provider": "acquired-test-audio", "batch_limit": limit, "speed": speed,
                        "max_pause_seconds": 0.5, "retained_pause_seconds": 0.1, "trim_edges": True,
                        "tail_seconds": tail, "loudness_lufs": target, "peak_dbtp": -1, "level_match": True},
              "styles": {"caption": style(900, width / 2, height * 0.65, "center"),
                         "note1": style(400, 16, 16, "top-left"),
                         "note2": style(400, width - 16, height - 16, "bottom-right")},
              "material": {"transition": "hard", "selection": "motion", "shortage": "error"},
              "naming": {"video": "sheet-without-star", "audio": "sheet_index"}}
    cuts = [{"row": c["row"], "mode": "source" if c["directives"]["audio"] == "動画の音声をそのまま使用"
             else "narration" if c["speech"] else "silence", "audio": {}, "styles": {},
             "bgm_gain_db": -20 if c["bgm"] else None} for c in plan["cuts"]]
    provenance = {}
    for path in leaf_paths(values):
        ref = "H2" if path.startswith("values.audio") else "A2" if path.startswith("values.styles.caption") else \
            "D2" if path.startswith("values.styles.note1") else "E2" if path.startswith("values.styles.note2") else \
            "F2" if path.startswith("values.material") else "A1"
        provenance[path] = [ref]
    for i, cut in enumerate(cuts):
        provenance[f"cuts.{i}.mode"] = [f"J{cut['row']}" if f"J{cut['row']}" in plan["rule_inputs"] else "H2"]
        provenance[f"cuts.{i}.bgm_gain_db"] = ["A1"]
    return {"values": values, "cuts": cuts, "provenance": provenance,
            "interpretations": {k: "Test fixture's explicit sheet instruction" for k in plan["rule_inputs"]},
            "unresolved": []}


def resolved(plan, **kwargs):
    return resolve_plan(plan, interpretation(plan, **kwargs))


def fonts(path):
    return {"Test JP": {"400": str(path), "900": str(path)}}
