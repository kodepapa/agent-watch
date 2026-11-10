"""Data sources. Each `poll()` returns new `Event`s since the last call."""
from __future__ import annotations

import glob
import json
import os
import time

from .model import Event
from .normalize import Resolver, TranscriptParser, hook_events, task_event, team_event


class JsonlTail:
    """Incrementally reads complete JSON lines from a growing file."""

    def __init__(self, path: str):
        self.path = path
        self.offset = 0
        self.buf = b""
        self.inode = None

    def read(self) -> list[dict]:
        try:
            st = os.stat(self.path)
        except OSError:
            return []
        if self.inode is not None and (st.st_ino != self.inode or st.st_size < self.offset):
            self.offset, self.buf = 0, b""          # rotated or truncated
        self.inode = st.st_ino
        if st.st_size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
            self.offset = f.tell()
        data = self.buf + chunk
        lines = data.split(b"\n")
        self.buf = lines.pop()                     # keep the partial last line
        out = []
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out


class LiveCollector:
    """Hook events file + team/task folders + transcripts -> Events."""

    def __init__(self, events_path: str, claude_dir: str, team: str = "",
                 since: float = 3600, record_path: str = ""):
        self.events_path = events_path
        self.claude_dir = claude_dir
        self.pinned_team = team
        self.since = since
        self.record_path = record_path
        self.reset()

    def reset(self, team: str | None = None) -> None:
        if team is not None:
            self.pinned_team = team
        self.resolver = Resolver()
        self.hooks = JsonlTail(self.events_path)
        self.transcripts: dict[str, tuple[JsonlTail, TranscriptParser]] = {}
        self.file_mtimes: dict[str, float] = {}
        self.task_snapshots: dict[str, str] = {}
        self.known_members: set = set()
        self.team = ""
        self.started = time.time()
        self.first_poll = True
        self.raw_samples: dict[str, dict] = {}

    # ---- teams ---------------------------------------------------------
    def teams(self) -> list[str]:
        paths = glob.glob(os.path.join(self.claude_dir, "teams", "*", "config.json"))
        paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return [os.path.basename(os.path.dirname(p)) for p in paths]

    def _active_team(self) -> str:
        if self.pinned_team:
            return self.pinned_team
        teams = self.teams()
        return teams[0] if teams else ""

    def _changed(self, path: str) -> float | None:
        try:
            m = os.path.getmtime(path)
        except OSError:
            return None
        if self.file_mtimes.get(path) == m:
            return None
        self.file_mtimes[path] = m
        return m

    def _poll_team(self) -> list[Event]:
        out: list[Event] = []
        team = self._active_team()
        self.team = team
        task_dirs = []
        if team:
            cfg_path = os.path.join(self.claude_dir, "teams", team, "config.json")
            m = self._changed(cfg_path)
            if m is not None:
                try:
                    with open(cfg_path) as f:
                        cfg = json.load(f)
                    out.append(team_event(team, cfg, m, self.resolver, self.known_members))
                except (OSError, ValueError):
                    pass
            task_dirs.append(os.path.join(self.claude_dir, "tasks", team))
        if self.resolver.lead_session:
            task_dirs.append(os.path.join(self.claude_dir, "tasks", self.resolver.lead_session))
        seen = set()
        for d in task_dirs:
            for path in glob.glob(os.path.join(d, "*.json")):
                seen.add(path)
                m = self._changed(path)
                if m is None:
                    continue
                try:
                    with open(path) as f:
                        raw = f.read()
                    task = json.loads(raw)
                except (OSError, ValueError):
                    continue
                if not isinstance(task, dict) or self.task_snapshots.get(path) == raw:
                    continue
                self.task_snapshots[path] = raw
                task.setdefault("id", os.path.splitext(os.path.basename(path))[0])
                out.append(task_event(task, m, live=not self.first_poll))
        for path in list(self.task_snapshots):
            if path not in seen and any(path.startswith(d) for d in task_dirs):
                tid = os.path.splitext(os.path.basename(path))[0]
                self.task_snapshots.pop(path)
                out.append(Event(ts=time.time(), kind="task_removed", data={"id": tid}))
        return out

    # ---- hooks + transcripts --------------------------------------------
    def _poll_hooks(self) -> list[Event]:
        out: list[Event] = []
        cutoff = time.time() - self.since if self.first_poll else 0
        for p in self.hooks.read():
            if cutoff and float(p.get("_ts") or 0) < cutoff:
                continue
            hname = p.get("hook_event_name", "?")
            self.raw_samples[hname] = p
            evs = hook_events(p, self.resolver)
            out.extend(evs)
            tpath = p.get("transcript_path")
            if tpath and tpath not in self.transcripts and evs:
                self.transcripts[tpath] = (JsonlTail(tpath), TranscriptParser(evs[0].agent, evs[0].session))
            spath = p.get("agent_transcript_path")
            if spath and spath not in self.transcripts and evs and evs[0].data.get("subagent"):
                self.transcripts[spath] = (JsonlTail(spath),
                                           TranscriptParser(evs[0].data["subagent"], evs[0].session))
        return out

    def _poll_transcripts(self) -> list[Event]:
        out: list[Event] = []
        cutoff = time.time() - self.since if self.first_poll else 0
        for tail, parser in self.transcripts.values():
            for line in tail.read():
                for ev in parser.parse(line):
                    if cutoff and ev.ts and ev.ts < cutoff:
                        continue
                    out.append(ev)
        return out

    def poll(self) -> list[Event]:
        out = self._poll_team()          # team first so names resolve
        out += self._poll_hooks()
        out += self._poll_transcripts()
        self.first_poll = False
        out.sort(key=lambda e: e.ts)
        if self.record_path and out:
            with open(self.record_path, "a") as f:
                for ev in out:
                    f.write(json.dumps(ev.to_dict()) + "\n")
        return out


class ReplayCollector:
    """Plays back a file recorded with `agentwatch watch --record`."""

    def __init__(self, path: str, speed: float = 1.0):
        with open(path) as f:
            self.events = [Event.from_dict(json.loads(l)) for l in f if l.strip()]
        self.events.sort(key=lambda e: e.ts)
        self.speed = speed
        self.idx = 0
        self.paused = False
        self.team = ""
        self.start_virtual = self.events[0].ts if self.events else time.time()
        self.virtual = self.start_virtual
        self.last_wall = time.time()

    def clock(self) -> float:
        return self.virtual

    def _advance(self) -> None:
        now = time.time()
        if not self.paused:
            self.virtual += (now - self.last_wall) * self.speed
        self.last_wall = now

    def teams(self) -> list[str]:
        return []

    def reset(self, team: str | None = None) -> None:
        pass

    @property
    def done(self) -> bool:
        return self.idx >= len(self.events)

    def poll(self) -> list[Event]:
        self._advance()
        out = []
        while self.idx < len(self.events) and self.events[self.idx].ts <= self.virtual:
            ev = self.events[self.idx]
            if ev.kind == "team":
                self.team = ev.data.get("team", self.team)
            out.append(ev)
            self.idx += 1
        return out
