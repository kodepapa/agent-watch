"""Dashboard panels. Each one draws its own ASCII box from `app.state`."""
from __future__ import annotations

import math
import textwrap
import time

from rich.text import Text
from textual.widget import Widget

from . import theme as th
from .model import LEAD, Agent, State

WINDOWS = [None, 120, 600, 1800, 7200]          # None = fit the whole session


def clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def fmt_span(seconds: float) -> str:
    if seconds < 120:
        return f"{int(seconds)}s"
    if seconds < 7200:
        return f"{int(round(seconds / 60))}m"
    return f"{seconds / 3600:.1f}h"


def badge(a: Agent) -> Text:
    state, color = a.state, {"working": th.OK, "idle": th.DIM, "done": th.INK,
                             "stopped": th.FAINT, "waiting": th.FAINT}.get(a.state, th.DIM)
    if a.last_error and a.state == "working":
        state, color = "error", th.ERROR
    if a.blocked:                       # herdr sees an approval or question on screen
        state, color = "needs you", th.WARN
    t = Text(th.G["status"] + " ", style=color)
    t.append(state, style=f"bold {color}" if state in ("working", "error", "needs you") else color)
    return t


def pane_tag(s: State, a: Agent) -> Text:
    """Jump key and herdr pane id for an agent, empty when it has no pane."""
    t = Text()
    targets = s.jump_targets()
    if a in targets:
        t.append(f"{targets.index(a) + 1} ", style=f"bold {th.INK}")
    if a.pane:
        t.append(a.pane, style=th.DIM)
    return t


class Panel(Widget):
    DEFAULT_CSS = "Panel { background: #1c1f26; }"

    @property
    def state(self) -> State:
        return self.app.state

    def draw(self, w: int, h: int) -> list[Text]:
        return []

    def render(self) -> Text:
        w, h = self.size.width, self.size.height
        if w <= 0 or h <= 0:
            return Text("")
        return Text("\n").join(self.draw(w, h)[:h])


class HeaderBar(Panel):
    def draw(self, w, h):
        s = self.state
        left = Text()
        left.append("agentwatch", style=f"bold {th.INK}")
        if s.team:
            left.append("   team ", style=th.DIM)
            left.append(s.team, style=th.INK)
        mode = self.app.mode_label
        left.append("   ")
        left.append(mode, style=f"bold {th.WARN if mode == 'paused' else th.OK}")
        if s.herdr_connected:
            left.append("   herdr", style=th.DIM)
        right = Text(clock(s.now()), style=th.DIM)
        legend = Text()
        names = [a.name for a in s.ordered_agents()]
        if s.advisor_calls or s.advisor_model:
            names.append("advisor")
        for name in names:
            color = th.agent_color(name, s.agents[name].color if name in s.agents else "")
            legend.append(th.G["legend"] + " ", style=color)
            legend.append(name + "   ", style=th.DIM)
        if not names:
            legend.append("no agents yet", style=th.FAINT)
        return [th.spread(left, right, w), th.fit(legend, w)]


class AdvisorPanel(Panel):
    def draw(self, w, h):
        s = self.state
        inner = w - 4
        calls = s.advisor_calls
        lines: list[Text] = []
        model = Text(s.advisor_model or "model not reported",
                     style=f"bold {th.ADVISOR}" if s.advisor_model else th.DIM)
        if calls:
            model.append(" · on call", style=th.DIM)
        lines.append(model)
        stats = Text("calls ", style=th.DIM)
        stats.append(str(len(calls)), style=f"bold {th.INK}")
        if calls:
            lines.append(th.spread(stats, Text(th.ago(s.now() - calls[-1].ts), style=th.DIM), inner))
        else:
            lines.append(stats)
        lines.append(Text(""))

        if not calls:
            for msg in ("waiting for the first", "advisor call.", "",
                        "start claude with", "--advisor <model>", "to turn it on."):
                lines.append(Text(msg, style=th.ADVISOR if msg.startswith("--") else th.DIM))
            return th.box(w, h, Text("advisor", style=f"bold {th.ADVISOR}"), lines,
                          border=th.ADVISOR, heavy=True)

        def entry(c, advice_lines: int) -> list[Text]:
            e = []
            head = Text(th.G["call"] + " ", style=th.ADVISOR)
            head.append(clock(c.ts), style=th.DIM)
            head.append(" ")
            head.append(c.moment, style=f"bold {th.ADVISOR}")
            e.append(head)
            ctx = Text(th.G["rail"] + "  ", style=th.FAINT)
            ctx.append(c.agent, style=th.agent_color(c.agent))
            if c.context:
                ctx.append(" · " + c.context, style=th.DIM)
            e.append(ctx)
            if c.advice and advice_lines:
                wrapped = textwrap.wrap(c.advice.replace("\n", " "), max(8, inner - 5))
                if len(wrapped) > advice_lines:
                    wrapped = wrapped[:advice_lines]
                    wrapped[-1] = wrapped[-1].rstrip() + "…"
                for i, part in enumerate(wrapped):
                    t = Text(th.G["rail"] + "  ", style=th.FAINT)
                    t.append(th.G["advice"] + " " if i == 0 else "  ", style=th.ADVISOR)
                    t.append(part, style=th.INK)
                    e.append(t)
            e.append(Text(th.G["rail"], style=th.FAINT))
            return e

        body_h = h - 2
        avail = body_h - len(lines) - 1
        newest = list(reversed(calls))
        minimal = [entry(c, 0) for c in newest]
        k, used = 0, 0
        for i, e in enumerate(minimal):
            reserve = 1 if i + 1 < len(newest) else 0
            if used + len(e) > avail - reserve:
                break
            used += len(e)
            k += 1
        room = avail - used - (1 if k < len(newest) else 0)
        picked: list[list[Text]] = []
        for i, c in enumerate(newest[:k]):
            best = minimal[i]
            for level in range(8 if i == 0 else 3, 0, -1):
                e = entry(c, level)
                if len(e) - len(minimal[i]) <= room:
                    room -= len(e) - len(minimal[i])
                    best = e
                    break
            picked.insert(0, best)
        hidden = len(calls) - len(picked)
        if hidden:
            lines.append(Text(f"+{hidden} earlier", style=th.FAINT))
        for e in picked:
            lines.extend(e)
        while len(lines) < body_h - 1:
            rail = th.G["rail"]
            lines.append(Text(rail, style=th.FAINT) if lines[-1].plain.startswith(rail) else Text(""))
        lines = lines[:body_h - 1]
        lines.append(Text("moments are inferred", style=th.FAINT))
        return th.box(w, h, Text("advisor", style=f"bold {th.ADVISOR}"), lines,
                      border=th.ADVISOR, heavy=True)


class LeadPanel(Panel):
    def draw(self, w, h):
        s = self.state
        inner = w - 4
        lead = s.agents.get(LEAD)
        title = Text("lead", style=f"bold {th.LEAD}")
        if not lead:
            return th.box(w, h, title, [Text("waiting for the lead session to send its first event",
                                             style=th.DIM)], border=th.LEAD, heavy=True)
        l1 = badge(lead)
        l1.append("  now ", style=th.DIM)
        l1.append(lead.current or "-", style=th.INK)
        l1 = th.spread(l1, Text(th.ago(s.now() - lead.last_ts), style=th.DIM), inner)
        l2 = Text()
        for label, value, style in (
                ("ctx ", th.human_tokens(lead.context_tokens) if lead.context_tokens else "-", th.INK),
                ("calls ", str(lead.tool_calls), th.INK),
                ("errors ", str(lead.errors), th.ERROR if lead.errors else th.INK),
                ("subagents ", str(lead.subagents_running), th.INK)):
            l2.append(label, style=th.DIM)
            l2.append(value + "    ", style=style)
        if lead.pane:
            l2 = th.spread(l2, pane_tag(s, lead), inner)
        right = Text(lead.model or "model unknown", style=th.LEAD if lead.model else th.DIM)
        return th.box(w, h, title, [l1, l2], border=th.LEAD, heavy=True, right=right)


class TaskPanel(Panel):
    BAR = 14

    def draw(self, w, h):
        s = self.state
        inner = w - 4
        done, total = s.task_counts()
        title = Text("tasks", style=f"bold {th.INK}")
        title.append(f" · {done}/{total} done" if total else "", style=th.DIM)
        if not total:
            return th.box(w, h, title, [
                Text("no task list yet", style=th.DIM),
                Text("tasks appear here when the lead creates a team or a task list", style=th.FAINT)])
        owner_w, state_w, upd_w = 11, 8, 9
        spare = inner - owner_w - self.BAR - state_w - 3
        subj_w = max(10, min(44, spare))
        show_upd = spare - subj_w >= upd_w + 2
        header = Text()
        header.append_text(th.fit("task", subj_w, th.FAINT))
        header.append(" ")
        header.append_text(th.fit("owner", owner_w, th.FAINT))
        header.append(" ")
        header.append_text(th.fit("", self.BAR))
        header.append(" ")
        header.append_text(th.fit("state", state_w, th.FAINT))
        if show_upd:
            header.append(" " * (spare - subj_w - upd_w))
            header.append(f"{'updated':>{upd_w}}", style=th.FAINT)

        def open_blockers(t):
            return [b for b in t.blocked_by if str(b) in s.tasks
                    and s.tasks[str(b)].status != "completed"]

        def rank(t):
            if t.status == "in_progress":
                return (0, t.updated_ts)
            if t.status == "pending":
                return (2 if open_blockers(t) else 1, _num(t.id))
            return (3, -t.updated_ts)

        tasks = sorted(s.tasks.values(), key=rank)
        rows = []
        for t in tasks:
            row = Text()
            row.append_text(th.fit(t.subject, subj_w, th.DIM if t.status == "completed" else th.INK))
            row.append(" ")
            color = th.agent_color(t.owner) if t.owner else th.FAINT
            row.append_text(th.fit(t.owner or "-", owner_w, color))
            row.append(" ")
            blockers = open_blockers(t)
            if t.status == "completed":
                row.append(th.G["done"] * self.BAR, style=th.OK)
                word, wstyle = "done", th.OK
            elif t.status == "in_progress":
                row.append_text(self._shimmer(color))
                word, wstyle = "working", f"bold {color}"
            elif blockers:
                row.append_text(th.fit(f"waits on {len(blockers)}", self.BAR, th.WARN))
                word, wstyle = "blocked", th.WARN
            else:
                row.append(th.G["todo"] * self.BAR, style=th.FAINT)
                word, wstyle = "open", th.DIM
            row.append(" ")
            row.append_text(th.fit(word, state_w, wstyle))
            if show_upd:
                row.append(" " * (spare - subj_w - upd_w))
                row.append(f"{th.ago(s.now() - t.updated_ts) if t.updated_ts else '':>{upd_w}}",
                           style=th.FAINT)
            rows.append(row)
        body_h = h - 2
        lines = [header]
        room = body_h - 1
        if len(rows) > room:
            shown = rows[:room - 1]
            lines += shown
            lines.append(Text(f"+{len(rows) - len(shown)} more", style=th.FAINT))
        else:
            lines += rows
        return th.box(w, h, title, lines)

    def _shimmer(self, color: str) -> Text:
        n = self.BAR
        pos = self.app.frame % (n + 4) - 2
        t = Text()
        for i in range(n):
            if pos <= i < pos + 3:
                t.append(th.G["done"], style=f"bold {th.INK}")
            else:
                t.append(th.G["done"], style=color)
        return t


def _num(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return math.inf


class TeamPanel(Panel):
    CARD_H = 6
    MIN_W = 24

    def layout_for(self, width: int, n: int) -> tuple[int, int]:
        cols = max(1, min(n, (width + 1) // (self.MIN_W + 1)))
        rows = max(1, math.ceil(n / cols))
        return cols, rows

    def draw(self, w, h):
        s = self.state
        mates = s.teammates()
        if not mates:
            return th.box(w, h, Text("team", style=f"bold {th.INK}"),
                          [Text("no teammates or subagents yet", style=th.DIM)])
        cols, rows = self.layout_for(w, len(mates))
        rows = min(rows, max(1, h // self.CARD_H))
        card_w = (w - (cols - 1)) // cols
        slots = cols * rows
        shown = mates[:slots]
        hidden = len(mates) - len(shown)
        out: list[Text] = []
        for r in range(rows):
            blocks = []
            for c in range(cols):
                i = r * cols + c
                if i >= len(shown):
                    blocks.append([Text(" " * card_w)] * self.CARD_H)
                    continue
                more = hidden if (hidden and i == slots - 1) else 0
                blocks.append(self._card(shown[i], card_w, more))
            out += th.hjoin(blocks)
        return out + [Text("")] * (h - len(out))

    def _card(self, a: Agent, w: int, more: int) -> list[Text]:
        s = self.state
        inner = w - 4
        color = th.agent_color(a.name, a.color)
        owned = [t for t in s.tasks.values() if t.owner == a.name]
        active = [t for t in owned if t.status == "in_progress"]
        done = [t for t in owned if t.status == "completed"]
        l1 = th.spread(Text(a.model or a.role, style=th.DIM), badge(a), inner)
        l2 = Text("owns ", style=th.FAINT)
        if active:
            l2.append(active[0].subject, style=th.INK)
        elif owned:
            l2.append(f"{len(done)}/{len(owned)} tasks done", style=th.DIM)
        else:
            l2.append("-", style=th.FAINT)
        l3 = Text("> ", style=color)
        l3.append(a.current or "-", style=th.INK if a.state == "working" else th.DIM)
        stats = Text(f"{a.tool_calls} calls", style=th.DIM)
        if a.errors:
            stats.append(f"  {a.errors} err", style=th.ERROR)
        if more:
            stats = Text(f"+{more} more agents", style=th.WARN)
        since = th.ago(s.now() - a.last_ts).replace(" ago", "") if a.last_ts else ""
        l4 = th.spread(stats, Text(since, style=th.FAINT), inner)
        right = Text("sub", style=th.DIM) if a.role == "subagent" else pane_tag(s, a)
        return th.box(w, self.CARD_H, Text(a.name, style=f"bold {color}"), [l1, l2, l3, l4],
                      border=color, right=right)


class TimelinePanel(Panel):
    LABEL = 10

    def rows(self) -> list[str]:
        s = self.state
        rows = []
        if s.advisor_calls or "advisor" in s.marks:
            rows.append("advisor")
        if s.tasks or "tasks" in s.marks:
            rows.append("tasks")
        return rows + [a.name for a in s.ordered_agents()]

    def span(self) -> float:
        s = self.state
        fixed = WINDOWS[self.app.window_idx]
        if fixed:
            return fixed
        if not s.first_ts:
            return 120
        return min(max(s.now() - s.first_ts, 20) * 1.04, 7200)

    def draw(self, w, h):
        s = self.state
        inner = w - 4
        span = self.span()
        title = Text("timeline", style=f"bold {th.INK}")
        right = Text(f"last {fmt_span(span)}" + ("" if WINDOWS[self.app.window_idx] else " · fit"),
                     style=th.DIM)
        rows = self.rows()
        if not rows:
            return th.box(w, h, title, [Text("activity per agent shows up here", style=th.DIM)],
                          right=right)
        track = max(10, inner - self.LABEL)
        now = s.now()
        start = now - span
        per = span / track
        lines = []
        for name in rows:
            cells = [dict() for _ in range(track)]
            for ts, mark in reversed(s.marks.get(name, ())):
                if ts < start:
                    break
                i = min(track - 1, int((ts - start) / per))
                cells[i][mark] = cells[i].get(mark, 0) + 1
            if name not in ("advisor", "tasks"):
                self._fill_work(cells, per)
            color = th.agent_color(name, s.agents[name].color if name in s.agents else "")
            line = th.fit(Text(name, style=f"bold {color}"), self.LABEL)
            for i, c in enumerate(cells):
                line.append_text(self._cell(c, color, i == track - 1))
            lines.append(line)
        axis = [Text(th.G["todo"], style=th.FAINT) for _ in range(track)]
        labels = [(0, f"-{fmt_span(span)}"), (track // 2, f"-{fmt_span(span / 2)}"),
                  (track - 3, "now")]
        for pos, label in labels:
            for j, ch in enumerate(label):
                if 0 <= pos + j < track:
                    axis[pos + j] = Text(ch, style=th.DIM)
        axis_line = Text(" " * self.LABEL)
        for t in axis:
            axis_line.append_text(t)
        lines.append(axis_line)
        legend = Text(" " * self.LABEL)
        sp = th.G["spark"]
        for sym, style, label in ((sp[7], th.INK, "busy"), (sp[2], th.INK, "working"),
                                  (th.G["error"], th.ERROR, "error"),
                                  (th.G["advisor"], th.ADVISOR, "advisor"),
                                  (th.G["task_done"], th.OK, "task done"),
                                  (th.G["claim"], th.INK, "task claimed"),
                                  (th.G["prompt"], th.INK, "prompt"), (sp[1], th.DIM, "idle")):
            legend.append(sym, style=f"bold {style}")
            legend.append(f" {label}   ", style=th.FAINT)
        lines.append(legend)
        return th.box(w, h, title, lines, right=right)

    @staticmethod
    def _fill_work(cells: list, per: float, max_gap: float = 25.0) -> None:
        active = [i for i, c in enumerate(cells)
                  if any(k in c for k in ("tool", "error", "prompt", "task", "done"))]
        for a, b in zip(active, active[1:], strict=False):
            if b - a > 1 and (b - a) * per <= max_gap:
                if any(("idle" in cells[k] or "stop" in cells[k]) for k in range(a, b + 1)):
                    continue
                for k in range(a + 1, b):
                    cells[k]["work"] = 1

    @staticmethod
    def _cell(c: dict, color: str, is_now: bool) -> Text:
        g, sp = th.G, th.G["spark"]
        if c.get("error"):
            return Text(g["error"], style=f"bold {th.ERROR}")
        if c.get("advisor"):
            return Text(g["advisor"], style=f"bold {th.ADVISOR}")
        if c.get("done"):
            return Text(g["task_done"], style=f"bold {th.OK}")
        if c.get("task"):
            return Text(g["claim"], style=th.INK)
        if c.get("prompt"):
            return Text(g["prompt"], style=f"bold {color}")
        n = c.get("tool", 0)
        if n:
            return Text(sp[min(7, 3 + 2 * (n - 1))], style=color)
        if c.get("work"):
            return Text(sp[2], style=color)
        if c.get("idle") or c.get("stop"):
            return Text(sp[1], style=th.DIM)
        if is_now:
            return Text(g["now"], style=th.DIM)
        return Text(g["dot"], style=th.FAINT)


class LogPanel(Panel):
    LEVEL = {"error": th.ERROR, "advisor": th.ADVISOR, "task": th.OK, "warn": th.WARN,
             "info": th.INK}

    def draw(self, w, h):
        s = self.state
        title = Text("log", style=f"bold {th.INK}")
        right = Text(f"{s.events_total:,} events", style=th.DIM)
        if not s.log:
            return th.box(w, h, title, [
                Text("waiting for hook events", style=th.DIM),
                Text(f"reading {self.app.events_hint}", style=th.FAINT),
                Text("run `agentwatch install-hooks` once, then start claude", style=th.FAINT)],
                right=right)
        lines = []
        for entry in list(s.log)[-(h - 2):]:
            t = Text(clock(entry.ts), style=th.FAINT)
            t.append(" ")
            t.append_text(th.fit(entry.agent, 10, f"bold {th.agent_color(entry.agent)}"))
            t.append(" ")
            t.append(entry.text, style=self.LEVEL.get(entry.level, th.INK))
            lines.append(t)
        return th.box(w, h, title, lines, right=right, fill_from_bottom=True)


class TokensPanel(Panel):
    def draw(self, w, h):
        s = self.state
        inner = w - 4
        title = Text("tokens read", style=f"bold {th.INK}")
        agents = [a for a in s.ordered_agents() if a.tokens_read]
        if not agents:
            return th.box(w, h, title, [Text("from session transcripts", style=th.DIM),
                                        Text("appears after the first reply", style=th.FAINT)])
        top = max(a.tokens_read for a in agents)
        bar_w = max(4, inner - 10 - 7)
        lines = []
        for a in agents[: h - 4]:
            color = th.agent_color(a.name, a.color)
            t = th.fit(a.name, 10, color)
            n = max(1, round(bar_w * a.tokens_read / top))
            t.append(th.G["done"] * n, style=color)
            t.append(th.G["todo"] * (bar_w - n), style=th.FAINT)
            t.append(f"{th.human_tokens(a.tokens_read):>7}", style=th.INK)
            lines.append(t)
        total = sum(a.tokens_read for a in agents)
        lines.append(Text(""))
        total_text = Text(th.human_tokens(total), style=f"bold {th.INK}")
        lines.append(th.spread(Text("total", style=th.DIM), total_text, inner))
        return th.box(w, h, title, lines)


class StatusBar(Panel):
    def draw(self, w, h):
        s = self.state
        done, total = s.task_counts()
        mates = s.teammates()
        working = sum(1 for a in mates if a.state == "working")
        idle = sum(1 for a in mates if a.state in ("idle", "done", "stopped"))
        t = Text()

        def seg(label, value, style):
            t.append(label + " ", style=th.DIM)
            t.append(value, style=f"bold {style}")
            t.append("    ")

        seg("tasks", f"{done}/{total} done", th.OK if total and done == total else th.INK)
        seg("team", f"{working} working, {idle} idle", th.INK)
        n = len(s.advisor_calls)
        seg("advisor", f"{n} call" + ("" if n == 1 else "s"), th.ADVISOR)
        seg("errors", str(s.errors_total), th.ERROR if s.errors_total else th.INK)
        blocked = sum(1 for a in s.agents.values() if a.blocked)
        if blocked:
            seg("needs you", str(blocked), th.WARN)
        full = "q quit  p pause  t team  w window"
        options = [full]
        if s.jump_targets() and self.app.herdr:
            options = ["1-9 jump  " + full, "1-9 jump  q quit"]
        for keys in options:
            if len(t.plain.rstrip()) + len(keys) + 2 <= w:
                return [th.spread(t, Text(keys, style=th.FAINT), w)]
        return [th.fit(t, w)]
