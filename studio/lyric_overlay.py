"""Small, cached subtitles composited after the untouched C preview image."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import math
import os

from PIL import Image, ImageDraw, ImageFont


@lru_cache(maxsize=4)
def _font(size: int):
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for candidate in (fonts / "msyh.ttc", fonts / "msyhbd.ttc", fonts / "simhei.ttf",
                      "msyh.ttc", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _prefix(text: str, width: float, font) -> int:
    """Find a fitting prefix without measuring every prefix of a long lyric."""
    first, last = 0, len(text)
    while first < last:
        middle = (first + last + 1) // 2
        if font.getlength(text[:middle]) <= width:
            first = middle
        else:
            last = middle - 1
    return first


def _lines(text: str, width: int, font) -> tuple[str, ...]:
    if font.getlength(text) <= width:
        return (text,)
    split = _prefix(text, width, font)
    # Prefer an English word boundary; CJK text can wrap between characters.
    space = text.rfind(" ", 0, split + 1)
    if space >= max(1, split // 2):
        split = space
    if split < 1:
        return ()
    first, remaining = text[:split].rstrip(), text[split:].lstrip()
    if font.getlength(remaining) <= width:
        return (first, remaining)
    suffix = "…"
    count = _prefix(remaining, max(0, width - font.getlength(suffix)), font)
    return (first, remaining[:count].rstrip() + suffix)


@lru_cache(maxsize=128)
def _caption_patch(text: str, image_width: int):
    size = max(12, min(20, round(image_width / 24)))
    font = _font(size)
    padding_x, padding_y, stroke, spacing = 11, 7, 2, 4
    available = min(image_width - 32, round(image_width * .88)) - 2 * (padding_x + stroke)
    if available < font.getlength("…"):
        return None
    lines = _lines(text, available, font)
    if not lines:
        return None
    left, top, right, bottom = font.getbbox("国Ag", stroke_width=stroke)
    line_height = bottom - top
    width = math.ceil(max(font.getlength(line) for line in lines)) + 2 * (padding_x + stroke)
    height = len(lines) * line_height + (len(lines) - 1) * spacing + padding_y * 2
    patch = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(patch)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=10, fill=(7, 12, 22, 174))
    for row, line in enumerate(lines):
        draw.text((width / 2, padding_y + stroke + row * (line_height + spacing)), line,
                  font=font, anchor="mt", fill=(255, 255, 255, 255),
                  stroke_width=stroke, stroke_fill=(5, 8, 15, 245))
    return patch


def draw_lyric_overlay(image: Image.Image, text: str) -> Image.Image:
    """Return a new captioned image; empty lyrics return the original object.

    Only the current line is supplied by the song's lyric timeline. The patch
    is at most two lines, with ellipsis for longer text. Its translucent
    backing hugs the text rather than covering the whole lower scene.
    """
    if not text:
        return image
    text = " ".join(text.split())
    if not text:
        return image
    patch = _caption_patch(text, image.width)
    if patch is None or patch.height >= image.height:
        return image
    x = (image.width - patch.width) // 2
    y = max(0, image.height - min(12, image.height // 20) - patch.height)
    result = image.copy()
    if result.mode == "RGBA":
        result.alpha_composite(patch, dest=(x, y))
    else:
        result.paste(patch, (x, y), patch)
    return result
