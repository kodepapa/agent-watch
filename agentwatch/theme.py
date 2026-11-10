"""Palette, agent colors and ASCII box helpers.

Look: a slate terminal, warm paper-colored ink, and one color per agent that
is reused in every panel so you can follow an agent across the screen.
"""
from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

BG = "#1c1f26"
PANEL = "#20242c"
SELECT = "#2a2f3a"                       # background of the selected row
INK = "#d8d4c8"
DIM = "#7a7f8c"
FAINT = "#3d424e"
LEAD = "#e9a35c"
ADVISOR = "#d8c46a"
ERROR = "#e86a6a"
OK = "#7cc48f"
WARN = "#e0b35a"

TEAM_COLORS = ["#7fa8e6", "#5fc2b0", "#b291e8", "#e48aa8", "#9cc464", "#7dd3e0", "#d99a6c"]
NAMED = {"blue": "#7fa8e6", "green": "#7cc48f", "teal": "#5fc2b0", "cyan": "#7dd3e0",
         "purple": "#b291e8", "violet": "#b291e8", "magenta": "#d98ad9", "pink": "#e48aa8",
         "red": "#e86a6a", "orange": "#e9a35c", "yellow": "#d8c46a", "lime": "#9cc464"}

_assigned: dict[str, str] = {}
_from_hint: set[str] = set()


def _free_color(exclude: str = "") -> str:
    used = set(_assigned.values())
    free = [c for c in TEAM_COLORS if c not in used and c != exclude]
    return free[0] if free else TEAM_COLORS[len(_assigned) % len(TEAM_COLORS)]


def agent_color(name: str, hint: str = "") -> str:
    """Stable color per agent. A configured color (team config) wins over an
    automatic one, and bumps any agent that was auto-assigned that color."""
    if name == "lead":
        return LEAD
    if name == "advisor":
        return ADVISOR
    if name == "tasks":
        return INK
    wanted = NAMED.get(hint.lower(), hint if hint.startswith("#") else "") if hint else ""
    if name in _assigned and (not wanted or name in _from_hint):
        return _assigned[name]
    if wanted:
        for other, color in list(_assigned.items()):
            if color == wanted and other != name and other not in _from_hint:
                _assigned[other] = _free_color(exclude=wanted)
        _assigned[name] = wanted
        _from_hint.add(name)
        return wanted
    _assigned[name] = _free_color()
    return _assigned[name]


def reset_colors() -> None:
    _assigned.clear()
    _from_hint.clear()


def fit(text: Text | str, width: int, style: str = "") -> Text:
    """Crop with an ellipsis or pad with spaces to exactly `width` cells."""
    t = Text(text, style=style) if isinstance(text, str) else text.copy()
    if width <= 0:
        return Text("")
    if cell_len(t.plain) > width:
        t.truncate(width, overflow="ellipsis")
    t.pad_right(width - cell_len(t.plain))
    return t


def spread(left: Text | str, right: Text | str, width: int) -> Text:
    """Left-aligned and right-aligned text on one line of `width` cells."""
    left = Text(left) if isinstance(left, str) else left.copy()
    right = Text(right) if isinstance(right, str) else right
    room = width - cell_len(right.plain) - 1
    if room < 4:
        return fit(left, width)
    out = fit(left, room)
    out.append(" ")
    out.append_text(right)
    return fit(out, width)


BORDERS = {
    #          tl   tr   bl   br   h    v
    "round": ("╭", "╮", "╰", "╯", "─", "│"),
    "heavy": ("┏", "┓", "┗", "┛", "━", "┃"),
    "double": ("╔", "╗", "╚", "╝", "═", "║"),
}
ASCII_BORDERS = {"round": ("+", "+", "+", "+", "-", "|"),
                 "heavy": ("+", "+", "+", "+", "=", "|"),
                 "double": ("+", "+", "+", "+", "=", "|")}

# glyphs used across panels; `use_ascii()` swaps them for plain ASCII
G = {"dot": "·", "rail": "┊", "call": "◆", "advice": "›", "status": "●", "legend": "■",
     "done": "━", "todo": "─", "error": "✕", "advisor": "◆", "task_done": "✓",
     "claim": "┃", "prompt": "▸", "idle": "_", "work": "─",
     "spark": " ▁▂▄▅▆▇█", "now": "│"}
_ascii = False


def use_ascii(on: bool = True) -> None:
    global _ascii
    _ascii = on
    if on:
        G.update({"dot": ".", "rail": ":", "call": "<>", "advice": "»", "status": "*",
                  "legend": "#", "done": "#", "todo": ".", "error": "x", "advisor": "@",
                  "task_done": "+", "claim": "|", "prompt": ">", "work": "-",
                  "spark": " _.--==#", "now": "|"})


def box(width: int, height: int, title: Text | str, lines: list[Text],
        border: str = FAINT, heavy: bool = False, right: Text | str = "",
        fill_from_bottom: bool = False, kind: str = "") -> list[Text]:
    """Draw a box; returns exactly `height` lines of `width` cells.

    kind: "round" (default), "heavy" (primary panels) or "double".
    """
    if width < 6 or height < 2:
        return [Text(" " * max(width, 0)) for _ in range(max(height, 0))]
    kind = kind or ("heavy" if heavy else "round")
    tl, tr, bl, br, h, v = (ASCII_BORDERS if _ascii else BORDERS)[kind]
    inner = width - 2
    title = Text(title) if isinstance(title, str) else title
    right = Text(right) if isinstance(right, str) else right

    top = Text(tl + h, style=border)
    if title.plain:
        top.append(" ")
        top.append_text(title)
        top.append(" ")
    used = cell_len(top.plain)
    tail = Text()
    if right.plain:
        tail.append(" ")
        tail.append_text(right)
        tail.append(" ")
        tail.append(h + tr, style=border)
    else:
        tail.append(tr, style=border)
    gap = width - used - cell_len(tail.plain)
    if gap < 1:
        tail = Text(tr, style=border)
        gap = width - used - 1
        if gap < 1:
            top = fit(top, width - 1)
            gap = 0
    top.append(h * gap, style=border)
    top.append_text(tail)

    body_h = height - 2
    body = lines[-body_h:] if fill_from_bottom else lines[:body_h]
    if fill_from_bottom:
        body = [Text("")] * (body_h - len(body)) + body
    out = [top]
    for i in range(body_h):
        line = body[i] if i < len(body) else Text("")
        row = Text(v, style=border)
        row.append(" ")
        row.append_text(fit(line, inner - 2))
        row.append(" ")
        row.append(v, style=border)
        out.append(row)
    out.append(Text(bl + h * inner + br, style=border))
    return out


def hjoin(blocks: list[list[Text]], gap: int = 1) -> list[Text]:
    """Place blocks of equal height side by side."""
    if not blocks:
        return []
    height = max(len(b) for b in blocks)
    out = []
    for i in range(height):
        row = Text()
        for j, b in enumerate(blocks):
            if j:
                row.append(" " * gap)
            row.append_text(b[i] if i < len(b) else Text(""))
        out.append(row)
    return out


def human_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"
