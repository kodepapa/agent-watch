"""The Textual app: layout, polling loop and key bindings."""
from __future__ import annotations

import os
import time

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from . import theme as th
from .herdr import HerdrError
from .model import State
from .mux import MuxError
from .panels import (
    WINDOWS,
    AdvisorPanel,
    HeaderBar,
    LeadPanel,
    LogPanel,
    Panel,
    StatusBar,
    TaskPanel,
    TeamPanel,
    TimelinePanel,
    TokensPanel,
)
from .term import SessionList, TerminalView, th_home

PREFIX_KEYS = "c new   1-9 switch   n/p next/prev   d dashboard   x close   pgup/pgdn scroll   q detach"


class NewSession(ModalScreen):
    """Ask which folder to start a session in."""

    DEFAULT_CSS = f"""
    NewSession {{ align: center middle; background: {th.BG} 60%; }}
    NewSession > Vertical {{ width: 72; height: 7; background: {th.PANEL}; border: round {th.FAINT}; padding: 1 2; }}
    NewSession Input {{ background: {th.BG}; color: {th.INK}; border: none; height: 1; padding: 0 1; }}
    NewSession Static {{ color: {th.DIM}; height: 1; }}
    """
    BINDINGS = [("escape", "dismiss(None)", "cancel")]

    def __init__(self, folder: str, command: str):
        super().__init__()
        self.folder = folder
        self.command = command

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"start {self.command} in which folder?")
            yield Input(value=self.folder)
            yield Static("")
            yield Static("enter start   esc cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


class AgentWatchApp(App):
    TITLE = "agentwatch"
    CSS = f"""
    Screen {{ background: {th.BG}; color: {th.INK}; layout: vertical; }}
    #workspace {{ height: 1fr; }}
    #sessions {{ width: 34; }}
    #terminal {{ width: 1fr; }}
    #dashboard {{ height: 1fr; }}
    #header {{ height: 2; }}
    #main {{ height: 1fr; }}
    #advisor {{ width: 32; }}
    #right {{ width: 1fr; }}
    #lead {{ height: 4; }}
    #tasks {{ height: 1fr; min-height: 5; }}
    #team {{ height: 6; }}
    #timeline {{ height: 10; }}
    #bottom {{ height: 9; }}
    #log {{ width: 1fr; }}
    #tokens {{ width: 36; }}
    #status {{ height: 1; }}
    """
    BINDINGS = [("q", "quit", "quit"), ("p", "toggle_pause", "pause"),
                ("t", "next_team", "team"), ("w", "zoom", "window"),
                ("plus,equals_sign", "faster", "faster"), ("minus", "slower", "slower"),
                *[(str(n), f"jump({n})", f"jump {n}") for n in range(1, 10)]]

    def __init__(self, collector, mode: str = "live", advisor_model: str = "",
                 tick: float = 0.25, events_hint: str = "", herdr=None, leave_after_jump: bool = False,
                 mux=None, prefix: str = "ctrl+b", view: str = "sessions"):
        super().__init__()
        self.collector = collector
        self.mux = mux                           # mux.Mux when agentwatch hosts the sessions, else None
        self.prefix = prefix
        self.prefix_armed = False
        self.closing = ""                        # session waiting for a y to confirm closing it
        self.view = view if mux else "dashboard"
        self.sessions: list = []
        self.active = ""
        self.herdr = herdr                       # herdr.Herdr client, or None outside herdr
        self.leave_after_jump = leave_after_jump
        self.mode = mode
        self.advisor_model = advisor_model
        self.tick_s = tick
        self.events_hint = events_hint
        self.frame = 0
        self.paused = False
        self.window_idx = 0
        self.state = self._new_state()

    def _new_state(self) -> State:
        th.reset_colors()
        clock = getattr(self.collector, "clock", time.time)
        return State(clock=clock, advisor_model=self.advisor_model)

    @property
    def mode_label(self) -> str:
        if self.paused:
            return "paused"
        if self.mode == "replay":
            speed = getattr(self.collector, "speed", 1)
            done = getattr(self.collector, "done", False)
            return f"replay {speed:g}x" + (" · end" if done else "")
        return self.mode

    def compose(self) -> ComposeResult:
        if self.mux:
            with Horizontal(id="workspace"):
                yield SessionList(id="sessions")
                yield TerminalView(self.mux, id="terminal")
        with Vertical(id="dashboard"):
            yield HeaderBar(id="header")
            with Horizontal(id="main"):
                yield AdvisorPanel(id="advisor")
                with Vertical(id="right"):
                    yield LeadPanel(id="lead")
                    yield TaskPanel(id="tasks")
                    yield TeamPanel(id="team")
            yield TimelinePanel(id="timeline")
            with Horizontal(id="bottom"):
                yield LogPanel(id="log")
                yield TokensPanel(id="tokens")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        if self.mux:
            self._refresh_sessions()
            self.show_view(self.view)
        self.tick()
        self.set_interval(self.tick_s, self.tick)

    # ---- hosted sessions -------------------------------------------------
    @property
    def terminal(self) -> TerminalView:
        return self.query_one("#terminal", TerminalView)

    def show_view(self, view: str) -> None:
        self.view = view
        self.query_one("#workspace").display = view == "sessions"
        self.query_one("#dashboard").display = view == "dashboard"
        self.set_focus(self.terminal if view == "sessions" else None)

    def _refresh_sessions(self) -> None:
        try:
            self.sessions = self.mux.sessions()
        except MuxError:
            return
        names = [s.name for s in self.sessions]
        if self.active not in names:
            self.active = names[-1] if names else ""
        self.terminal.show(self.active)

    def select_session(self, name: str) -> None:
        self.active = name
        self.terminal.show(name)
        if self.view != "sessions":
            self.show_view("sessions")
        else:
            self.set_focus(self.terminal)

    def _step_session(self, step: int) -> None:
        names = [s.name for s in self.sessions]
        if names:
            at = names.index(self.active) if self.active in names else 0
            self.select_session(names[(at + step) % len(names)])

    def new_session(self) -> None:
        current = next((s for s in self.sessions if s.name == self.active), None)
        folder = th_home(current.cwd if current and current.cwd else os.getcwd())

        def start(folder: str | None) -> None:
            if self.view == "sessions":
                self.set_focus(self.terminal)
            if not folder:
                return
            size = self.terminal.size
            try:
                name = self.mux.new(folder, size=(max(size.width, 40), max(size.height, 10)))
            except MuxError as e:
                self.notify(f"could not start a session: {e}", severity="warning", timeout=5)
                return
            self._refresh_sessions()
            self.select_session(name)

        self.push_screen(NewSession(folder, self.mux.command), start)

    def handle_prefix(self, key: str) -> bool:
        """Prefix-key handling shared by the terminal and the dashboard. True when the key was ours."""
        if not self.mux:
            return False
        if self.closing:
            name, self.closing = self.closing, ""
            if key == "y":
                self.mux.kill(name)
                self._refresh_sessions()
            return True
        if not self.prefix_armed:
            if key != self.prefix:
                return False
            self.prefix_armed = True
            return True
        self.prefix_armed = False
        if key == self.prefix:                   # prefix twice: pass one through to the session
            self.terminal.send("keys", ("C-" + key.rpartition("+")[2],))
        elif key == "c":
            self.new_session()
        elif key == "d":
            self.show_view("dashboard" if self.view == "sessions" else "sessions")
        elif key in ("n", "p"):
            self._step_session(1 if key == "n" else -1)
        elif key.isdigit() and 0 < int(key) <= len(self.sessions):
            self.select_session(self.sessions[int(key) - 1].name)
        elif key == "x" and self.active:
            self.closing = self.active
        elif key in ("pageup", "pagedown"):
            self.terminal.scroll_by(self.terminal.size.height // 2 * (1 if key == "pageup" else -1))
        elif key == "q":
            self.exit()
        elif key in ("question_mark", "h"):
            self.notify(f"{self.prefix} then:   {PREFIX_KEYS}", timeout=8)
        return True

    def on_key(self, event) -> None:
        # Keys reach the app only when the terminal is not focused (dashboard view).
        if self.handle_prefix(event.key):
            event.stop()
            event.prevent_default()

    async def action_quit(self) -> None:
        # ctrl+q is a priority binding in Textual; inside a session it belongs to the session.
        if self.mux and self.view == "sessions" and self.focused is self.terminal:
            self.terminal.send("keys", ("C-q",))
            return
        self.exit()

    def tick(self) -> None:
        self.frame += 1
        if self.mux and self.frame % 4 == 0:
            self._refresh_sessions()
        if hasattr(self.collector, "paused"):
            self.collector.paused = self.paused
        if not self.paused or self.mode == "replay":
            events = self.collector.poll()
            if events:
                self.state.apply_all(events)
        if getattr(self.collector, "team", "") and not self.state.team:
            self.state.team = self.collector.team
        self._fit_heights()
        for panel in self.query(Panel):
            panel.refresh()

    def _fit_heights(self) -> None:
        team = self.query_one("#team", TeamPanel)
        mates = len(self.state.teammates())
        width = self.query_one("#right").size.width or 80
        main_h = self.query_one("#main").size.height or 24
        max_rows = max(1, (main_h - 4 - 8) // TeamPanel.CARD_H)
        if mates:
            _, rows = team.layout_for(width, mates)
            want = TeamPanel.CARD_H * min(rows, 2, max_rows)
        else:
            want = 3
        if team.styles.height is None or team.styles.height.value != want:
            team.styles.height = want
        timeline = self.query_one("#timeline", TimelinePanel)
        want_t = max(5, min(len(timeline.rows()) + 4, 16))
        if timeline.styles.height is None or timeline.styles.height.value != want_t:
            timeline.styles.height = want_t

    # ---- actions -------------------------------------------------------
    def action_toggle_pause(self) -> None:
        self.paused = not self.paused

    def action_zoom(self) -> None:
        self.window_idx = (self.window_idx + 1) % len(WINDOWS)

    def action_next_team(self) -> None:
        teams = self.collector.teams()
        if len(teams) < 2:
            self.notify("only one team found", timeout=2)
            return
        current = self.state.team or getattr(self.collector, "team", "")
        nxt = teams[(teams.index(current) + 1) % len(teams)] if current in teams else teams[0]
        self.collector.reset(nxt)
        self.state = self._new_state()
        self.notify(f"watching team {nxt}", timeout=2)

    def action_jump(self, n: int) -> None:
        """Go to the nth agent: its hosted session, or its herdr pane."""
        targets = self.state.jump_targets()
        if n > len(targets):
            return
        agent = targets[n - 1]
        if self.mux and agent.term in [s.name for s in self.sessions]:
            self.select_session(agent.term)
            return
        if not self.herdr or not agent.pane:
            return
        self.run_worker(lambda: self._jump(agent.name, agent.pane), thread=True, exclusive=True)

    def _jump(self, name: str, pane: str) -> None:
        try:
            self.herdr.focus(pane)
        except HerdrError as e:
            self.call_from_thread(self.notify, f"could not focus {name} in {pane}: {e}",
                                  severity="warning", timeout=4)
            return
        if self.leave_after_jump:
            self.call_from_thread(self.exit)

    def action_faster(self) -> None:
        if hasattr(self.collector, "speed"):
            self.collector.speed = min(64, self.collector.speed * 2)

    def action_slower(self) -> None:
        if hasattr(self.collector, "speed"):
            self.collector.speed = max(0.25, self.collector.speed / 2)
