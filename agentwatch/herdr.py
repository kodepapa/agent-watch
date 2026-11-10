"""Client for herdr's local socket API (https://herdr.dev).

herdr is a terminal multiplexer for coding agents. When agentwatch runs next
to it, herdr tells us which pane each Claude Code session lives in and whether
an agent is blocked on an approval or question, and lets us focus that pane.

The protocol is newline-delimited JSON over a Unix socket: one request per
line, one response per line. Field names here follow herdr's published schema
(docs/next/api/herdr-api.schema.json in the herdr repo), not guesses.
"""
from __future__ import annotations

import json
import os
import socket
import threading

PLUGIN_ID = "kodepapa.agentwatch"
PLACEMENTS = ("tab", "split", "popup", "overlay", "zoomed")
# Placements herdr closes for us when the program exits: leave after a jump.
TRANSIENT = ("popup", "overlay")


class HerdrError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def socket_path(env=None) -> str:
    """Same resolution order as the herdr CLI, minus its --session flag."""
    env = os.environ if env is None else env
    if env.get("HERDR_SOCKET_PATH"):
        return env["HERDR_SOCKET_PATH"]
    base = os.path.join(os.path.expanduser("~"), ".config", "herdr")
    if env.get("HERDR_SESSION"):
        return os.path.join(base, "sessions", env["HERDR_SESSION"], "herdr.sock")
    return os.path.join(base, "herdr.sock")


def session_key(agent_session) -> str:
    """Claude session id from herdr's agent_session reference (an id or a transcript path)."""
    if not isinstance(agent_session, dict) or not agent_session.get("value"):
        return ""
    value = str(agent_session["value"])
    if agent_session.get("kind") == "path":
        return os.path.splitext(os.path.basename(value))[0]
    return value


class Herdr:
    def __init__(self, path: str = "", timeout: float = 1.0):
        self.path = path or socket_path()
        self.timeout = timeout
        self._n = 0

    def available(self) -> bool:
        return hasattr(socket, "AF_UNIX") and os.path.exists(self.path)

    def call(self, method: str, params: dict | None = None) -> dict:
        """One request, one response. Raises HerdrError on any failure."""
        if not hasattr(socket, "AF_UNIX"):
            raise HerdrError("unsupported", "no Unix sockets on this platform")
        self._n += 1
        request = {"id": f"agentwatch_{self._n}", "method": method, "params": params or {}}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.path)
                s.sendall((json.dumps(request) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as e:
            raise HerdrError("unreachable", str(e)) from e
        try:
            reply = json.loads(buf.split(b"\n", 1)[0].decode())
        except ValueError as e:
            raise HerdrError("bad_reply", str(e)) from e
        if not isinstance(reply, dict):
            raise HerdrError("bad_reply", "not an object")
        if reply.get("error"):
            err = reply["error"]
            raise HerdrError(str(err.get("code", "error")), str(err.get("message", "")))
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def snapshot(self) -> dict:
        """Agents herdr currently sees, trimmed to what the dashboard uses."""
        agents = self.call("agent.list").get("agents") or []
        labels = {w.get("workspace_id"): w.get("label", "")
                  for w in self.call("workspace.list").get("workspaces") or []}
        return {"connected": True, "agents": [
            {"pane_id": a.get("pane_id", ""),
             "workspace": labels.get(a.get("workspace_id"), ""),
             "status": a.get("agent_status", "unknown"),
             "name": a.get("name") or "",
             "kind": a.get("agent") or "",
             "session": session_key(a.get("agent_session"))}
            for a in agents if isinstance(a, dict) and a.get("pane_id")]}

    def focus(self, pane_id: str) -> None:
        """Focus the agent in a pane; fall back to the pane if herdr no longer sees an agent there."""
        try:
            self.call("agent.focus", {"target": pane_id})
        except HerdrError as e:
            if e.code == "unreachable":
                raise
            self.call("pane.focus", {"pane_id": pane_id})

    def open_dashboard(self, placement: str = "tab", env=None) -> dict:
        """Open agentwatch in a herdr pane through the plugin's `dashboard` entrypoint."""
        env = os.environ if env is None else env
        params: dict = {"plugin_id": env.get("HERDR_PLUGIN_ID") or PLUGIN_ID, "entrypoint": "dashboard",
                        "placement": placement, "focus": True,
                        "env": {"AGENTWATCH_HERDR_PLACEMENT": placement}}
        if placement == "popup":
            params.update(width="90%", height="90%")
        if placement == "split":
            params["direction"] = "right"
            if env.get("HERDR_PANE_ID"):
                params["target_pane_id"] = env["HERDR_PANE_ID"]
        return self.call("plugin.pane.open", params)


class HerdrWatcher:
    """Polls herdr on a background thread so a slow socket never stalls the UI."""

    def __init__(self, client: Herdr, interval: float = 1.0):
        self.client = client
        self.interval = interval
        self._latest: dict | None = None
        self._taken: dict | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> dict:
        try:
            snap = self.client.snapshot()
        except HerdrError:
            snap = {"connected": False, "agents": []}
        with self._lock:
            self._latest = snap
        return snap

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="herdr-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval)

    def resend(self) -> None:
        """Hand the current snapshot out again (the dashboard state was rebuilt)."""
        with self._lock:
            self._taken = None

    def take(self) -> dict | None:
        """The newest snapshot if it differs from the last one handed out."""
        with self._lock:
            if self._latest is None or self._latest == self._taken:
                return None
            self._taken = self._latest
            return self._taken
