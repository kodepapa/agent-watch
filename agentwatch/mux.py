"""Hosted sessions: Claude Code terminals that live inside agentwatch.

Each session is a detached tmux session on a private tmux server (its own
socket, no user config). tmux does the terminal emulation and keeps the
sessions alive when agentwatch closes; agentwatch draws the screen it captures
and sends keys back. Nothing here knows about Textual.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

SOCKET = os.environ.get("AGENTWATCH_TMUX_SOCKET") or "agentwatch"
DEFAULT_COMMAND = os.environ.get("AGENTWATCH_COMMAND") or "claude"
SESSION_ENV = "AGENTWATCH_SESSION"       # set inside every hosted session; the hook reports it
NAME_OK = re.compile(r"[^a-z0-9_-]+")
OPTIONS = (("-g", "status", "off"), ("-s", "escape-time", "0"), ("-g", "history-limit", "20000"),
           ("-g", "default-terminal", "screen-256color"), ("-g", "mouse", "off"),
           ("-s", "exit-empty", "on"), ("-g", "focus-events", "off"))
# Not set: `window-size manual`. tmux 3.4 crashes on the next new-session with it; resize-window
# switches a window to manual sizing on its own.
SEP = " :: "                             # tmux escapes control characters in formats
LIST_FORMAT = SEP.join(("#{session_name}", "#{session_created}", "#{pane_current_path}",
                        "#{pane_current_command}", "#{session_activity}", "#{session_id}"))
SCREEN_FORMAT = "#{cursor_x} #{cursor_y} #{cursor_flag} #{history_size} #{pane_width} #{pane_height}"


class MuxError(Exception):
    pass


@dataclass
class Session:
    name: str
    created: float = 0.0
    cwd: str = ""
    command: str = ""
    activity: float = 0.0


@dataclass
class Screen:
    lines: list[str] = field(default_factory=list)   # ANSI text, one per row
    cursor: tuple[int, int] = (0, 0)
    cursor_visible: bool = True
    history: int = 0
    size: tuple[int, int] = (80, 24)


def slug(text: str) -> str:
    """A tmux-safe session name (tmux rejects dots and colons)."""
    return NAME_OK.sub("-", text.lower()).strip("-")[:24] or "session"


class Mux:
    def __init__(self, socket: str = SOCKET, command: str = DEFAULT_COMMAND):
        self.socket = socket
        self.command = command

    @staticmethod
    def available() -> bool:
        return shutil.which("tmux") is not None

    def _tmux(self, *args: str, stdin: str | None = None, check: bool = True) -> str:
        try:
            proc = subprocess.run(["tmux", "-L", self.socket, "-f", os.devnull, *args], input=stdin,
                                  capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise MuxError(str(e)) from e
        if check and proc.returncode != 0:
            raise MuxError(proc.stderr.strip() or f"tmux {args[0]} failed")
        return proc.stdout

    @staticmethod
    def _target(name: str) -> str:
        return f"={name}:"                       # "=" means exact match, not prefix

    # ---- sessions --------------------------------------------------------
    def sessions(self) -> list[Session]:
        out = self._tmux("list-sessions", "-F", LIST_FORMAT, check=False)   # fails when no server runs
        found = []
        for line in out.splitlines():
            parts = line.split(SEP)
            if len(parts) == 6:
                found.append((int(parts[5].lstrip("$") or 0),      # tmux numbers sessions as it creates them
                              Session(parts[0], float(parts[1] or 0), parts[2], parts[3], float(parts[4] or 0))))
        return [s for _, s in sorted(found, key=lambda pair: pair[0])]

    def unique_name(self, wanted: str) -> str:
        taken = {s.name for s in self.sessions()}
        base, name, n = slug(wanted), slug(wanted), 1
        while name in taken:
            n += 1
            name = f"{base}-{n}"
        return name

    def new(self, cwd: str, name: str = "", command: str = "", size: tuple[int, int] = (120, 40)) -> str:
        cwd = os.path.abspath(os.path.expanduser(cwd or "."))
        if not os.path.isdir(cwd):
            raise MuxError(f"not a directory: {cwd}")
        name = self.unique_name(name or os.path.basename(cwd.rstrip(os.sep)) or "session")
        # TMUX is unset so Claude Code keeps its teammates in-process instead of
        # splitting panes we would not be drawing.
        run = f"exec env -u TMUX -u TMUX_PANE {command or self.command}"
        self._tmux("new-session", "-d", "-s", name, "-x", str(size[0]), "-y", str(size[1]), "-c", cwd,
                   "-e", f"{SESSION_ENV}={name}", "-e", "COLORTERM=truecolor", run)
        for scope, key, value in OPTIONS:
            self._tmux("set-option", scope, key, value, check=False)
        return name

    def kill(self, name: str) -> None:
        self._tmux("kill-session", "-t", self._target(name).rstrip(":"), check=False)

    def rename(self, name: str, new: str) -> str:
        new = self.unique_name(new)
        self._tmux("rename-session", "-t", self._target(name).rstrip(":"), new)
        return new

    def resize(self, name: str, width: int, height: int) -> None:
        if width >= 10 and height >= 3:
            self._tmux("resize-window", "-t", self._target(name), "-x", str(width), "-y", str(height), check=False)

    # ---- screen ----------------------------------------------------------
    def capture(self, name: str, scroll: int = 0) -> Screen:
        """The visible screen, or the screen `scroll` lines back into history."""
        target = self._target(name)
        grab = ["capture-pane", "-p", "-e", "-t", target]
        if scroll > 0:
            # -S/-E count from the top of the visible screen; negatives reach into history.
            meta = self._tmux("display-message", "-p", "-t", target, SCREEN_FORMAT).split()
            height = int(meta[5]) if len(meta) == 6 else 24
            grab += ["-S", str(-scroll), "-E", str(height - 1 - scroll)]
        out = self._tmux("display-message", "-p", "-t", target, SCREEN_FORMAT, ";", *grab)
        head, _, body = out.partition("\n")
        meta = head.split()
        if len(meta) != 6:
            raise MuxError(f"unexpected tmux reply: {head!r}")
        x, y, flag, history, width, height = (int(v) for v in meta)
        lines = body.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        lines = (lines + [""] * height)[:height]
        return Screen(lines, (x, y), bool(flag) and scroll == 0, history, (width, height))

    # ---- input -----------------------------------------------------------
    def send_text(self, name: str, text: str) -> None:
        if text:
            self._tmux("send-keys", "-l", "-t", self._target(name), "--", text)

    def send_keys(self, name: str, *keys: str) -> None:
        if keys:
            self._tmux("send-keys", "-t", self._target(name), "--", *keys)

    def send_hex(self, name: str, *codes: str) -> None:
        self._tmux("send-keys", "-H", "-t", self._target(name), *codes)

    def paste(self, name: str, text: str) -> None:
        """Paste as one block; -p uses bracketed paste when the program asked for it."""
        if text:
            buffer = f"agentwatch-{time.monotonic_ns()}"
            self._tmux("load-buffer", "-b", buffer, "-", stdin=text)
            self._tmux("paste-buffer", "-p", "-d", "-r", "-b", buffer, "-t", self._target(name))


# Textual key name -> tmux key name, for keys that are not plain characters.
KEYS = {"enter": "Enter", "tab": "Tab", "shift+tab": "BTab", "escape": "Escape", "backspace": "BSpace",
        "delete": "DC", "insert": "IC", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "home": "Home", "end": "End", "pageup": "PPage", "pagedown": "NPage", "space": "Space"}
NEWLINE = ("1b", "0d")                   # ESC CR: Claude Code reads it as "insert a newline"


def translate(key: str, character: str | None) -> tuple[str, tuple[str, ...]]:
    """Map a Textual key to ("text" | "keys" | "hex" | "", payload)."""
    if key in ("shift+enter", "alt+enter"):
        return "hex", NEWLINE
    if key in KEYS:
        return "keys", (KEYS[key],)
    if character and character.isprintable() and "+" not in key.replace("plus", ""):
        return "text", (character,)
    mods, _, base = key.rpartition("+")
    prefix = "".join({"ctrl": "C-", "alt": "M-", "shift": "S-"}.get(m, "") for m in mods.split("+") if m)
    if base in KEYS:
        return "keys", (prefix + KEYS[base],)
    if re.fullmatch(r"f\d{1,2}", base):
        return "keys", (prefix + base.upper(),)
    if len(base) == 1 and prefix:
        return "keys", (prefix + base,)
    if character and character.isprintable():
        return "text", (character,)
    return "", ()
