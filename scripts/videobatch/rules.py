"""Typed execution contract. Production preferences have no defaults here.

Codex interprets the sheet into this run-local document. This module checks the
contract and binds that interpretation to exact source cells; it is not a second
editable rule book or an arbitrary natural-language parser.
"""
from __future__ import annotations

import copy
import math
from fractions import Fraction

from .common import digest, require


class Timebase:
    def __init__(self, fps):
        require(isinstance(fps, str), "fps must be a rational string, e.g. num/den")
        try:
            self.fps = Fraction(fps)
        except (ValueError, ZeroDivisionError):
            require(False, "Invalid rational fps")
        require(0 < self.fps <= 120, "Renderer supports fps in (0,120]")

    @property
    def text(self):
        return f"{self.fps.numerator}/{self.fps.denominator}"

    def seconds(self, frames):
        return float(Fraction(frames, 1) / self.fps)

    def frames(self, seconds):
        return math.ceil(Fraction(str(seconds)) * self.fps)

    def sample(self, frame, rate=48000):
        exact = Fraction(frame * rate, 1) / self.fps
        return (2 * exact.numerator + exact.denominator) // (2 * exact.denominator)

    def samples(self, first, frames, rate=48000):
        return self.sample(first + frames, rate) - self.sample(first, rate)

    def cover_samples(self, first, samples, rate=48000):
        count = math.ceil(Fraction(samples, rate) * self.fps)
        while self.samples(first, count, rate) < samples:
            count += 1
        return count


def raw_plan(plan):
    return {k: v for k, v in plan.items() if k not in ("resolved_rules", "batches")}


def number(value, name, low, high):
    require(isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high, f"{name}: number in {low}..{high} required")


def keys(obj, required, optional=(), label="rules"):
    require(isinstance(obj, dict), f"{label}: object required")
    require(set(required) <= set(obj) <= set(required) | set(optional),
            f"{label}: required={sorted(required)}; unknown/missing keys are not ignored")


def validate_style(style, video):
    keys(style, {"font", "weight", "size", "fill", "stroke_fill", "stroke", "spacing",
                 "align", "x", "y", "anchor", "max_width", "overflow"},
         optional={"glow", "shadow", "band"}, label="text style")
    require(isinstance(style["font"], str) and style["font"].strip(), "Font family required")
    for field, low, high in (("weight", 1, 1000), ("size", 1, 1000), ("stroke", 0, 100),
                             ("spacing", 0, 1000), ("max_width", 1, video["width"])):
        number(style[field], field, low, high)
        require(isinstance(style[field], int), f"{field}: integer required")
    number(style["x"], "text x", 0, video["width"])
    number(style["y"], "text y", 0, video["height"])
    require(style["anchor"] in ("top-left", "top-center", "top-right", "center", "bottom-left",
                                 "bottom-center", "bottom-right"), "Unsupported text anchor")
    require(style["align"] in ("left", "center", "right"), "Unsupported text alignment")
    require(style["overflow"] in ("compress-width", "wrap", "error"), "Unsupported text overflow")
    from PIL import ImageColor
    for field in ("fill", "stroke_fill"):
        require(isinstance(style[field], str), f"{field}: color string required")
        ImageColor.getcolor(style[field], "RGBA")
    for field, required in (("glow", {"color", "radius", "opacity"}),
                            ("shadow", {"color", "radius", "opacity", "dx", "dy"}),
                            ("band", {"fill", "opacity", "pad_x", "pad_y", "full_width"})):
        effect = style.get(field)
        if effect is None:
            continue
        keys(effect, required, label=field)
        number(effect["opacity"], f"{field}.opacity", 0, 1)
        ImageColor.getcolor(effect["fill" if field == "band" else "color"], "RGBA")
        if field == "band":
            number(effect["pad_x"], "band.pad_x", 0, video["width"])
            number(effect["pad_y"], "band.pad_y", 0, video["height"])
            require(type(effect["full_width"]) is bool, "band.full_width must be boolean")
        else:
            number(effect["radius"], f"{field}.radius", 0, 200)
            if field == "shadow":
                number(effect["dx"], "shadow.dx", -video["width"], video["width"])
                number(effect["dy"], "shadow.dy", -video["height"], video["height"])


def validate_audio(audio):
    keys(audio, {"provider", "batch_limit", "speed", "max_pause_seconds", "retained_pause_seconds",
                 "trim_edges", "tail_seconds", "loudness_lufs", "peak_dbtp", "level_match"}, label="audio")
    require(isinstance(audio["provider"], str) and audio["provider"].strip(), "Audio provider required")
    number(audio["batch_limit"], "batch_limit", 1, 100000)
    require(isinstance(audio["batch_limit"], int), "batch_limit must be integer")
    number(audio["speed"], "speed", 0.5, 2)
    if audio["max_pause_seconds"] is not None:
        number(audio["max_pause_seconds"], "max_pause_seconds", 0.02, 3600)
    number(audio["retained_pause_seconds"], "retained_pause_seconds", 0, 3600)
    require(audio["max_pause_seconds"] is None or
            audio["retained_pause_seconds"] < audio["max_pause_seconds"],
            "Retained pause must be shorter than maximum pause")
    number(audio["tail_seconds"], "tail_seconds", 0, 3600)
    number(audio["loudness_lufs"], "loudness_lufs", -70, -5)
    number(audio["peak_dbtp"], "peak_dbtp", -9, 0)
    require(type(audio["trim_edges"]) is bool and type(audio["level_match"]) is bool, "Audio flags must be boolean")


def cut_rules(rules, row):
    matches = [c for c in rules["cuts"] if c["row"] == row]
    require(len(matches) == 1, f"Row {row}: resolved cut missing/duplicated")
    result = copy.deepcopy(rules["values"])
    decision = matches[0]
    result["audio"].update(decision["audio"])
    for field in ("caption", "note1", "note2"):
        if field in decision["styles"]:
            if result["styles"][field] is None:
                result["styles"][field] = {}
            result["styles"][field].update(decision["styles"][field])
    result.update({k: decision[k] for k in ("mode", "bgm_gain_db")})
    return result


def leaf_paths(obj, prefix="values"):
    if isinstance(obj, dict):
        for key, val in obj.items():
            yield from leaf_paths(val, f"{prefix}.{key}")
    else:
        yield prefix


def validate_rules(plan):
    require(plan.get("schema_version") == 2, "Legacy plan: inspect and resolve the current sheet again")
    rules = plan.get("resolved_rules")
    require(isinstance(rules, dict), "Resolve sheet rules before batching or editing")
    require(rules.get("schema_version") == 2 and rules.get("source_plan_hash") == digest(raw_plan(plan)),
            "Rules are from another sheet snapshot")
    require(rules.get("integrity") == digest({k: v for k, v in rules.items() if k != "integrity"}),
            "Resolved rules changed; resolve from the sheet again")
    keys(rules["values"], {"video", "audio", "styles", "material", "naming"}, label="values")
    video, audio = rules["values"]["video"], rules["values"]["audio"]
    keys(video, {"width", "height", "fps", "codec", "audio_codec", "fit", "background"}, label="video")
    for field in ("width", "height"):
        number(video[field], field, 16, 8192)
        require(isinstance(video[field], int) and video[field] % 2 == 0, "H.264 dimensions must be even integers")
    Timebase(video["fps"])
    require(video["codec"] == "h264" and video["audio_codec"] == "aac", "This renderer supports H.264/AAC only")
    require(video["fit"] in ("cover", "contain"), "Unsupported material fit")
    from PIL import ImageColor
    ImageColor.getcolor(video["background"], "RGBA")
    validate_audio(audio)
    keys(rules["values"]["styles"], {"caption", "note1", "note2"}, label="styles")
    for style in rules["values"]["styles"].values():
        if style is not None:
            validate_style(style, video)
    material = rules["values"]["material"]
    keys(material, {"transition", "selection", "shortage"}, label="material")
    require(material["transition"] == "hard" and material["shortage"] == "error",
            "Unsupported transition/shortage policy; do not silently replace it")
    require(material["selection"] in ("motion", "manual"), "Unsupported material selection")
    keys(rules["values"]["naming"], {"video", "audio"}, label="naming")
    require(rules["values"]["naming"]["video"] in ("sheet", "sheet-without-star"), "Unsupported video naming")
    require(isinstance(rules["values"]["naming"]["audio"], str), "Audio naming description required")
    require([c["row"] for c in rules["cuts"]] == [c["row"] for c in plan["cuts"]], "Resolved cut rows mismatch")
    paths = set(leaf_paths(rules["values"]))
    for i, cut in enumerate(rules["cuts"]):
        keys(cut, {"row", "mode", "audio", "styles", "bgm_gain_db"}, label="cut rules")
        require(cut["mode"] in ("narration", "source", "silence"), "Unsupported audio mode")
        require(isinstance(cut["audio"], dict) and isinstance(cut["styles"], dict), "Cut overrides must be objects")
        require(set(cut["audio"]) <= {"provider", "batch_limit", "speed", "max_pause_seconds",
                                           "retained_pause_seconds", "trim_edges"},
                "Tail and mastering are video-wide; per-cut overrides unsupported")
        require(set(cut["styles"]) <= {"caption", "note1", "note2"}, "Unknown style override")
        require(all(isinstance(s, dict) for s in cut["styles"].values()), "Style overrides must be objects")
        merged = cut_rules(rules, cut["row"])
        validate_audio(merged["audio"])
        for field, style in merged["styles"].items():
            if style is not None or plan["cuts"][i][field]:
                validate_style(style, video)
        if cut["bgm_gain_db"] is not None:
            number(cut["bgm_gain_db"], "BGM gain", -100, 24)
        require(not plan["cuts"][i]["bgm"] or cut["bgm_gain_db"] is not None,
                f"Row {cut['row']}: resolve BGM gain from sheet")
        require(cut["mode"] != "narration" or plan["cuts"][i]["speech"], "Narration needs speech text")
        for key in ("mode", "bgm_gain_db"):
            paths.add(f"cuts.{i}.{key}")
        for field in ("audio", "styles"):
            paths.update(leaf_paths(cut[field], f"cuts.{i}.{field}"))
    provenance = rules["provenance"]
    require(set(provenance) == paths, "Every resolved field needs exact cell provenance; unknown fields rejected")
    source_inputs = plan["rule_inputs"]
    for path, refs in provenance.items():
        require(isinstance(refs, list) and refs and all(r in source_inputs for r in refs),
                f"{path}: invalid/missing source cells")
    covered = rules["interpretations"]
    require(set(covered) == set(source_inputs), "Interpret every nonempty rule cell; do not drop unknown directives")
    require(all(isinstance(v, str) and v.strip() for v in covered.values()), "Cell interpretation text required")
    require(not rules["unresolved"], "Unresolved sheet directives: " + str(rules["unresolved"]))
    if "batches" in plan:
        from .sheets import make_batches
        require(digest(plan["batches"]) == digest(make_batches(plan["cuts"], rules)),
                "Narration batches changed; resolve from the sheet again")
    return rules


def resolve_plan(plan, interpretation):
    result = copy.deepcopy(raw_plan(plan))
    rules = copy.deepcopy(interpretation)
    keys(rules, {"values", "cuts", "provenance", "interpretations", "unresolved"}, label="interpretation")
    rules.update(schema_version=2, source_plan_hash=digest(result))
    rules["integrity"] = digest(rules)
    result["resolved_rules"] = rules
    validate_rules(result)
    from .sheets import make_batches
    result["batches"] = make_batches(result["cuts"], rules)
    return result


def interpretation_template(plan):
    return {"values": {}, "cuts": [{"row": c["row"], "mode": None, "audio": {}, "styles": {},
                                      "bgm_gain_db": None} for c in plan["cuts"]],
            "provenance": {}, "interpretations": {k: "" for k in plan["rule_inputs"]},
            "unresolved": ["Codex: interpret current global/common/individual sheet cells using references/rules.md"]}
