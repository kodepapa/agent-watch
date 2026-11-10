"""Widgets for hosted sessions: the live terminal and the session list."""
from __future__ import annotations

import contextlib
import os
import queue
import threading
import time

from rich.text import Text
from textual import events
from textual.widget import Widget

from . import theme as th
from .herdr import HerdrError, HerdrHost
from .mux import Mux, MuxError, Screen
from .panels import Panel, badge, fmt_span, strip

FAST, SLOW = 0.04, 0.25                  # seconds between captures: while busy, while quiet


class TerminalView(Widget, can_focus=True):
    """Draws one tmux session and sends it everything typed here."""

    DEFAULT_CSS = f"TerminalView {{ background: {th.BG}; }}"

    def __init__(self, mux: Mux | None = None, herdr: HerdrHost | None = None, **kwargs):
        super().__init__(**kwargs)
        self.mux = mux
        self.herdr = herdr
        self.session = ""
        self.screen_data: Screen | None = None
        self.scroll = 0
        self._outbox: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy_until = 0.0

    def on_mount(self) -> None:
        threading.Thread(target=self._send_loop, name="term-send", daemon=True).start()
        threading.Thread(target=self._capture_loop, name="term-capture", daemon=True).start()

    def on_unmount(self) -> None:
        self._stop.set()
        self._wake.set()
        self._outbox.put(None)

    def host(self, name: str):
        """Whoever runs the session: herdr for its panes, our tmux server otherwise."""
        return self.herdr if self.herdr and HerdrHost.owns(name) else self.mux

    def _later(self, name: str, call) -> None:
        host = self.host(name)
        if host:
            self._outbox.put(lambda: call(host))

    # ---- what is shown ---------------------------------------------------
    def show(self, name: str) -> None:
        if name != self.session:
            self.session = name
            self.scroll = 0
            self.screen_data = None
            self._fit()
            self.refresh()
        self.poke()

    def poke(self) -> None:
        self._busy_until = time.monotonic() + 1.5
        self._wake.set()

    def _fit(self) -> None:
        name, (w, h) = self.session, (self.size.width, self.size.height)
        if name and w and h:
            self._later(name, lambda host: host.resize(name, w, h))

    def on_resize(self, event: events.Resize) -> None:
        self._fit()
        self.poke()

    def _capture_loop(self) -> None:
        while not self._stop.is_set():
            name, scroll = self.session, self.scroll
            host = self.host(name) if name else None
            if host:
                try:
                    shot = host.capture(name, scroll, self.size.height)
                except (MuxError, HerdrError):
                    shot = None              # the session ended; the app notices on its next tick
                if name == self.session and shot != self.screen_data:
                    self.screen_data = shot
                    self._busy_until = time.monotonic() + 1.5
                    try:
                        self.app.call_from_thread(self.refresh)
                    except Exception:        # noqa: BLE001  the app is shutting down
                        return
            self._wake.clear()
            self._wake.wait(FAST if time.monotonic() < self._busy_until else SLOW)

    def _send_loop(self) -> None:
        while True:
            job = self._outbox.get()
            if job is None:
                return
            with contextlib.suppress(MuxError, HerdrError):   # the session is gone; nothing to deliver to
                job()
            self._wake.set()

    def render(self) -> Text:
        w, h = self.size.width, self.size.height
        shot = self.screen_data
        if not self.session or shot is None:
            return self._placeholder(w, h)
        rows = [Text.from_ansi(line) for line in shot.lines[:h]]
        x, y = shot.cursor
        if shot.cursor_visible and self.has_focus and y < len(rows):
            row = rows[y]
            if len(row) <= x:
                row.append(" " * (x + 1 - len(row)))
            row.stylize("reverse", x, x + 1)
        out = Text("\n").join(rows)
        out.no_wrap, out.overflow = True, "crop"
        return out

    def _placeholder(self, w: int, h: int) -> Text:
        prefix = self.app.prefix
        if self.session:
            lines = [Text("connecting", style=th.DIM)]
        else:
            lines = [Text("no sessions yet", style=f"bold {th.INK}"), Text(""),
                     Text(f"{prefix} then c    start {self.app.command} in a folder", style=th.DIM),
                     Text(f"{prefix} then d    dashboard", style=th.DIM),
                     Text(f"{prefix} then ?    all keys", style=th.DIM)]
        pad = [Text("")] * max(0, (h - len(lines)) // 2)
        out = Text("\n").join(pad + [Text(" " * max(0, (w - 40) // 2)) + line for line in lines])
        out.no_wrap, out.overflow = True, "crop"
        return out

    # ---- input -----------------------------------------------------------
    def press(self, key: str, character: str | None = None) -> None:
        name = self.session
        if not name:
            return
        if self.scroll:                      # typing returns to the live screen
            self.scroll = 0
        self._later(name, lambda host: host.press(name, key, character))
        self.poke()

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        if self.app.handle_prefix(event.key):
            return
        self.press(event.key, event.character)

    def on_paste(self, event: events.Paste) -> None:
        event.stop()
        name, text = self.session, event.text
        if name and text:
            self._later(name, lambda host: host.paste(name, text))
            self.poke()

    def scroll_by(self, lines: int) -> None:
        history = self.screen_data.history if self.screen_data else 0
        self.scroll = max(0, min(history, self.scroll + lines))
        self.poke()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self.scroll_by(3)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self.scroll_by(-3)


class SessionList(Panel):
    """Every hosted session with the state agentwatch knows for it. Click to switch."""

    ROW_H = 4                            # name, what it is doing, activity strip, gap
    SPAN = 600                           # seconds covered by the activity strip

    def draw(self, w, h):
        app = self.app
        s = self.state
        inner = w - 4
        mine = [a for a in s.agents.values() if a.role != "subagent"]
        by_term = {a.term: a for a in mine if a.term}
        by_pane = {a.pane: a for a in mine if a.pane}
        lines: list[Text] = []
        for i, sess in enumerate(app.sessions):
            agent = by_pane.get(sess.pane) if sess.host == "herdr" else by_term.get(sess.name)
            selected = sess.name == app.active
            color = th.agent_color(agent.name, agent.color) if agent else th.DIM
            left = Text(f"{i + 1} " if i < 9 else "  ", style=f"bold {th.INK}" if selected else th.FAINT)
            left.append(sess.title or (agent.name if agent and sess.host == "herdr" else "")
                        or (sess.pane if sess.host == "herdr" else sess.name), style=f"bold {color}")
            if agent:
                right = badge(agent)
                if agent.since:
                    right.append(" " + fmt_span(max(0, s.now() - agent.since)), style=th.DIM)
            elif sess.status:                # no hook events, but herdr reads the agent's screen
                right = herdr_badge(sess.status)
            else:
                right = Text("no events yet", style=th.FAINT)
            row = th.spread(left, right, inner)
            busy = agent and (agent.state == "working" or agent.blocked) and agent.current
            detail = th.fit(Text("  " + (agent.current if busy else th_home(sess.cwd)),
                                 style=th.INK if busy else th.DIM), inner)
            if agent:
                trail = Text("  ") + strip(s, agent, inner - 2, self.SPAN)
            else:
                via = f"{sess.command or 'agent'} in herdr {sess.pane}" if sess.host == "herdr" else sess.command
                trail = Text("  " + (via or ""), style=th.FAINT)
            trail = th.fit(trail, inner)
            if selected:
                for line in (row, detail, trail):
                    line.stylize(f"on {th.SELECT}")
            lines += [row, detail, trail, Text("")]
        if not app.sessions:
            lines = [Text("none yet", style=th.DIM), Text(f"{app.prefix} then c", style=th.FAINT)]
        elif len(lines) < h - 3:
            lines += [Text("")] * (h - 3 - len(lines))
            lines.append(Text(f"strip: last {fmt_span(self.SPAN)}", style=th.FAINT))
        title = Text("sessions", style=f"bold {th.INK}")
        waiting = sum(1 for sess in app.sessions
                      if (by_pane.get(sess.pane) if sess.host == "herdr" else by_term.get(sess.name)) is None
                      and sess.status == "blocked")
        waiting += sum(1 for a in mine if a.blocked and (a.term or a.pane))
        right = Text(f"{waiting} need you", style=f"bold {th.WARN}") if waiting else ""
        return th.box(w, h, title, lines, right=right)

    def on_click(self, event: events.Click) -> None:
        index = (event.y - 1) // self.ROW_H
        if 0 <= index < len(self.app.sessions):
            self.app.select_session(self.app.sessions[index].name)


def herdr_badge(status: str) -> Text:
    """herdr's status for an agent agentwatch has no hook events for."""
    label, color = {"blocked": ("needs you", th.WARN), "working": ("working", th.OK),
                    "done": ("done", th.INK)}.get(status, (status, th.DIM))
    t = Text(th.G["status"] + " ", style=color)
    t.append(label, style=f"bold {color}" if status in ("blocked", "working") else color)
    return t


def th_home(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path
