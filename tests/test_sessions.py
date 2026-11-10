"""Hosted sessions, tested against a real tmux running a shell instead of Claude Code."""
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from agentwatch import cli
from agentwatch.app import AgentWatchApp
from agentwatch.model import Event, State
from agentwatch.mux import Mux, MuxError, slug, translate
from agentwatch.normalize import Resolver, hook_events
from agentwatch.sources import LiveCollector

EMIT = os.path.join(os.path.dirname(cli.__file__), "hooks", "emit.py")
needs_tmux = pytest.mark.skipif(not Mux.available(), reason="needs tmux")


@pytest.fixture
def mux():
    m = Mux(socket=f"aw-test-{uuid.uuid4().hex[:8]}", command="env PS1='$ ' bash --norc --noprofile")
    yield m
    subprocess.run(["tmux", "-L", m.socket, "kill-server"], capture_output=True, check=False)


def wait_for(check, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def screen_text(mux, name):
    return "\n".join(mux.capture(name).lines)


# ---- no tmux needed --------------------------------------------------------

def test_slug_and_key_translation():
    assert slug("My Proj.x") == "my-proj-x"
    assert slug("...") == "session"
    assert translate("a", "a") == ("text", ("a",))
    assert translate("space", " ") == ("keys", ("Space",))
    assert translate("enter", "\r") == ("keys", ("Enter",))
    assert translate("shift+tab", None) == ("keys", ("BTab",))
    assert translate("ctrl+c", "\x03") == ("keys", ("C-c",))
    assert translate("alt+p", None) == ("keys", ("M-p",))
    assert translate("ctrl+left", None) == ("keys", ("C-Left",))
    assert translate("f5", None) == ("keys", ("F5",))
    assert translate("shift+enter", None) == ("hex", ("1b", "0d"))
    assert translate("plus", "+") == ("text", ("+",))
    assert translate("é", "é") == ("text", ("é",))


def test_emit_reports_the_hosting_session(tmp_path):
    events = tmp_path / "events.jsonl"
    env = {**os.environ, "AGENTWATCH_EVENTS": str(events), "AGENTWATCH_SESSION": "api"}
    subprocess.run([sys.executable, EMIT], input=json.dumps({"hook_event_name": "Stop", "session_id": "s1"}),
                   text=True, env=env, timeout=10, check=True)
    assert json.loads(events.read_text())["_aw_session"] == "api"


def test_hosted_sessions_are_named_after_their_tab():
    r = Resolver()
    one = hook_events({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                       "_ts": 1, "_aw_session": "api"}, r)
    two = hook_events({"hook_event_name": "PreToolUse", "session_id": "s2", "tool_name": "Bash",
                       "_ts": 2, "_aw_session": "web"}, r)
    assert [(e.kind, e.agent) for e in one] == [("tool_start", "api"), ("term", "api")]
    assert [(e.kind, e.agent) for e in two] == [("tool_start", "web"), ("term", "web")]
    s = State()
    s.apply_all(one + two)
    assert (s.agents["api"].term, s.agents["web"].term) == ("api", "web")
    assert [a.name for a in s.jump_targets()] == ["api", "web"]
    named = hook_events({"hook_event_name": "Stop", "session_id": "s3", "teammate_name": "ux",
                         "_ts": 3, "_aw_session": "api"}, r)
    assert named[0].agent == "ux"                # an explicit teammate name still wins


def test_permission_notification_means_needs_you():
    r, s = Resolver(), State()
    base = {"session_id": "s1", "_aw_session": "api"}
    s.apply_all(hook_events({**base, "hook_event_name": "PreToolUse", "tool_name": "Bash", "_ts": 1}, r))
    s.apply_all(hook_events({**base, "hook_event_name": "Notification", "_ts": 2,
                             "notification_type": "permission_prompt",
                             "message": "Claude needs your permission to use Bash"}, r))
    assert s.agents["api"].blocked
    s.apply_all(hook_events({**base, "hook_event_name": "PostToolUse", "tool_name": "Bash", "_ts": 3}, r))
    assert not s.agents["api"].blocked           # answered: the tool ran
    s.apply_all(hook_events({**base, "hook_event_name": "Notification", "_ts": 4,
                             "notification_type": "idle_prompt",
                             "message": "Claude is waiting for your input"}, r))
    assert not s.agents["api"].blocked           # done and idle is not the same as blocked


def test_herdr_and_hook_blocked_do_not_clear_each_other():
    s = State()
    s.apply(Event(ts=1, kind="notification", agent="api", session="s1", data={"attention": True}))
    s.apply(Event(ts=2, kind="herdr", data={"connected": True, "agents": []}))
    assert s.agents["api"].blocked


# ---- tmux ---------------------------------------------------------------------

@needs_tmux
def test_mux_runs_types_resizes_and_scrolls(mux, tmp_path):
    assert mux.sessions() == []
    a = mux.new(str(tmp_path), name="My Proj.x")
    b = mux.new(str(tmp_path), name="my-proj-x")
    assert (a, b) == ("my-proj-x", "my-proj-x-2")
    assert [(s.name, s.cwd) for s in mux.sessions()] == [(a, str(tmp_path)), (b, str(tmp_path))]
    mux.send_text(a, "echo $AGENTWATCH_SESSION ${TMUX:-notmux}")
    mux.send_keys(a, "Enter")
    assert wait_for(lambda: "my-proj-x notmux" in screen_text(mux, a))
    assert "my-proj-x notmux" not in screen_text(mux, b)

    mux.resize(a, 50, 8)
    mux.send_text(a, "seq 1 30")
    mux.send_keys(a, "Enter")
    assert wait_for(lambda: mux.capture(a).history > 10)
    live = mux.capture(a)
    assert live.size == (50, 8) and len(live.lines) == 8 and live.cursor_visible
    back = mux.capture(a, scroll=5)
    assert not back.cursor_visible
    assert back.lines[-1].strip() == live.lines[-6].strip()

    mux.paste(a, "echo one; echo two")
    mux.send_keys(a, "Enter")
    assert wait_for(lambda: "two" in screen_text(mux, a))

    mux.send_keys(a, "C-d")                      # the program exits: the session goes away
    assert wait_for(lambda: [s.name for s in mux.sessions()] == [b])
    with pytest.raises(MuxError):
        mux.capture(a)
    mux.kill(b)
    assert mux.sessions() == []
    with pytest.raises(MuxError):
        mux.new(str(tmp_path / "missing"))


@needs_tmux
def test_app_hosts_sessions(mux, tmp_path):
    one = mux.new(str(tmp_path), name="one")
    two = mux.new(str(tmp_path), name="two")
    col = LiveCollector(str(tmp_path / "events.jsonl"), str(tmp_path / "claude"))
    app = AgentWatchApp(col, mode="live", mux=mux)

    async def run():
        async with app.run_test(size=(120, 36)) as pilot:
            term = app.terminal

            async def shown(text, name):
                for _ in range(100):
                    await pilot.pause(0.05)
                    if app.active == name and text in term.render().plain:
                        return True
                return False

            assert [s.name for s in app.sessions] == [one, two]
            assert app.active == two and app.focused is term
            await pilot.press(*"echo hi-$((40+2))", "enter")
            assert await shown("hi-42", two)
            assert term.size.width == mux.capture(two).size[0]     # the session was sized to the view

            await pilot.press("q")                                 # plain keys belong to the session
            assert app.is_running and await shown("$ q", two)
            await pilot.press("ctrl+c", "ctrl+q")                  # so do ctrl+c and ctrl+q
            await pilot.pause(0.2)
            assert app.is_running

            await pilot.press("ctrl+b", "1")
            assert app.active == one
            assert await shown("$", one) and "hi-42" not in term.render().plain
            await pilot.press("ctrl+b", "n")
            assert app.active == two
            await pilot.press("ctrl+b", "p")
            assert app.active == one

            await pilot.press("ctrl+b", "d")
            assert app.view == "dashboard" and app.focused is None
            assert not app.query_one("#workspace").display
            await pilot.press("ctrl+b", "d")
            assert app.view == "sessions" and app.focused is term

            await pilot.press("ctrl+b", "c")                       # new session dialog
            await pilot.pause(0.2)
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert len(app.sessions) == 3 and app.active == app.sessions[-1].name
            assert app.sessions[-1].cwd == str(tmp_path) and app.focused is term

            await pilot.press("ctrl+b", "x", "n")                  # anything but y keeps it
            await pilot.pause(0.2)
            assert len(mux.sessions()) == 3
            await pilot.press("ctrl+b", "x", "y")
            await pilot.pause(0.3)
            assert len(mux.sessions()) == 2 and app.active == two

            app.state.apply(Event(ts=time.time(), kind="term", agent="one", data={"name": one}))
            await pilot.press("ctrl+b", "d")
            await pilot.press("1")                                 # dashboard jump key: back to that session
            assert app.view == "sessions" and app.active == one

            await pilot.press("ctrl+b", "q")                       # detach
            await pilot.pause(0.2)
        assert not app.is_running

    asyncio.run(run())
    assert [s.name for s in mux.sessions()] == [one, two]          # sessions outlive the app
