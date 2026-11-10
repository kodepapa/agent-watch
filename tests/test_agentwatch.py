import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from agentwatch import cli
from agentwatch.app import AgentWatchApp
from agentwatch.model import Event, State
from agentwatch.normalize import Resolver, TranscriptParser, hook_events, pretty_model
from agentwatch.sim import DemoSession
from agentwatch.sources import LiveCollector, ReplayCollector

EMIT = os.path.join(os.path.dirname(cli.__file__), "hooks", "emit.py")


def run_emit(tmp_path, payload) -> dict:
    events = tmp_path / "events.jsonl"
    env = {**os.environ, "AGENTWATCH_EVENTS": str(events), "CLAUDE_CODE_TEAMMATE_NAME": "ux"}
    proc = subprocess.run([sys.executable, EMIT], input=json.dumps(payload), text=True,
                          env=env, capture_output=True, timeout=10)
    assert proc.returncode == 0
    return json.loads(events.read_text().splitlines()[-1])


def test_emit_trims_and_stamps(tmp_path):
    out = run_emit(tmp_path, {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                              "tool_response": {"stdout": "x" * 5000, "exit_code": 1}})
    assert out["_ts"] > 0
    assert out["_is_error"] is True
    assert len(out["tool_response"]["stdout"]) < 500
    assert out["_env"]["CLAUDE_CODE_TEAMMATE_NAME"] == "ux"


def test_emit_survives_garbage(tmp_path):
    events = tmp_path / "events.jsonl"
    proc = subprocess.run([sys.executable, EMIT], input="not json", text=True,
                          env={**os.environ, "AGENTWATCH_EVENTS": str(events)}, timeout=10)
    assert proc.returncode == 0
    assert "_unparsed" in json.loads(events.read_text())


def test_pretty_model():
    assert pretty_model("claude-opus-5-5") == "opus 5.5"
    assert pretty_model("claude-sonnet-5-5-20261001") == "sonnet 5.5"
    assert pretty_model("claude-haiku-4-5") == "haiku 4.5"
    assert pretty_model("") == ""


def test_resolver_names_lead_teammates_and_subagents():
    r = Resolver()
    lead = hook_events({"hook_event_name": "SessionStart", "session_id": "s1", "_ts": 1}, r)[0]
    mate = hook_events({"hook_event_name": "PreToolUse", "session_id": "s2", "teammate_name": "ux",
                        "tool_name": "Read", "tool_input": {"file_path": "/a/b/c.ts"}, "_ts": 2}, r)[0]
    start = hook_events({"hook_event_name": "SubagentStart", "session_id": "s1",
                         "agent_id": "a1", "agent_type": "Explore", "_ts": 3}, r)[0]
    inner = hook_events({"hook_event_name": "PreToolUse", "session_id": "s1", "agent_id": "a1",
                         "tool_name": "Grep", "tool_input": {"pattern": "x"}, "_ts": 4}, r)[0]
    assert lead.agent == "lead"
    assert mate.agent == "ux" and mate.detail == "Read b/c.ts"
    assert start.data["subagent"] == "explore"
    assert inner.agent == "explore"


def test_same_error_twice_survives_a_fix_attempt():
    s = State()
    fail = dict(kind="tool_end", agent="backend", detail="Bash npm test",
                data={"tool": "Bash", "error": True, "error_sig": "Bash:TypeError"})
    s.apply(Event(ts=1, **fail))
    s.apply(Event(ts=2, kind="tool_end", agent="backend", detail="Edit x.ts", data={"error": False}))
    s.apply(Event(ts=3, **fail))
    assert s.agents["backend"].repeat_errors == 2
    assert any("same error x2" in entry.text for entry in s.log)
    s.apply(Event(ts=4, kind="tool_end", agent="backend", detail="Bash npm test", data={"error": False}))
    assert s.agents["backend"].repeat_errors == 0


def test_transcript_parser_finds_advisor_and_dedupes_usage():
    p = TranscriptParser("lead", "s1")
    msg = {"id": "m1", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 10, "cache_read_input_tokens": 90},
           "content": [{"type": "server_tool_use", "id": "srv1", "name": "advisor", "input": {}}]}
    line = {"type": "assistant", "timestamp": "2026-10-29T10:00:00.000Z", "message": msg}
    first = p.parse(line)
    again = p.parse(line)
    assert [e.kind for e in first] == ["model", "advisor_call"]
    assert again == []
    res = p.parse({"type": "assistant", "timestamp": "2026-10-29T10:00:04.000Z", "message": {
        "id": "m2", "content": [{"type": "advisor_tool_result", "tool_use_id": "srv1",
                                 "content": [{"type": "text", "text": "do X"}]}]}})
    assert res[-1].kind == "advisor_result" and res[-1].data["text"] == "do X"


@pytest.fixture
def demo_dirs(tmp_path):
    claude = tmp_path / "claude"
    events = tmp_path / "events.jsonl"
    return str(claude), str(events)


def test_full_demo_session_state(demo_dirs):
    claude, events = demo_dirs
    DemoSession(claude, events).generate()
    s = State()
    s.apply_all(LiveCollector(events, claude, since=86400).poll())
    assert s.team == "billing-revamp"
    assert s.task_counts() == (8, 8)
    assert {"lead", "ux", "backend", "advocate", "explore"} <= set(s.agents)
    assert s.agents["advocate"].model == "haiku 5.5"
    assert [c.moment for c in s.advisor_calls] == ["before plan", "mid-work", "after error", "before done"]
    assert all(c.advice for c in s.advisor_calls)
    assert s.agents["lead"].tokens_read > 0
    assert s.errors_total == 4


def test_live_updates_and_record_replay(demo_dirs, tmp_path):
    claude, events = demo_dirs
    sim = DemoSession(claude, events)
    sim.generate(until=100, end_at=time.time() - 400)
    record = str(tmp_path / "rec.jsonl")
    col = LiveCollector(events, claude, since=86400, record_path=record)
    s = State()
    s.apply_all(col.poll())
    before = s.task_counts()
    # now write the rest of the session as if it happened live
    start = time.time() - 400
    for t, fn in sim.actions:
        if t > 100:
            fn(start + t)
    s.apply_all(col.poll())
    assert before[0] < s.task_counts()[0] == 8
    assert any(entry.text.startswith("claimed") for entry in s.log)
    rc = ReplayCollector(record, speed=1000)
    r = State(clock=rc.clock)
    for _ in range(200):
        r.apply_all(rc.poll())
        if rc.done:
            break
        time.sleep(0.005)
    assert rc.done and r.task_counts() == (8, 8)


def test_install_and_uninstall_hooks(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "sonnet", "hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "say done"}]}]}}))
    assert cli.main(["install-hooks", "--settings", str(settings)]) == 0
    data = json.loads(settings.read_text())
    assert data["model"] == "sonnet"
    assert len(data["hooks"]["Stop"]) == 2
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "*"
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["async"] is True
    assert cli.main(["install-hooks", "--settings", str(settings)]) == 0
    assert len(json.loads(settings.read_text())["hooks"]["Stop"]) == 2      # idempotent
    assert cli.main(["uninstall-hooks", "--settings", str(settings)]) == 0
    data = json.loads(settings.read_text())
    assert data["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}


def test_app_runs_live_demo_without_errors(demo_dirs):
    claude, events = demo_dirs
    sim = DemoSession(claude, events)
    sim.run_live(speed=60)
    app = AgentWatchApp(LiveCollector(events, claude, since=86400), mode="demo", tick=0.05)

    async def go():
        async with app.run_test(size=(120, 40)) as pilot:
            for key in ("w", "w", "p", "p", "t"):
                await pilot.press(key)
            deadline = time.time() + 30
            while time.time() < deadline and app.state.task_counts() != (8, 8):
                await pilot.pause(0.5)
    asyncio.run(go())
    assert app.state.task_counts() == (8, 8)


def test_app_renders_at_tiny_size(demo_dirs):
    claude, events = demo_dirs
    DemoSession(claude, events).generate()
    app = AgentWatchApp(LiveCollector(events, claude, since=86400), mode="demo")

    async def go():
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause(0.5)
    asyncio.run(go())


# ---- timeline ---------------------------------------------------------------

def kinds(cells):
    """One letter per cell: the same priority the panel draws with."""
    order = (("error", "E"), ("prompt", "P"), ("wait", "?"), ("tool", "T"), ("work", "w"),
             ("stop", "_"), ("idle", "_"))
    return "".join(next((letter for key, letter in order if c.get(key)), ".") for c in cells)


def test_timeline_fills_long_work_and_stops_at_stop():
    from agentwatch.panels import activity
    marks = [(100, "prompt"), (110, "tool"), (300, "tool"), (310, "stop"), (400, "prompt")]
    # 10s cells covering 100..500; the agent is working right now
    assert kinds(activity(marks, 10, 10, 40, live="work")) == "PTwwwwwwwwwwwwwwwwwwT_........Pwwwwwwwww"
    # same marks, agent no longer working: nothing is painted after the last mark
    assert kinds(activity(marks, 10, 10, 40))[-10:] == "P" + "." * 9


def test_timeline_cells_do_not_move_within_a_bucket():
    from agentwatch.panels import activity
    marks = [(1000.2, "tool"), (1003.9, "tool"), (1004.1, "error"), (1031, "stop")]
    per, track = 4.0, 12

    def at(now):
        return kinds(activity(marks, int(now // per) - (track - 1), per, track))

    assert at(1040.0) == at(1041.5) == at(1043.9)       # same bucket: an identical picture
    assert at(1044.0) == at(1040.0)[1:] + "."           # next bucket: everything moves one cell
    assert at(1040.0).count("T") == 1 and "E" in at(1040.0)


def test_timeline_handles_marks_that_arrive_out_of_order_and_from_before_the_window():
    from agentwatch.panels import activity
    marks = [(250, "tool"), (40, "prompt"), (260, "stop")]     # the prompt predates the window
    assert kinds(activity(marks, 20, 10, 10)) == "wwwwwT_..."


def test_timeline_shows_waiting_for_the_user():
    s = State()
    s.apply(Event(ts=100, kind="tool_start", agent="api", detail="Bash rm -rf build"))
    s.apply(Event(ts=105, kind="notification", agent="api", data={"attention": True}))
    assert s.agents["api"].blocked and s.agents["api"].since == 105
    from agentwatch.panels import activity, live_mode
    assert live_mode(s.agents["api"]) == "wait"
    assert kinds(activity(s.marks["api"], 10, 10, 6, "wait")) == "?" * 6
    s.apply(Event(ts=140, kind="tool_end", agent="api", data={"error": False}))
    assert not s.agents["api"].blocked and live_mode(s.agents["api"]) == "work"
    assert kinds(activity(s.marks["api"], 10, 10, 6, "work")) == "????ww"


def test_interrupt_and_idle_prompt_end_a_stale_working_state():
    r, s = Resolver(), State()
    base = {"session_id": "s1", "_ts": 1}
    s.apply_all(hook_events({**base, "hook_event_name": "PreToolUse", "tool_name": "Bash"}, r))
    s.apply_all(hook_events({**base, "_ts": 2, "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                             "is_interrupt": True}, r))
    lead = s.agents["lead"]
    assert (lead.state, lead.current, lead.errors) == ("idle", "interrupted", 0)
    assert s.marks["lead"][-1] == (2, "stop")
    s.apply_all(hook_events({**base, "_ts": 3, "hook_event_name": "UserPromptSubmit", "prompt": "go on"}, r))
    assert lead.state == "working"
    s.apply_all(hook_events({**base, "_ts": 70, "hook_event_name": "Notification",
                             "message": "Claude is waiting for your input"}, r))
    assert (lead.state, lead.since) == ("idle", 70)


def test_subagent_and_session_end_leave_a_stop_on_the_timeline():
    s = State()
    s.apply(Event(ts=1, kind="subagent_start", agent="lead", data={"subagent": "explore"}))
    s.apply(Event(ts=9, kind="subagent_stop", agent="lead", data={"subagent": "explore"}))
    s.apply(Event(ts=12, kind="session_end", agent="lead"))
    assert s.marks["explore"][-1] == (9, "stop") and s.marks["lead"][-1] == (12, "stop")
