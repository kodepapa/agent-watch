"""Headless screenshots of the dashboard (SVG via Textual, PNG via Pillow)."""
from __future__ import annotations

import asyncio
import io
import os

from rich.console import Console
from rich.segment import Segment
from rich.terminal_theme import MONOKAI

from . import theme as th

FONT_DIRS = [os.path.join(os.path.dirname(__file__), "fonts"),
             os.path.expanduser("~/.local/share/fonts"), "/usr/share/fonts/truetype/dejavu"]
FONT_FILES = [("JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf"),
              ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf")]


def _find_fonts():
    for regular, bold in FONT_FILES:
        for d in FONT_DIRS:
            r, b = os.path.join(d, regular), os.path.join(d, bold)
            if os.path.exists(r):
                return r, b if os.path.exists(b) else r
    return None, None


async def _capture(app, size, settle: float):
    async with app.run_test(size=size) as pilot:
        await pilot.pause(settle)
        app.tick()
        await pilot.pause(0.3)
        svg = app.export_screenshot(title="agentwatch")
        console = Console(width=size[0], height=size[1], file=io.StringIO(), force_terminal=True,
                          color_system="truecolor", record=True, legacy_windows=False, safe_box=False)
        console.print(app.screen._compositor.render_update(full=True))
        segments = list(console._record_buffer)
    return svg, segments


def capture(app, size=(132, 46), settle: float = 0.6):
    return asyncio.run(_capture(app, size, settle))


def _hex(color, default):
    if color is None:
        return default
    tc = color.get_truecolor(MONOKAI)
    return (tc.red, tc.green, tc.blue)


LINES = {"─": ("lr", 1), "│": ("ud", 1), "━": ("lr", 2), "┃": ("ud", 2),
         "┏": ("rd", 2), "┓": ("ld", 2), "┗": ("ur", 2), "┛": ("ul", 2),
         "┌": ("rd", 1), "┐": ("ld", 1), "└": ("ur", 1), "┘": ("ul", 1),
         "═": ("lr", 3), "║": ("ud", 3)}
ROUND = {"╭": "rd", "╮": "ld", "╰": "ur", "╯": "ul"}
EIGHTHS = {"▁": 1, "▂": 2, "▃": 3, "▄": 4, "▅": 5, "▆": 6, "▇": 7, "█": 8}


class _SafeDraw:
    """Skips zero- or negative-size rectangles that rounding can produce at 1x."""

    def __init__(self, d):
        self.d = d

    def rectangle(self, xy, fill=None):
        x0, y0, x1, y1 = xy
        if x1 >= x0 and y1 >= y0:
            self.d.rectangle([x0, y0, x1, y1], fill=fill)

    def arc(self, *args, **kwargs):
        self.d.arc(*args, **kwargs)


def _draw_cell_glyph(d, ch, px, py, cw, chh, color, scale) -> bool:
    """Draw box-drawing and block characters as geometry so they meet at
    cell edges the way a terminal renders them. Returns False if not handled."""
    d = _SafeDraw(d)
    light = max(1, round(scale))
    cx, cy = px + cw // 2, py + chh // 2
    if ch in EIGHTHS:
        h = round(chh * EIGHTHS[ch] / 8)
        d.rectangle([px, py + chh - h, px + cw - 1, py + chh - 1], fill=color)
        return True
    if ch == "┊":
        seg = max(1, chh // 8)
        for k in range(4):
            y = py + k * chh // 4 + chh // 16
            d.rectangle([cx - light // 2, y, cx - light // 2 + light - 1, y + seg], fill=color)
        return True
    if ch in LINES or ch in ROUND:
        kind, weight = LINES.get(ch, (ROUND.get(ch), 1))
        w = light * (2 if weight == 2 else 1)
        if weight == 3:                       # double lines
            off = light * 2
            if kind == "lr":
                for y in (cy - off, cy + off):
                    d.rectangle([px, y - light // 2, px + cw - 1, y - light // 2 + light - 1], fill=color)
            else:
                for x in (cx - off, cx + off):
                    d.rectangle([x - light // 2, py, x - light // 2 + light - 1, py + chh - 1], fill=color)
            return True
        x0, y0 = cx - w // 2, cy - w // 2
        x1, y1 = x0 + w - 1, y0 + w - 1
        if ch in ROUND:
            r = cw // 2
            box = {"rd": (cx, cy, 180, 270), "ld": (cx - 2 * r, cy, 270, 360),
                   "ur": (cx, cy - 2 * r, 90, 180), "ul": (cx - 2 * r, cy - 2 * r, 0, 90)}[kind]
            bx, by, a0, a1 = box
            d.arc([bx - w / 2, by - w / 2, bx + 2 * r + w / 2, by + 2 * r + w / 2], a0, a1,
                  fill=color, width=w)
            if "d" in kind:
                d.rectangle([x0, cy + r, x1, py + chh - 1], fill=color)
            if "u" in kind:
                d.rectangle([x0, py, x1, cy - r], fill=color)
            if "r" in kind:
                d.rectangle([cx + r, y0, px + cw - 1, y1], fill=color)
            if "l" in kind:
                d.rectangle([px, y0, cx - r, y1], fill=color)
            return True
        if "l" in kind:
            d.rectangle([px, y0, x1 if kind != "lr" else px + cw - 1, y1], fill=color)
        if "r" in kind:
            d.rectangle([x0 if kind != "lr" else px, y0, px + cw - 1, y1], fill=color)
        if "u" in kind:
            d.rectangle([x0, py, x1, y1 if kind != "ud" else py + chh - 1], fill=color)
        if "d" in kind:
            d.rectangle([x0, y0 if kind != "ud" else py, x1, py + chh - 1], fill=color)
        return True
    return False


def render_png(segments, cols: int, rows: int, path: str, title: str = "", scale: int = 2) -> None:
    from PIL import Image, ImageDraw, ImageFont

    regular, bold = _find_fonts()
    size = 14 * scale
    font = ImageFont.truetype(regular, size) if regular else ImageFont.load_default()
    font_b = ImageFont.truetype(bold, size) if bold else font
    cell_w = int(round(font.getlength("M")))
    cell_h = int(size * 1.32)
    pad = 18 * scale
    bar = 30 * scale
    width = cols * cell_w + pad * 2
    height = rows * cell_h + pad * 2 + bar
    bg = tuple(int(th.BG[i:i + 2], 16) for i in (1, 3, 5))
    ink = tuple(int(th.INK[i:i + 2], 16) for i in (1, 3, 5))

    img = Image.new("RGB", (width + 40 * scale, height + 40 * scale), (12, 13, 16))
    win = Image.new("RGB", (width, height), bg)
    d = ImageDraw.Draw(win)
    d.rectangle([0, 0, width, bar], fill=(40, 43, 51))
    for i, c in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
        cx, cy, r = pad + i * 20 * scale, bar // 2, 6 * scale
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
    if title:
        small = ImageFont.truetype(regular, 11 * scale) if regular else font
        tw = small.getlength(title)
        d.text(((width - tw) / 2, bar / 2 - 7 * scale), title, font=small, fill=(140, 145, 156))

    y0 = bar + pad
    segments = [s for s in segments if not s.control]
    lines = list(Segment.split_and_crop_lines(segments, cols, pad=True))
    for row, line in enumerate(lines[:rows]):
        x = 0
        for seg in line:
            text, style = seg.text, seg.style
            if not text:
                continue
            fg = _hex(style.color if style else None, ink)
            bgc = _hex(style.bgcolor if style else None, bg)
            if style and style.reverse:
                fg, bgc = bgc, fg
            if style and style.dim:
                fg = tuple(int(a * 0.6 + b * 0.4) for a, b in zip(fg, bg))
            f = font_b if (style and style.bold) else font
            for ch in text:
                px, py = pad + x * cell_w, y0 + row * cell_h
                if bgc != bg:
                    d.rectangle([px, py, px + cell_w, py + cell_h], fill=bgc)
                if ch != " " and not _draw_cell_glyph(d, ch, px, py, cell_w, cell_h, fg, scale):
                    d.text((px, py + (cell_h - size) // 2 - scale), ch, font=f, fill=fg)
                x += 1
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, width - 1, height - 1], radius=12 * scale, fill=255)
    img.paste(win, (20 * scale, 20 * scale), mask)
    img.save(path)


def screenshot(app, path: str, size=(132, 46), settle: float = 0.6, title: str = "") -> str:
    svg, segments = capture(app, size, settle)
    if path.endswith(".svg"):
        with open(path, "w") as f:
            f.write(svg)
    else:
        render_png(segments, size[0], size[1], path, title=title)
    return path
