"""herdr integration, tested against a stand-in server that speaks herdr's wire format."""
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from agentwatch import cli
from agentwatch.herdr import Herdr, HerdrError, HerdrWatcher, session_key, socket_path
from agentwatch.model import Event, State
from agentwatch.normalize import Resolver, hook_events
from agentwatch.sources import LiveCollector

EMIT = os.path.join(os.path.dirname(cli.__file__), "hooks", "emit.py")
pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")


class FakeHerdr:
    """Newline-delimited JSON over a Unix socket, one request per connection."""

    def __init__(self, path):
        self.path = str(path)
        self.requests = []
        self.agents = [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "terminal_id": "t1",
             "agent": "claude", "agent_status": "working", "focused": True, "revision": 1,
             "agent_session": {"source": "hook", "agent": "claude", "kind": "id", "value": "sess-lead"}},
            {"pane_id": "w1:p2", "workspace_id": "w1", "tab_id": "w1:t1", "terminal_id": "t2",
             "agent": "claude", "agent_status": "blocked", "focused": False, "revision": 1,
             "agent_session": {"source": "hook", "agent": "claude", "kind": "path",
                               "value": "/home/me/.claude/projects/x/sess-ux.jsonl"}}]
        self.errors = {}
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                req = json.loads(conn.makefile().readline())
                self.requests.append(req)
                method = req["method"]
                if method in self.errors:
                    reply = {"error": {"code": self.errors[method], "message": "nope"}}
                elif method == "agent.list":
                    reply = {"result": {"type": "agent_list", "agents": self.agents}}
                elif method == "workspace.list":
                    reply = {"result": {"type": "workspace_list",
                                        "workspaces": [{"workspace_id": "w1", "label": "billing"}]}}
                elif method == "ping":
                    reply = {"result": {"type": "pong", "version": "0.9.5", "protocol": 22}}
                else:
                    reply = {"result": {"type": "ok"}}
                conn.sendall((json.dumps({"id": req["id"], **reply}) + "\n").encode())

    def close(self):
        self.sock.close()


@pytest.fixture
def fake(tmp_path):
    server = FakeHerdr(tmp_path / "herdr.sock")
    yield server
    server.close()


def test_socket_path_follows_herdr_resolution_order():
    home = os.path.expanduser("~")
    assert socket_path({}) == os.path.join(home, ".config", "herdr", "herdr.sock")
    assert socket_path({"HERDR_SESSION": "work"}) == os.path.join(
        home, ".config", "herdr", "sessions", "work", "herdr.sock")
    assert socket_path({"HERDR_SESSION": "work", "HERDR_SOCKET_PATH": "/x/h.sock"}) == "/x/h.sock"


def test_session_key_accepts_ids_and_transcript_paths():
    assert session_key({"kind": "id", "value": "abc"}) == "abc"
    assert session_key({"kind": "path", "value": "/p/abc.jsonl"}) == "abc"
    assert session_key(None) == ""


def test_snapshot_and_errors(fake, tmp_path):
    client = Herdr(fake.path)
    assert client.available()
    snap = client.snapshot()
    assert snap["connected"] is True
    assert snap["agents"][1] == {"pane_id": "w1:p2", "workspace": "billing", "status": "blocked",
                                 "name": "", "kind": "claude", "session": "sess-ux"}
    fake.errors["agent.list"] = "not_found"
    with pytest.raises(HerdrError) as e:
        client.snapshot()
    assert e.value.code == "not_found"
    gone = Herdr(str(tmp_path / "missing.sock"))
    assert not gone.available()
    with pytest.raises(HerdrError) as e:
        gone.call("ping")
    assert e.value.code == "unreachable"
    assert HerdrWatcher(gone).poll_once() == {"connected": False, "agents": []}


def test_focus_falls_back_to_the_pane(fake):
    client = Herdr(fake.path)
    client.focus("w1:p2")
    assert fake.requests[-1]["method"] == "agent.focus"
    assert fake.requests[-1]["params"] == {"target": "w1:p2"}
    fake.errors["agent.focus"] = "agent_not_found"
    client.focus("w1:p2")
    assert fake.requests[-1]["method"] == "pane.focus"
    assert fake.requests[-1]["params"] == {"pane_id": "w1:p2"}


def test_open_dashboard_request(fake):
    Herdr(fake.path).open_dashboard("popup", env={})
    req = fake.requests[-1]
    assert req["method"] == "plugin.pane.open"
    assert req["params"] == {"plugin_id": "kodepapa.agentwatch", "entrypoint": "dashboard",
                             "placement": "popup", "focus": True, "width": "90%", "height": "90%",
                             "env": {"AGENTWATCH_HERDR_PLACEMENT": "popup"}}
    Herdr(fake.path).open_dashboard("split", env={"HERDR_PANE_ID": "w1:p1", "HERDR_PLUGIN_ID": "x.y"})
    params = fake.requests[-1]["params"]
    assert (params["plugin_id"], params["direction"], params["target_pane_id"]) == ("x.y", "right", "w1:p1")


def test_emit_records_the_herdr_pane(tmp_path):
    events = tmp_path / "events.jsonl"
    env = {**os.environ, "AGENTWATCH_EVENTS": str(events), "HERDR_ENV": "1",
           "HERDR_PANE_ID": "w2:p3", "HERDR_TAB_ID": "w2:t1", "HERDR_WORKSPACE_ID": "w2"}
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "s1"})
    subprocess.run([sys.executable, EMIT], input=payload, text=True, env=env, timeout=10, check=True)
    env.pop("HERDR_ENV")
    subprocess.run([sys.executable, EMIT], input=payload, text=True, env=env, timeout=10, check=True)
    inside, outside = (json.loads(line) for line in events.read_text().splitlines())
    assert inside["_herdr"] == {"pane_id": "w2:p3", "tab_id": "w2:t1", "workspace_id": "w2"}
    assert "_herdr" not in outside


def test_hook_pane_is_reported_once_per_agent():
    r = Resolver()
    p = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash", "_ts": 5,
         "_herdr": {"pane_id": "w1:p1"}}
    first = hook_events(p, r)
    assert [e.kind for e in first] == ["tool_start", "herdr_pane"]
    assert first[1].agent == "lead" and first[1].data == {"pane_id": "w1:p1"}
    assert [e.kind for e in hook_events(p, r)] == ["tool_start"]
    moved = hook_events({**p, "_herdr": {"pane_id": "w3:p9"}}, r)
    assert moved[-1].data == {"pane_id": "w3:p9"}


def test_state_maps_agents_to_panes_and_flags_blocked(fake):
    s = State()
    s.apply(Event(ts=1, kind="tool_start", agent="lead", session="sess-lead"))
    s.apply(Event(ts=2, kind="tool_start", agent="ux", session="sess-ux"))
    s.apply(Event(ts=3, kind="tool_start", agent="backend", session="sess-other"))
    s.apply(Event(ts=4, kind="herdr", data=Herdr(fake.path).snapshot()))
    lead, ux, backend = s.agents["lead"], s.agents["ux"], s.agents["backend"]
    assert (lead.pane, lead.blocked, lead.workspace) == ("w1:p1", False, "billing")
    assert (ux.pane, ux.blocked) == ("w1:p2", True)          # matched through the transcript path
    assert (backend.pane, backend.blocked) == ("", False)    # herdr does not know this session
    assert [a.name for a in s.jump_targets()] == ["lead", "ux"]
    assert sum("needs you" in entry.text for entry in s.log) == 1

    s.apply(Event(ts=5, kind="herdr", data=Herdr(fake.path).snapshot()))
    assert sum("needs you" in entry.text for entry in s.log) == 1   # no repeat while still blocked
    fake.agents[1]["agent_status"] = "working"
    s.apply(Event(ts=6, kind="herdr", data=Herdr(fake.path).snapshot()))
    assert not ux.blocked

    s.apply(Event(ts=7, kind="herdr_pane", agent="backend", data={"pane_id": "w1:p2"}))
    assert backend.pane == "w1:p2"                           # the hook's pane id wins over no match


def test_subagents_share_the_pane_but_are_not_jump_targets():
    s = State()
    s.apply(Event(ts=1, kind="tool_start", agent="lead", session="s1"))
    s.apply(Event(ts=2, kind="subagent_start", agent="lead", session="s1", data={"subagent": "explore"}))
    s.apply(Event(ts=3, kind="herdr_pane", agent="explore", data={"pane_id": "w1:p1"}))
    s.apply(Event(ts=4, kind="herdr", data={"connected": True, "agents": [
        {"pane_id": "w1:p1", "workspace": "", "status": "blocked", "name": "", "kind": "claude", "session": "s1"}]}))
    assert s.agents["lead"].blocked and not s.agents["explore"].blocked
    assert [a.name for a in s.jump_targets()] == ["lead"]


def test_collector_emits_herdr_snapshots_only_on_change(fake, tmp_path):
    watcher = HerdrWatcher(Herdr(fake.path))
    col = LiveCollector(str(tmp_path / "events.jsonl"), str(tmp_path / "claude"), herdr=watcher)
    watcher.poll_once()
    assert [e.kind for e in col.poll()] == ["herdr"]
    watcher.poll_once()
    assert col.poll() == []
    fake.agents[0]["agent_status"] = "blocked"
    watcher.poll_once()
    (ev,) = col.poll()
    assert ev.data["agents"][0]["status"] == "blocked"
    col.reset()
    assert [e.kind for e in col.poll()] == ["herdr"]         # a rebuilt state gets it again


def test_watcher_thread_stops(fake):
    watcher = HerdrWatcher(Herdr(fake.path), interval=0.05)
    watcher.start()
    deadline = time.time() + 5
    while watcher.take() is None and time.time() < deadline:
        time.sleep(0.02)
    watcher.stop()
    assert any(r["method"] == "agent.list" for r in fake.requests)
