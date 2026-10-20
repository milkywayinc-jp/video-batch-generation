from __future__ import annotations

import math
from PIL import Image, ImageDraw, ImageFont

from .common import require


def load_font(path, size, weight):
    require(isinstance(size, int) and 1 <= size <= 500, "Font size must be an integer in 1..500")
    font = ImageFont.truetype(str(path), size)
    try:
        axes = font.get_variation_axes()
    except OSError as exc:
        raise ValueError("Use Noto Sans JP variable font to support both Regular 400 and Black 900") from exc
    values = []
    for axis in axes:
        name = axis["name"].decode() if isinstance(axis["name"], bytes) else axis["name"]
        values.append(weight if "weight" in name.lower() else axis["default"])
    require(any((a["name"].decode() if isinstance(a["name"], bytes) else a["name"]).lower() == "weight"
                for a in axes), "Font has no weight variation axis")
    font.set_variation_by_axes(values)
    return font


def text_image(text, font, fill, stroke_fill, stroke_width, spacing, align):
    require(isinstance(stroke_width, int) and 0 <= stroke_width <= 30, "Invalid text stroke width")
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bounds = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing,
                                    align=align, stroke_width=stroke_width)
    bounds = (math.floor(bounds[0]), math.floor(bounds[1]), math.ceil(bounds[2]), math.ceil(bounds[3]))
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    result = Image.new("RGBA", (max(1, width + 4), max(1, height + 4)))
    ImageDraw.Draw(result).multiline_text((2 - bounds[0], 2 - bounds[1]), text, font=font,
                                        fill=fill, spacing=spacing, align=align,
                                        stroke_width=stroke_width, stroke_fill=stroke_fill)
    return result


def wrap_note(text, font, width):
    lines = []
    for original in text.split("\n"):
        line = ""
        for character in original:
            if line and font.getlength(line + character) > width:
                lines.append(line)
                line = character
            else:
                line += character
        lines.append(line)
    return "\n".join(lines)


def overlay(cut, decision, font_path, destination):
    image = Image.new("RGBA", (1080, 1080))
    style = {"size": 85, "stroke": 8, "fill": "white", "stroke_fill": "black",
             "max_width": 1024, "center_y": decision.get("caption_y", 810),
             "spacing": 15}
    overrides = decision.get("caption_style", {})
    require(not (set(overrides) - set(style)), "Unrecognized caption_style key; do not ignore individual directives")
    style.update(overrides)
    if cut["caption"]:
        font = load_font(font_path, style["size"], 900)
        block = text_image(cut["caption"], font, style["fill"], style["stroke_fill"],
                           style["stroke"], style["spacing"], "center")
        require(isinstance(style["max_width"], int) and 1 <= style["max_width"] <= 1080,
                "Caption maximum width must be in 1..1080")
        if block.width > style["max_width"]:
            block = block.resize((style["max_width"], block.height), Image.Resampling.LANCZOS)
        top = round(style["center_y"] - block.height / 2)
        require(0 <= top and top + block.height <= 1080, "Caption block exceeds frame; adjust its center")
        image.alpha_composite(block, ((1080 - block.width) // 2, top))
    note_font = load_font(font_path, 28, 400)
    if cut["note1"]:
        note = text_image(wrap_note(cut["note1"], note_font, 1016), note_font,
                          "black", "white", 2, 5, "left")
        require(note.width <= 1024 and note.height <= 1024, "Note ① exceeds frame")
        image.alpha_composite(note, (28, 28))
    if cut["note2"]:
        note = text_image(cut["note2"], note_font, "black", "white", 2, 5, "right")
        require(note.width <= 1024 and note.height <= 1024, "Note ② exceeds frame")
        image.alpha_composite(note, (1052 - note.width, 1052 - note.height))
    image.save(destination)
