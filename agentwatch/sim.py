"""Demo simulator.

Writes a scripted agent-team session to disk in the same shapes the live
monitor reads (hook payloads, team config, task files, transcripts), so the
demo exercises the real data path end to end. Nothing here talks to Claude.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid

CWD = "/home/demo/billing-app"
TEAM = "billing-revamp"

AGENTS = {
    "lead": {"model": "claude-sonnet-5-5", "ctx": 16000},
    "ux": {"model": "claude-sonnet-5-5", "ctx": 9000, "color": "blue"},
    "backend": {"model": "claude-sonnet-5-5", "ctx": 9000, "color": "teal"},
    "advocate": {"model": "claude-haiku-5-5", "ctx": 7000, "color": "purple"},
}

TASKS = [
    ("1", "api: billing endpoints", []),
    ("2", "api: invoice model + migration", []),
    ("3", "ui: billing page", []),
    ("4", "ui: plan picker", []),
    ("5", "break: billing flows", ["1", "3"]),
    ("6", "docs: changelog", []),
    ("7", "tests: billing e2e", ["1", "2", "3", "4"]),
    ("8", "review + merge", ["1", "2", "3", "4", "5", "6", "7"]),
]

ADVICE = {
    "plan": "Split the work by directory so every file has one owner: ui/ to ux, "
            "api/ to backend. Give one teammate the job of breaking it.",
    "blocker": "Fix proration in one place (api/billing/proration.ts) and have "
               "advocate re-run the downgrade case before anyone builds on it.",
    "error": "Stop retrying the migration. 0041 already creates invoices; drop the "
             "duplicate CREATE in 0042_invoices.sql.",
    "done": "Add one test for the downgrade proration case, then merge. Nothing "
            "else blocks shipping.",
}

POOLS = {
    "ux": [("Read", {"file_path": f"{CWD}/src/ui/Settings.tsx"}),
           ("Edit", {"file_path": f"{CWD}/src/ui/billing/BillingPage.tsx"}),
           ("Edit", {"file_path": f"{CWD}/src/ui/billing/PlanPicker.tsx"}),
           ("Write", {"file_path": f"{CWD}/src/ui/billing/InvoiceList.tsx"}),
           ("Bash", {"command": "npm run typecheck"}),
           ("Bash", {"command": "npm test -- ui/billing"}),
           ("Grep", {"pattern": "usePlan"})],
    "backend": [("Read", {"file_path": f"{CWD}/src/api/routes.ts"}),
                ("Edit", {"file_path": f"{CWD}/src/api/billing/invoices.ts"}),
                ("Edit", {"file_path": f"{CWD}/src/api/billing/proration.ts"}),
                ("Write", {"file_path": f"{CWD}/migrations/0042_invoices.sql"}),
                ("Bash", {"command": "npm test -- api/billing"}),
                ("Grep", {"pattern": "stripe.invoices"})],
    "advocate": [("Read", {"file_path": f"{CWD}/src/api/billing/proration.ts"}),
                 ("Read", {"file_path": f"{CWD}/src/ui/billing/PlanPicker.tsx"}),
                 ("Bash", {"command": "npm test -- e2e/billing"}),
                 ("Write", {"file_path": f"{CWD}/tests/e2e/billing.spec.ts"}),
                 ("Grep", {"pattern": "downgrade"})],
    "lead": [("TaskList", {}), ("Read", {"file_path": f"{CWD}/src/api/billing/invoices.ts"}),
             ("Bash", {"command": "git diff --stat"}), ("Read", {"file_path": f"{CWD}/README.md"})],
}


class DemoSession:
    def __init__(self, claude_dir: str, events_path: str, seed: int = 7):
        self.claude_dir = claude_dir
        self.events_path = events_path
        self.rng = random.Random(seed)
        self.sessions = {name: str(uuid.UUID(int=self.rng.getrandbits(128))) for name in AGENTS}
        self.ctx = {name: spec["ctx"] for name, spec in AGENTS.items()}
        self.tasks = {tid: {"id": tid, "subject": subj, "description": "", "status": "pending",
                            "owner": "", "blocks": [], "blockedBy": deps}
                      for tid, subj, deps in TASKS}
        self.actions: list[tuple[float, callable]] = []
        self.project = os.path.join(claude_dir, "projects", "-home-demo-billing-app")
        self._script()
        self.actions.sort(key=lambda a: a[0])
        self.duration = self.actions[-1][0]

    # ---- low-level writers ----------------------------------------------
    def _transcript(self, agent: str) -> str:
        return os.path.join(self.project, self.sessions[agent] + ".jsonl")

    def _append(self, path: str, obj: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(obj) + "\n")

    def _hook(self, ts: float, agent: str, event: str, **fields) -> None:
        p = {"session_id": self.sessions[agent], "transcript_path": self._transcript(agent),
             "cwd": CWD, "hook_event_name": event, **fields, "_ts": ts}
        if agent != "lead":
            p.setdefault("teammate_name", agent)
            p.setdefault("team_name", TEAM)
        self._append(self.events_path, p)

    def _assistant(self, ts: float, agent: str, content: list, grow: int = 1800) -> None:
        self.ctx[agent] += grow
        ctx = self.ctx[agent]
        msg = {"id": "msg_" + uuid.UUID(int=self.rng.getrandbits(128)).hex[:20],
               "model": AGENTS[agent]["model"], "role": "assistant", "content": content,
               "usage": {"input_tokens": 240, "cache_creation_input_tokens": 600,
                         "cache_read_input_tokens": ctx - 840, "output_tokens": 310}}
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z"
        self._append(self._transcript(agent), {"type": "assistant", "timestamp": iso,
                                               "sessionId": self.sessions[agent], "message": msg})

    def _write_task(self, ts: float, tid: str, **changes) -> None:
        self.tasks[tid].update(changes)
        d = os.path.join(self.claude_dir, "tasks", TEAM)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{tid}.json")
        with open(path, "w") as f:
            json.dump(self.tasks[tid], f, indent=2)
        os.utime(path, (ts, ts))

    def _write_team(self, ts: float) -> None:
        d = os.path.join(self.claude_dir, "teams", TEAM)
        os.makedirs(d, exist_ok=True)
        members = [{"agentId": f"team-lead@{TEAM}", "name": "team-lead", "agentType": "team-lead",
                    "model": AGENTS["lead"]["model"]}]
        for name in ("ux", "backend", "advocate"):
            members.append({"agentId": f"{name}@{TEAM}", "name": name, "agentType": "general-purpose",
                            "model": AGENTS[name]["model"], "color": AGENTS[name]["color"]})
        path = os.path.join(d, "config.json")
        with open(path, "w") as f:
            json.dump({"name": TEAM, "leadAgentId": f"team-lead@{TEAM}", "members": members}, f, indent=2)
        os.utime(path, (ts, ts))

    # ---- script helpers ----------------------------------------------------
    def at(self, t: float, fn) -> None:
        self.actions.append((t, fn))

    def tool(self, t: float, agent: str, name: str, inp: dict, error: str = "",
             dur: float = 1.6, **extra) -> None:
        tid = "toolu_" + uuid.UUID(int=self.rng.getrandbits(128)).hex[:16]
        self.at(t - 0.2, lambda ts: self._assistant(ts, agent, [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]))
        self.at(t, lambda ts: self._hook(ts, agent, "PreToolUse", tool_name=name,
                                         tool_input=inp, tool_use_id=tid, **extra))
        if error:
            self.at(t + dur, lambda ts: self._hook(ts, agent, "PostToolUseFailure", tool_name=name,
                                                   tool_input=inp, tool_use_id=tid, error=error,
                                                   is_interrupt=False, **extra))
        else:
            self.at(t + dur, lambda ts: self._hook(ts, agent, "PostToolUse", tool_name=name,
                                                   tool_input=inp, tool_use_id=tid,
                                                   tool_response={"ok": True}, **extra))

    def work(self, agent: str, t0: float, t1: float, gap=(5, 11)) -> None:
        t = t0 + self.rng.uniform(0.5, 3)
        while t < t1:
            name, inp = self.rng.choice(POOLS[agent])
            self.tool(t, agent, name, inp, dur=self.rng.uniform(0.8, 3.5))
            t += self.rng.uniform(*gap)

    def advisor(self, t: float, key: str, delay: float = 4.5) -> None:
        bid = "srvtoolu_" + uuid.UUID(int=self.rng.getrandbits(128)).hex[:16]
        self.at(t, lambda ts: self._assistant(ts, "lead", [
            {"type": "server_tool_use", "id": bid, "name": "advisor", "input": {}}], grow=400))
        self.at(t + delay, lambda ts: self._assistant(ts, "lead", [
            {"type": "advisor_tool_result", "tool_use_id": bid,
             "content": [{"type": "text", "text": ADVICE[key]}]}], grow=900))

    def task(self, t: float, tid: str, **changes) -> None:
        self.at(t, lambda ts: self._write_task(ts, tid, **changes))

    def task_done(self, t: float, agent: str, tid: str) -> None:
        self.tool(t - 1, agent, "TaskUpdate", {"taskId": tid, "status": "completed"}, dur=0.4)
        self.task(t, tid, status="completed")
        self.at(t + 0.1, lambda ts: self._hook(ts, agent, "TaskCompleted", task_id=tid,
                                               task_subject=self.tasks[tid]["subject"]))

    def claim(self, t: float, agent: str, tid: str, status: str = "in_progress") -> None:
        self.tool(t - 0.6, agent, "TaskUpdate", {"taskId": tid, "owner": agent}, dur=0.3)
        self.task(t, tid, owner=agent, status=status)

    # ---- the session -------------------------------------------------------
    def _script(self) -> None:
        self.at(0, lambda ts: self._hook(ts, "lead", "SessionStart", source="startup"))
        self.at(2, lambda ts: self._hook(ts, "lead", "UserPromptSubmit",
                                         prompt="Build the billing page: UI, API and docs. Use a team."))
        self.advisor(4, "plan")
        self.tool(10, "lead", "Read", {"file_path": f"{CWD}/package.json"})
        self.tool(13, "lead", "Glob", {"pattern": "src/**/*.ts"})

        sub = {"agent_id": "agt_explore01", "agent_type": "Explore"}
        self.at(16, lambda ts: self._hook(ts, "lead", "SubagentStart", **sub))
        for t, name, inp in ((18, "Grep", {"pattern": "stripe"}),
                             (21, "Read", {"file_path": f"{CWD}/src/api/routes.ts"}),
                             (25, "Read", {"file_path": f"{CWD}/src/ui/Settings.tsx"}),
                             (28, "Grep", {"pattern": "invoice"})):
            self.tool(t, "lead", name, inp, **sub)
        self.at(33, lambda ts: self._hook(ts, "lead", "SubagentStop", **sub,
                                          agent_transcript_path=""))

        self.at(38, self._write_team)
        for i, (tid, subj, _) in enumerate(TASKS):
            t = 40 + i
            self.tool(t - 0.5, "lead", "TaskCreate", {"subject": subj}, dur=0.3)
            self.task(t, tid)
            self.at(t + 0.1, lambda ts, tid=tid, subj=subj: self._hook(
                ts, "lead", "TaskCreated", task_id=tid, task_subject=subj))
        for t, name in ((50, "ux"), (51, "backend"), (53, "advocate")):
            self.at(t, lambda ts, name=name: self._hook(ts, name, "SessionStart", source="startup"))

        # backend: endpoints, then invoices with a repeated test failure
        self.claim(56, "backend", "1")
        self.work("backend", 58, 168)
        self.task_done(172, "backend", "1")
        self.claim(175, "backend", "2")
        self.work("backend", 177, 196)
        err = "TypeError: unknown key 'currency' in InvoiceLine"
        self.tool(200, "backend", "Bash", {"command": "npm test -- api/billing"}, error=err, dur=3)
        self.tool(208, "backend", "Edit", {"file_path": f"{CWD}/src/api/billing/invoices.ts"})
        self.tool(214, "backend", "Bash", {"command": "npm test -- api/billing"}, error=err, dur=3)
        self.tool(222, "backend", "Edit", {"file_path": f"{CWD}/src/api/billing/types.ts"})
        self.tool(228, "backend", "Bash", {"command": "npm test -- api/billing"}, dur=3)
        self.task_done(236, "backend", "2")
        self.at(240, lambda ts: self._hook(ts, "backend", "TeammateIdle"))

        # ux: page, then plan picker
        self.claim(57, "ux", "3")
        self.work("ux", 59, 198)
        self.task_done(202, "ux", "3")
        self.claim(205, "ux", "4")
        self.work("ux", 207, 286)
        self.task_done(290, "ux", "4")
        self.at(295, lambda ts: self._hook(ts, "ux", "TeammateIdle"))

        # advocate: waits on 1 and 3, finds a blocker, then writes e2e tests
        self.claim(59, "advocate", "5", status="pending")
        self.work("advocate", 62, 200, gap=(12, 22))
        self.task(205, "5", status="in_progress")
        self.work("advocate", 207, 258)
        self.tool(262, "advocate", "SendMessage",
                  {"to": "lead", "message": "found 1 blocker: proration rounds down on downgrade"})
        self.advisor(266, "blocker")
        self.tool(272, "lead", "Edit", {"file_path": f"{CWD}/src/api/billing/proration.ts"})
        self.tool(280, "lead", "SendMessage", {"to": "advocate", "message": "fixed, please re-run"})
        self.tool(286, "advocate", "Bash", {"command": "npm test -- e2e/billing --grep downgrade"}, dur=4)
        self.task_done(300, "advocate", "5")
        self.claim(303, "advocate", "7")
        self.work("advocate", 305, 376, gap=(5, 9))
        self.task_done(380, "advocate", "7")
        self.at(385, lambda ts: self._hook(ts, "advocate", "TeammateIdle"))

        # lead: coordinates, writes docs, merges
        self.work("lead", 60, 118, gap=(9, 16))
        self.claim(120, "lead", "6")
        self.tool(126, "lead", "Read", {"file_path": f"{CWD}/CHANGELOG.md"})
        self.tool(131, "lead", "Edit", {"file_path": f"{CWD}/CHANGELOG.md"})
        self.task_done(150, "lead", "6")
        self.work("lead", 152, 258, gap=(10, 18))
        self.work("lead", 292, 386, gap=(10, 18))
        self.claim(390, "lead", "8")
        self.at(391, lambda ts: self._hook(ts, "lead", "Notification",
                                           message="Claude needs your permission to use Bash"))
        self.tool(394, "lead", "Bash", {"command": "git merge ux/billing-page"})
        self.tool(398, "lead", "Bash", {"command": "git merge backend/billing-api"})
        merr = 'error: relation "invoices" already exists'
        self.tool(403, "lead", "Bash", {"command": "npm run migrate"}, error=merr, dur=2.5)
        self.tool(410, "lead", "Bash", {"command": "npm run migrate"}, error=merr, dur=2.5)
        self.advisor(414, "error")
        self.tool(421, "lead", "Edit", {"file_path": f"{CWD}/migrations/0042_invoices.sql"})
        self.tool(426, "lead", "Bash", {"command": "npm run migrate"}, dur=2)
        self.tool(431, "lead", "Bash", {"command": "npm test"}, dur=5)
        self.advisor(441, "done")
        self.tool(450, "lead", "Write", {"file_path": f"{CWD}/tests/billing/proration.test.ts"})
        self.tool(456, "lead", "Bash", {"command": "npm test -- proration"}, dur=3)
        self.task_done(466, "lead", "8")
        self.at(469, lambda ts: self._hook(ts, "lead", "Stop"))

    # ---- running -------------------------------------------------------------
    def generate(self, until: float | None = None, end_at: float | None = None) -> None:
        """Write everything up to `until` seconds instantly, ending at `end_at`."""
        until = self.duration if until is None else until
        start = (end_at or time.time()) - until
        for t, fn in self.actions:
            if t > until:
                break
            fn(start + t)

    def run_live(self, speed: float = 2.0, stop: threading.Event | None = None) -> threading.Thread:
        def loop():
            start = time.time()
            for t, fn in self.actions:
                due = start + t / speed
                while time.time() < due:
                    if stop is not None and stop.is_set():
                        return
                    time.sleep(min(0.05, max(0.0, due - time.time())))
                fn(time.time())
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread
