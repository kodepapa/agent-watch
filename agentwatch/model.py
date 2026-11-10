"""Event model and the reducer that folds events into dashboard state.

Every source (hook events, team files, task files, transcripts, replays)
produces `Event`s. The UI only ever reads `State`, so live mode and replay
mode render identically.
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

LEAD = "lead"
# Events that show an agent is moving again after a permission prompt or question.
ANSWERED = {"tool_start", "tool_end", "prompt", "stop", "idle", "session_end", "interrupt"}


@dataclass
class Event:
    ts: float
    kind: str
    agent: str = ""
    session: str = ""
    detail: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Event:
        return cls(ts=float(d["ts"]), kind=d["kind"], agent=d.get("agent", ""),
                   session=d.get("session", ""), detail=d.get("detail", ""),
                   data=d.get("data") or {})


@dataclass
class Agent:
    name: str
    role: str = "teammate"          # lead | teammate | subagent
    model: str = ""
    color: str = ""
    state: str = "waiting"          # waiting | working | idle | done | stopped
    current: str = ""
    last_ts: float = 0.0
    first_ts: float = 0.0
    since: float = 0.0              # when the state shown on the badge last changed
    tool_calls: int = 0
    errors: int = 0
    last_error: bool = False
    last_error_sig: str = ""
    last_error_detail: str = ""
    last_error_ts: float = 0.0
    repeat_errors: int = 0
    tokens_read: int = 0
    context_tokens: int = 0
    calls_since_prompt: int = 0
    subagents_running: int = 0
    session: str = ""
    pane: str = ""                  # herdr pane id, when the session runs inside herdr
    workspace: str = ""             # herdr workspace label
    term: str = ""                  # hosted session name, when agentwatch runs the terminal
    herdr_blocked: bool = False     # herdr sees an approval or question on screen
    hook_blocked: bool = False      # Claude Code sent a permission or question notification

    @property
    def blocked(self) -> bool:
        """Stopped until the user answers."""
        return self.herdr_blocked or self.hook_blocked


@dataclass
class Task:
    id: str
    subject: str
    status: str = "pending"         # pending | in_progress | completed
    owner: str = ""
    blocked_by: list = field(default_factory=list)
    updated_ts: float = 0.0


@dataclass
class AdvisorCall:
    ts: float
    agent: str
    call_id: str = ""
    moment: str = ""                # inferred: before plan | after error | before done | mid-work
    context: str = ""               # what the agent did just before
    advice: str = ""


@dataclass
class LogLine:
    ts: float
    agent: str
    text: str
    level: str = "info"             # info | warn | error | advisor | task


class State:
    def __init__(self, clock: Callable[[], float] = time.time,
                 advisor_model: str = ""):
        self.clock = clock
        self.agents: dict[str, Agent] = {}
        self.tasks: dict[str, Task] = {}
        self.advisor_calls: list[AdvisorCall] = []
        self.advisor_model = advisor_model
        self.log: deque[LogLine] = deque(maxlen=400)
        self.marks: dict[str, deque] = {}       # row -> deque[(ts, mark)]
        self.team = ""
        self.first_ts = 0.0
        self.last_ts = 0.0
        self.events_total = 0
        self.errors_total = 0
        self.herdr_connected = False
        self.herdr_agents: list[dict] = []
        self._recent_task_logs: dict[tuple, float] = {}

    # ---- helpers -------------------------------------------------------
    def now(self) -> float:
        return self.clock()

    def agent(self, name: str, ts: float = 0.0) -> Agent:
        name = name or LEAD
        a = self.agents.get(name)
        if a is None:
            a = Agent(name=name, role=LEAD if name == LEAD else "teammate",
                      first_ts=ts)
            self.agents[name] = a
        return a

    def mark(self, row: str, ts: float, mark: str) -> None:
        self.marks.setdefault(row, deque(maxlen=6000)).append((ts, mark))

    def say(self, ts: float, agent: str, text: str, level: str = "info") -> None:
        self.log.append(LogLine(ts, agent, text, level))

    def ordered_agents(self) -> list[Agent]:
        lead = [a for a in self.agents.values() if a.role == LEAD]
        team = [a for a in self.agents.values() if a.role == "teammate"]
        subs = [a for a in self.agents.values() if a.role == "subagent"]
        return lead + team + subs

    def teammates(self) -> list[Agent]:
        return [a for a in self.ordered_agents() if a.role != LEAD]

    def jump_targets(self) -> list[Agent]:
        """Agents the number keys can jump to: a hosted session or a herdr pane."""
        return [a for a in self.ordered_agents() if (a.term or a.pane) and a.role != "subagent"][:9]

    def task_counts(self) -> tuple[int, int]:
        done = sum(1 for t in self.tasks.values() if t.status == "completed")
        return done, len(self.tasks)

    # ---- reducer -------------------------------------------------------
    def apply_all(self, events: list[Event]) -> None:
        for ev in sorted(events, key=lambda e: e.ts):
            self.apply(ev)

    def apply(self, ev: Event) -> None:
        self.events_total += 1
        if not self.first_ts or ev.ts < self.first_ts:
            self.first_ts = ev.ts
        self.last_ts = max(self.last_ts, ev.ts)
        before = {name: (a.state, a.blocked) for name, a in self.agents.items()}
        if ev.kind in ANSWERED and ev.agent in self.agents:
            self.agents[ev.agent].hook_blocked = False
        handler = getattr(self, "_on_" + ev.kind, None)
        if handler:
            handler(ev)
        for name, a in self.agents.items():
            now = (a.state, a.blocked)
            if before.get(name) != now:
                a.since = ev.ts
                was_blocked = before.get(name, ("", False))[1]
                if now[1] and not was_blocked:
                    self.mark(name, ev.ts, "wait")      # started waiting for the user
                elif was_blocked and not now[1] and a.state == "working":
                    self.mark(name, ev.ts, "go")        # answered: back to work

    def _touch(self, ev: Event) -> Agent:
        a = self.agent(ev.agent, ev.ts)
        if not a.first_ts:
            a.first_ts = ev.ts
        a.last_ts = max(a.last_ts, ev.ts)
        if ev.session and not a.session:
            a.session = ev.session
            if self.herdr_agents:
                self._sync_herdr(ev.ts)
        return a

    def _on_term(self, ev: Event) -> None:
        self.agent(ev.agent, ev.ts).term = ev.data.get("name", "")

    # ---- herdr ---------------------------------------------------------
    def _on_herdr_pane(self, ev: Event) -> None:
        a = self.agent(ev.agent, ev.ts)
        a.pane = ev.data.get("pane_id", "") or a.pane
        self._sync_herdr(ev.ts)

    def _on_herdr(self, ev: Event) -> None:
        self.herdr_connected = bool(ev.data.get("connected"))
        self.herdr_agents = list(ev.data.get("agents") or [])
        self._sync_herdr(ev.ts)

    def _sync_herdr(self, ts: float) -> None:
        by_pane = {h["pane_id"]: h for h in self.herdr_agents}
        by_session = {h["session"]: h for h in self.herdr_agents if h.get("session")}
        for a in self.agents.values():
            info = by_pane.get(a.pane) or (by_session.get(a.session) if a.session else None)
            if not info:
                a.herdr_blocked = False
                continue
            if a.role == "subagent":        # shares its parent's pane; the parent carries the status
                a.pane = a.pane or info["pane_id"]
                continue
            a.pane = a.pane or info["pane_id"]
            a.workspace = info.get("workspace", "")
            blocked = info.get("status") == "blocked"
            if blocked and not a.blocked:
                self.say(ts, a.name, f"needs you · waiting in herdr pane {a.pane}", "warn")
            a.herdr_blocked = blocked

    def _on_team(self, ev: Event) -> None:
        self.team = ev.data.get("team", self.team)
        for m in ev.data.get("members", []):
            a = self.agent(m["name"], ev.ts)
            a.role = m.get("role", a.role)
            a.model = m.get("model") or a.model
            a.color = m.get("color") or a.color
            if a.color:
                from .theme import agent_color
                agent_color(a.name, a.color)
        if ev.data.get("new_members"):
            for name in ev.data["new_members"]:
                self.say(ev.ts, name, "joined the team", "info")

    def _on_session_start(self, ev: Event) -> None:
        a = self._touch(ev)
        a.state = "idle"
        self.say(ev.ts, a.name, f"session started{(' · ' + ev.detail) if ev.detail else ''}")

    def _on_session_end(self, ev: Event) -> None:
        a = self._touch(ev)
        a.state = "stopped"
        a.current = "session ended"
        self.mark(a.name, ev.ts, "stop")
        self.say(ev.ts, a.name, "session ended")

    def _on_interrupt(self, ev: Event) -> None:
        # The user pressed escape mid-tool. Claude Code sends no Stop for this.
        a = self._touch(ev)
        a.state = "idle"
        a.current = "interrupted"
        self.mark(a.name, ev.ts, "stop")
        self.say(ev.ts, a.name, f"interrupted · {ev.detail}" if ev.detail else "interrupted", "warn")

    def _on_prompt(self, ev: Event) -> None:
        a = self._touch(ev)
        a.state = "working"
        a.calls_since_prompt = 0
        a.current = "thinking"
        self.mark(a.name, ev.ts, "prompt")
        self.say(ev.ts, a.name, f"prompt · {ev.detail}" if ev.detail else "prompt")

    def _on_tool_start(self, ev: Event) -> None:
        a = self._touch(ev)
        a.state = "working"
        a.current = ev.detail or ev.data.get("tool", "")
        a.tool_calls += 1
        a.calls_since_prompt += 1
        self.mark(a.name, ev.ts, "tool")

    def _on_tool_end(self, ev: Event) -> None:
        a = self._touch(ev)
        if not ev.data.get("error"):
            a.last_error = False
            if ev.detail and ev.detail == a.last_error_detail:
                a.repeat_errors = 0          # the failing call now succeeds
                a.last_error_sig = a.last_error_detail = ""
            return
        a.errors += 1
        self.errors_total += 1
        a.last_error = True
        sig = ev.data.get("error_sig", "")
        if sig and sig == a.last_error_sig and ev.ts - a.last_error_ts < 600:
            a.repeat_errors += 1
        else:
            a.repeat_errors = 1
        a.last_error_sig = sig
        a.last_error_detail = ev.detail
        a.last_error_ts = ev.ts
        self.mark(a.name, ev.ts, "error")
        what = ev.detail or ev.data.get("tool", "tool")
        if a.repeat_errors >= 2:
            self.say(ev.ts, a.name, f"same error x{a.repeat_errors} · {what}", "error")
        else:
            reason = ev.data.get("error_text", "")
            self.say(ev.ts, a.name, f"failed · {what}{(' · ' + reason) if reason else ''}", "error")

    def _on_subagent_start(self, ev: Event) -> None:
        parent = self._touch(ev)
        parent.subagents_running += 1
        name = ev.data.get("subagent", "")
        if name:
            sub = self.agent(name, ev.ts)
            sub.role = "subagent" if sub.role != LEAD else sub.role
            sub.state = "working"
            sub.current = ev.detail or "starting"
            sub.last_ts = ev.ts
            sub.first_ts = sub.first_ts or ev.ts
            self.mark(sub.name, ev.ts, "tool")
        self.say(ev.ts, parent.name, f"spawned {name or 'subagent'}{(' · ' + ev.detail) if ev.detail else ''}")

    def _on_subagent_stop(self, ev: Event) -> None:
        parent = self._touch(ev)
        parent.subagents_running = max(0, parent.subagents_running - 1)
        name = ev.data.get("subagent", "")
        if name and name in self.agents:
            sub = self.agents[name]
            sub.state = "done"
            sub.current = "finished"
            sub.last_ts = ev.ts
            self.mark(sub.name, ev.ts, "stop")
        self.say(ev.ts, name or parent.name, "subagent finished")

    def _on_idle(self, ev: Event) -> None:
        a = self._touch(ev)
        a.state = "idle"
        a.current = "waiting for work"
        self.mark(a.name, ev.ts, "idle")
        self.say(ev.ts, a.name, "went idle")

    def _on_stop(self, ev: Event) -> None:
        a = self._touch(ev)
        a.state = "idle" if a.role == LEAD else "done"
        a.current = "turn finished"
        self.mark(a.name, ev.ts, "stop")

    def _on_notification(self, ev: Event) -> None:
        a = self._touch(ev)
        if ev.data.get("attention") and a.role != "subagent":
            a.hook_blocked = True
        if ev.data.get("idle") and a.state == "working":
            # Claude Code says it is waiting for input, so a turn ended without a Stop
            # (an interrupt while thinking, for example).
            a.state = "idle"
            a.current = "waiting for input"
            self.mark(a.name, ev.ts, "stop")
        self.say(ev.ts, a.name, ev.detail or "notification", "warn")

    def _on_compact(self, ev: Event) -> None:
        a = self._touch(ev)
        self.say(ev.ts, a.name, "compacting context", "warn")

    def _on_model(self, ev: Event) -> None:
        a = self._touch(ev)
        if ev.data.get("model"):
            a.model = ev.data["model"]
        a.tokens_read += int(ev.data.get("tokens_in", 0))
        if ev.data.get("ctx_tokens"):
            a.context_tokens = int(ev.data["ctx_tokens"])

    def _task_log(self, ev: Event, task_id: str, what: str, text: str, agent: str) -> None:
        key = (task_id, what)
        last = self._recent_task_logs.get(key)
        if last is not None and abs(ev.ts - last) < 10:
            return
        self._recent_task_logs[key] = ev.ts
        self.say(ev.ts, agent, text, "task")

    def _on_task_upsert(self, ev: Event) -> None:
        d = ev.data
        tid = str(d.get("id", ""))
        if not tid:
            return
        prev = self.tasks.get(tid)
        t = Task(id=tid, subject=d.get("subject") or (prev.subject if prev else f"task {tid}"),
                 status=d.get("status", "pending"), owner=d.get("owner", ""),
                 blocked_by=list(d.get("blocked_by") or []), updated_ts=ev.ts)
        self.tasks[tid] = t
        who = t.owner or LEAD
        if not ev.data.get("live"):
            return                       # history snapshot: state only, no log lines
        if prev is None:
            self._task_log(ev, tid, "created", f"task added · {t.subject}", LEAD)
            self.mark("tasks", ev.ts, "task")
            return
        if t.owner and t.owner != prev.owner:
            self._task_log(ev, tid, "claimed", f"claimed · {t.subject}", t.owner)
            self.mark("tasks", ev.ts, "task")
            self.agent(t.owner, ev.ts)
        if t.status != prev.status:
            if t.status == "completed":
                self._task_log(ev, tid, "completed", f"done · {t.subject}", who)
                self.mark("tasks", ev.ts, "done")
            elif t.status == "in_progress":
                self._task_log(ev, tid, "started", f"started · {t.subject}", who)
                self.mark("tasks", ev.ts, "task")

    def _on_task_removed(self, ev: Event) -> None:
        self.tasks.pop(str(ev.data.get("id", "")), None)

    def _on_task_created(self, ev: Event) -> None:
        tid = str(ev.data.get("id", ""))
        subject = ev.data.get("subject") or (self.tasks[tid].subject if tid in self.tasks else "")
        self._touch(ev)
        if tid and tid not in self.tasks:
            self.tasks[tid] = Task(id=tid, subject=subject or f"task {tid}", updated_ts=ev.ts)
        self.mark("tasks", ev.ts, "task")
        self._task_log(ev, tid or subject, "created", f"task added · {subject or tid}", ev.agent or LEAD)

    def _on_task_completed(self, ev: Event) -> None:
        tid = str(ev.data.get("id", ""))
        subject = ev.data.get("subject") or (self.tasks[tid].subject if tid in self.tasks else "")
        a = self._touch(ev)
        if tid in self.tasks:
            self.tasks[tid].status = "completed"
            self.tasks[tid].updated_ts = ev.ts
        self.mark("tasks", ev.ts, "done")
        self._task_log(ev, tid or subject, "completed", f"done · {subject or tid}", a.name)

    def _on_advisor_call(self, ev: Event) -> None:
        a = self._touch(ev)
        if ev.data.get("model"):
            self.advisor_model = ev.data["model"]
        moment = self._infer_moment(a)
        call = AdvisorCall(ts=ev.ts, agent=a.name, call_id=ev.data.get("id", ""),
                           moment=moment, context=a.current)
        self.advisor_calls.append(call)
        self.mark("advisor", ev.ts, "advisor")
        self.say(ev.ts, "advisor", f"consulted by {a.name} · {moment}", "advisor")

    def _on_advisor_result(self, ev: Event) -> None:
        cid = ev.data.get("id", "")
        text = (ev.data.get("text") or "").strip()
        if not text:
            return
        target = None
        for call in reversed(self.advisor_calls):
            if (cid and call.call_id == cid) or (not cid and not call.advice):
                target = call
                break
        if target:
            target.advice = text
            first = text.splitlines()[0][:80]
            self.say(ev.ts, "advisor", f"advice · {first}", "advisor")

    def _infer_moment(self, a: Agent) -> str:
        done, total = self.task_counts()
        if a.repeat_errors >= 2 or a.last_error:
            return "after error"
        if total and total - done <= 1:
            return "before done"
        if a.calls_since_prompt <= 1:
            return "before plan"
        return "mid-work"
