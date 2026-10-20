from __future__ import annotations

import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageColor

from .common import require


def load_font(path, size, weight):
    require(isinstance(size, int) and 1 <= size <= 1000, "Font size must be an integer in 1..1000")
    font = ImageFont.truetype(str(path), size)
    try:
        axes = font.get_variation_axes()
    except OSError:
        # Static fonts are explicitly mapped by family AND weight in edit.fonts.
        return font
    values = []
    for axis in axes:
        name = axis["name"].decode() if isinstance(axis["name"], bytes) else axis["name"]
        if "weight" in name.lower():
            require(axis["minimum"] <= weight <= axis["maximum"], "Requested font weight is unavailable")
            values.append(weight)
        else:
            values.append(axis["default"])
    font.set_variation_by_axes(values)
    return font


def text_image(text, font, fill, stroke_fill, stroke_width, spacing, align):
    require(isinstance(stroke_width, int) and 0 <= stroke_width <= 100, "Invalid text stroke width")
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


def font_file(fonts, style):
    require(style["font"] in fonts and str(style["weight"]) in fonts[style["font"]],
            f"Map local font file for {style['font']} weight {style['weight']} in edit.fonts")
    return fonts[style["font"]][str(style["weight"])]


def overlay(cut, resolved, fonts, destination):
    video = resolved["video"]
    image = Image.new("RGBA", (video["width"], video["height"]))
    for field in ("caption", "note1", "note2"):
        if not cut[field]:
            continue
        style = resolved["styles"][field]
        font = load_font(font_file(fonts, style), style["size"], style["weight"])
        text = wrap_note(cut[field], font, style["max_width"] - 2 * style["stroke"] - 4) \
            if style["overflow"] == "wrap" else cut[field]
        block = text_image(text, font, style["fill"], style["stroke_fill"],
                           style["stroke"], style["spacing"], style["align"])
        if block.width > style["max_width"]:
            require(style["overflow"] == "compress-width", f"{field}: text exceeds sheet maximum width")
            block = block.resize((style["max_width"], block.height), Image.Resampling.LANCZOS)
        anchor = style["anchor"]
        x = style["x"] - (block.width / 2 if anchor in ("center", "top-center", "bottom-center")
                           else block.width if anchor.endswith("right") else 0)
        y = style["y"] - (block.height / 2 if anchor == "center"
                           else block.height if anchor.startswith("bottom") else 0)
        x, y = round(x), round(y)
        require(0 <= x and x + block.width <= video["width"] and
                0 <= y and y + block.height <= video["height"], f"{field}: text exceeds canvas; resolve placement")
        band = style.get("band")
        if band:
            left = 0 if band["full_width"] else x - band["pad_x"]
            right = video["width"] if band["full_width"] else x + block.width + band["pad_x"]
            top, bottom = y - band["pad_y"], y + block.height + band["pad_y"]
            require(0 <= left <= right <= video["width"] and 0 <= top <= bottom <= video["height"],
                    f"{field}: band exceeds canvas")
            rgba = ImageColor.getcolor(band["fill"], "RGBA")
            layer = Image.new("RGBA", image.size)
            ImageDraw.Draw(layer).rectangle((left, top, right, bottom),
                                            fill=(*rgba[:3], round(rgba[3] * band["opacity"])))
            image.alpha_composite(layer)
        for effect_name in ("shadow", "glow"):
            effect = style.get(effect_name)
            if effect:
                mask = Image.new("L", image.size)
                dx, dy = (effect["dx"], effect["dy"]) if effect_name == "shadow" else (0, 0)
                mask.paste(block.getchannel("A"), (round(x + dx), round(y + dy)))
                mask = mask.filter(ImageFilter.GaussianBlur(effect["radius"]))
                rgba = ImageColor.getcolor(effect["color"], "RGBA")
                mask = mask.point(lambda a: round(a * effect["opacity"] * rgba[3] / 255))
                layer = Image.new("RGBA", image.size, rgba)
                layer.putalpha(mask)
                image.alpha_composite(layer)
        image.alpha_composite(block, (x, y))
    image.save(destination)
