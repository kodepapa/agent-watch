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

from .mux import Screen, Session

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
             "cwd": a.get("foreground_cwd") or a.get("cwd") or "",
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


# Textual key name -> herdr key name. herdr's send_keys takes "enter", "esc", "ctrl+c", single
# characters and a few named punctuation keys; it has no home, end, page or delete keys.
HERDR_KEYS = {"escape": "esc", "enter": "enter", "tab": "tab", "shift+tab": "shift+tab",
              "backspace": "backspace", "up": "up", "down": "down", "left": "left", "right": "right",
              "space": "space", "shift+enter": "alt+enter", "alt+enter": "alt+enter"}
HERDR_CHARS = {" ": "space", "+": "plus"}


def herdr_key(key: str, character: str | None) -> str:
    """One Textual key press as a herdr key name, or "" when herdr cannot send it."""
    if key in HERDR_KEYS:
        return HERDR_KEYS[key]
    if character and character.isprintable() and not key.startswith(("ctrl+", "alt+")):
        return HERDR_CHARS.get(character, character)
    mods, _, base = key.rpartition("+")
    base = HERDR_KEYS.get(base, base)
    if mods and (len(base) == 1 or base in HERDR_KEYS.values() or (base[:1] == "f" and base[1:].isdigit())):
        return f"{mods}+{base}"
    if base[:1] == "f" and base[1:].isdigit():
        return base
    return ""


class HerdrHost:
    """herdr panes as sessions: the same calls `mux.Mux` offers, answered by herdr.

    agentwatch does not own these terminals. It mirrors the pane's screen and
    passes keys on; it never resizes or closes them.
    """

    PREFIX = "herdr:"                    # session names; tmux names cannot contain a colon
    HISTORY = 2000                       # how far back scrolling may ask herdr for

    def __init__(self, client: Herdr):
        self.client = client

    @classmethod
    def owns(cls, name: str) -> bool:
        return name.startswith(cls.PREFIX)

    @classmethod
    def pane(cls, name: str) -> str:
        return name[len(cls.PREFIX):]

    def sessions(self, agents: list[dict]) -> list[Session]:
        """Sessions for the agents in a snapshot (see `Herdr.snapshot`)."""
        return [Session(name=self.PREFIX + a["pane_id"], cwd=a.get("cwd", ""), command=a.get("kind", ""),
                        host="herdr", title=a.get("name") or "", pane=a["pane_id"],
                        status=a.get("status", ""))
                for a in agents]

    def capture(self, name: str, scroll: int = 0, height: int = 0) -> Screen:
        params = {"pane_id": self.pane(name), "source": "visible", "format": "ansi", "strip_ansi": False}
        if scroll > 0 and height:
            params.update(source="recent", lines=height + scroll)
        text = (self.client.call("pane.read", params).get("read") or {}).get("text", "")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if scroll > 0 and height:
            lines = lines[:-scroll][-height:]
        # herdr does not report the cursor; Claude Code draws its own.
        return Screen(lines, (0, 0), False, self.HISTORY, (0, len(lines)))

    def press(self, name: str, key: str, character: str | None = None) -> None:
        mapped = herdr_key(key, character)
        if mapped:
            self.client.call("pane.send_keys", {"pane_id": self.pane(name), "keys": [mapped]})

    def paste(self, name: str, text: str) -> None:
        if text:                         # herdr wraps it in a bracketed paste when the program asked
            self.client.call("pane.send_text", {"pane_id": self.pane(name), "text": text})

    def resize(self, name: str, width: int, height: int) -> None:
        pass                             # the pane's size belongs to herdr's layout


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
