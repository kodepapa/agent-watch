"""The Textual app: layout, polling loop and key bindings."""
from __future__ import annotations

import time

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical

from . import theme as th
from .model import State
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


class AgentWatchApp(App):
    TITLE = "agentwatch"
    CSS = f"""
    Screen {{ background: {th.BG}; color: {th.INK}; layout: vertical; }}
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
                ("plus,equals_sign", "faster", "faster"), ("minus", "slower", "slower")]

    def __init__(self, collector, mode: str = "live", advisor_model: str = "",
                 tick: float = 0.25, events_hint: str = ""):
        super().__init__()
        self.collector = collector
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
        self.tick()
        self.set_interval(self.tick_s, self.tick)

    def tick(self) -> None:
        self.frame += 1
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

    def action_faster(self) -> None:
        if hasattr(self.collector, "speed"):
            self.collector.speed = min(64, self.collector.speed * 2)

    def action_slower(self) -> None:
        if hasattr(self.collector, "speed"):
            self.collector.speed = max(0.25, self.collector.speed / 2)
