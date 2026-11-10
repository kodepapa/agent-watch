"""agentwatch command line."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time

from . import __version__

HOME = os.path.expanduser("~")
DEFAULT_EVENTS = os.environ.get("AGENTWATCH_EVENTS") or os.path.join(HOME, ".agentwatch", "events.jsonl")
DEFAULT_CLAUDE = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
EMIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "emit.py")
HOOK_EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "PostToolUseFailure", "SubagentStart", "SubagentStop", "Stop", "Notification",
               "PreCompact", "TeammateIdle", "TaskCreated", "TaskCompleted"]
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}


def parse_duration(value: str) -> float:
    value = value.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if value and value[-1] in mult:
        return float(value[:-1]) * mult[value[-1]]
    return float(value)


# ---- hooks -----------------------------------------------------------------

def hook_command() -> str:
    return f'"{sys.executable}" "{EMIT}"'


def _load_settings(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        text = f.read().strip()
    return json.loads(text) if text else {}


def install_hooks(args) -> int:
    path = args.settings
    settings = _load_settings(path)
    hooks = settings.setdefault("hooks", {})
    added = []
    for event in args.events or HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if any("emit.py" in h.get("command", "") and "agentwatch" in h.get("command", "")
               for g in groups for h in g.get("hooks", [])):
            continue
        entry = {"hooks": [{"type": "command", "command": hook_command(), "timeout": 5, "async": True}]}
        if event in TOOL_EVENTS:
            entry["matcher"] = "*"
        groups.append(entry)
        added.append(event)
    if args.dry_run:
        print(json.dumps({"hooks": {e: hooks[e] for e in (args.events or HOOK_EVENTS)}}, indent=2))
        return 0
    if not added:
        print(f"agentwatch hooks are already installed in {path}")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        print(f"backed up {path} to {backup}")
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print(f"added agentwatch hooks for {len(added)} events to {path}")
    print("restart running claude sessions so they pick up the hooks, then run: agentwatch")
    return 0


def uninstall_hooks(args) -> int:
    path = args.settings
    if not os.path.exists(path):
        print(f"nothing to remove: {path} does not exist")
        return 0
    settings = _load_settings(path)
    hooks = settings.get("hooks", {})
    removed = 0
    for event in list(hooks):
        kept = []
        for g in hooks[event]:
            inner = [h for h in g.get("hooks", []) if not ("emit.py" in h.get("command", "")
                                                          and "agentwatch" in h.get("command", ""))]
            removed += len(g.get("hooks", [])) - len(inner)
            if inner:
                kept.append({**g, "hooks": inner})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not removed:
        print(f"no agentwatch hooks found in {path}")
        return 0
    if not hooks:
        settings.pop("hooks", None)
    shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print(f"removed {removed} agentwatch hook entries from {path}")
    return 0


# ---- doctor ------------------------------------------------------------------

def doctor(args) -> int:
    from .model import State
    from .sources import LiveCollector

    print(f"events file   {args.events}")
    if not os.path.exists(args.events):
        print("              missing: run `agentwatch install-hooks`, then start claude")
    else:
        size = os.path.getsize(args.events)
        print(f"              {size:,} bytes")
    print(f"claude dir    {args.claude_dir}")
    col = LiveCollector(args.events, args.claude_dir, since=parse_duration(args.since))
    events = col.poll()
    state = State()
    state.apply_all(events)
    print(f"teams         {', '.join(col.teams()) or 'none found'}")
    print(f"active team   {col.team or '-'}")
    print(f"transcripts   {len(col.transcripts)} followed")
    print(f"events        {len(events)} normalized from the last {args.since}")
    print()
    print("hook payload keys seen (last sample per event):")
    if not col.raw_samples:
        print("  none yet")
    for name, p in sorted(col.raw_samples.items()):
        keys = sorted(k for k in p if not k.startswith("_"))
        print(f"  {name:<20} {', '.join(keys)}")
        if p.get("_env"):
            print(f"  {'':<20} env: {', '.join(sorted(p['_env']))}")
    print()
    print("agents as resolved:")
    for a in state.ordered_agents():
        print(f"  {a.name:<14} role={a.role:<9} model={a.model or '-':<12} calls={a.tool_calls:<4} "
              f"tokens={a.tokens_read}")
    print(f"tasks         {len(state.tasks)}  advisor calls {len(state.advisor_calls)}")
    _doctor_herdr(state)
    unnamed = [a.name for a in state.agents.values() if a.name.startswith("session-")]
    if unnamed:
        print()
        print("some sessions have no agent name. Check the payload keys above for the field that")
        print("carries the teammate name and add it to NAME_KEYS in agentwatch/normalize.py.")
    return 0


def _doctor_herdr(state) -> None:
    from .herdr import Herdr, HerdrError
    from .model import Event

    client = Herdr()
    print()
    if not client.available():
        print(f"herdr         no server socket at {client.path}")
        return
    try:
        pong = client.call("ping")
        snap = client.snapshot()
    except HerdrError as e:
        print(f"herdr         socket {client.path} did not answer: {e}")
        return
    print(f"herdr         {pong.get('version', '?')} (protocol {pong.get('protocol', '?')}) on {client.path}")
    state.apply(Event(ts=time.time(), kind="herdr", data=snap))
    matched = {a.pane: a.name for a in state.agents.values() if a.pane and a.role != "subagent"}
    for h in snap["agents"]:
        who = matched.get(h["pane_id"], "not matched to a session agentwatch has seen")
        print(f"  {h['pane_id']:<10} {h['kind'] or '?':<8} {h['status']:<8} {h['workspace'] or '-':<14} {who}")
    if not snap["agents"]:
        print("  herdr reports no agents")


# ---- running the app -----------------------------------------------------------

def _run(app, args) -> int:
    if getattr(args, "screenshot", ""):
        from .shot import screenshot
        w, h = (int(x) for x in args.size.lower().split("x"))
        print(screenshot(app, args.screenshot, size=(w, h), title=args.title))
        return 0
    app.run()
    return 0


def _glyphs(args) -> None:
    if getattr(args, "ascii", False):
        from . import theme
        theme.use_ascii(True)


def watch(args) -> int:
    _glyphs(args)
    from .app import AgentWatchApp
    from .sources import LiveCollector

    client = None if args.no_herdr else _herdr_client()
    watcher = None
    if client:
        from .herdr import TRANSIENT, HerdrWatcher
        watcher = HerdrWatcher(client)
        watcher.poll_once()
        watcher.start()
    mux = None if (args.no_sessions or args.screenshot) else _mux(args.command)
    col = LiveCollector(args.events, args.claude_dir, team=args.team,
                        since=parse_duration(args.since), record_path=args.record, herdr=watcher)
    app = AgentWatchApp(col, mode="live", advisor_model=args.advisor_model, events_hint=args.events,
                        herdr=client, mux=mux, prefix=args.prefix,
                        view="dashboard" if args.dashboard else "sessions",
                        leave_after_jump=bool(client) and os.environ.get("AGENTWATCH_HERDR_PLACEMENT") in TRANSIENT)
    try:
        return _run(app, args)
    finally:
        if watcher:
            watcher.stop()
        if mux:
            left = len(mux.sessions())
            if left:
                print(f"{left} session{'' if left == 1 else 's'} still running. "
                      "Run agentwatch to return, or agentwatch ls to list them.")


def _mux(command: str = ""):
    """The session host when tmux is installed, else None (dashboard only)."""
    from .mux import DEFAULT_COMMAND, Mux
    return Mux(command=command or DEFAULT_COMMAND) if Mux.available() else None


def _need_mux(args):
    mux = _mux(getattr(args, "command", ""))
    if not mux:
        print("hosted sessions need tmux, which is not installed (brew install tmux / apt install tmux)",
              file=sys.stderr)
    return mux


def sessions_new(args) -> int:
    from .mux import MuxError
    mux = _need_mux(args)
    if not mux:
        return 1
    try:
        print(mux.new(args.folder, name=args.name))
    except MuxError as e:
        print(f"could not start a session: {e}", file=sys.stderr)
        return 1
    return 0


def sessions_ls(args) -> int:
    mux = _need_mux(args)
    if not mux:
        return 1
    for s in mux.sessions():
        print(f"{s.name:<24} {s.command:<10} {s.cwd}")
    return 0


def sessions_kill(args) -> int:
    mux = _need_mux(args)
    if not mux:
        return 1
    if args.name not in [s.name for s in mux.sessions()]:
        print(f"no session named {args.name}", file=sys.stderr)
        return 1
    mux.kill(args.name)
    return 0


def _herdr_client():
    """A herdr client when a herdr server socket exists, else None."""
    from .herdr import Herdr
    client = Herdr()
    return client if client.available() else None


def herdr_open(args) -> int:
    from .herdr import PLUGIN_ID, Herdr, HerdrError
    client = Herdr()
    try:
        client.open_dashboard(args.placement)
    except HerdrError as e:
        print(f"could not open agentwatch in herdr: {e}", file=sys.stderr)
        if e.code == "unreachable":
            print(f"no herdr server answered on {client.path}", file=sys.stderr)
        else:
            print(f"is the plugin installed? run: herdr plugin install kodepapa/agent-watch  (id {PLUGIN_ID})",
                  file=sys.stderr)
        return 1
    return 0


def demo(args) -> int:
    _glyphs(args)
    from .app import AgentWatchApp
    from .sim import DemoSession
    from .sources import LiveCollector

    root = tempfile.mkdtemp(prefix="agentwatch-demo-")
    claude_dir = os.path.join(root, "claude")
    events = os.path.join(root, "events.jsonl")
    sim = DemoSession(claude_dir, events, seed=args.seed)
    stop = threading.Event()
    if args.at is not None:
        sim.generate(until=sim.duration * args.at)
    else:
        sim.run_live(speed=args.speed, stop=stop)
    col = LiveCollector(events, claude_dir, since=86400)
    app = AgentWatchApp(col, mode="demo", advisor_model=args.advisor_model or "opus 5.5",
                        events_hint=events)
    try:
        return _run(app, args)
    finally:
        stop.set()
        shutil.rmtree(root, ignore_errors=True)


def replay(args) -> int:
    _glyphs(args)
    from .app import AgentWatchApp
    from .sources import ReplayCollector

    col = ReplayCollector(args.file, speed=args.speed)
    if args.at is not None and col.events:
        col.virtual = col.events[0].ts + (col.events[-1].ts - col.events[0].ts) * args.at
    app = AgentWatchApp(col, mode="replay", advisor_model=args.advisor_model, events_hint=args.file)
    return _run(app, args)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentwatch",
                                description="Live terminal dashboard for Claude Code sessions and agent teams.")
    p.add_argument("--version", action="version", version=f"agentwatch {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp, live=True):
        sp.add_argument("--advisor-model", default="", help="label for the advisor model, e.g. 'opus 5.5'")
        sp.add_argument("--screenshot", default="", help="render one frame to .png or .svg and exit")
        sp.add_argument("--size", default="132x46", help="screenshot size in cells, e.g. 132x46")
        sp.add_argument("--title", default="agentwatch", help="screenshot window title")
        sp.add_argument("--ascii", action="store_true", default=bool(os.environ.get("AGENTWATCH_ASCII")),
                        help="plain ASCII boxes for terminals without box-drawing fonts")
        if live:
            sp.add_argument("--events", default=DEFAULT_EVENTS)
            sp.add_argument("--claude-dir", default=DEFAULT_CLAUDE)

    w = sub.add_parser("watch", help="watch live sessions (default)")
    common(w)
    w.add_argument("--team", default="", help="pin a team name (default: most recent)")
    w.add_argument("--since", default="2h", help="history to load on start, e.g. 30m, 2h")
    w.add_argument("--record", default="", help="also write normalized events to this file")
    w.add_argument("--no-herdr", action="store_true", help="do not talk to a running herdr server")
    w.add_argument("--no-sessions", action="store_true", help="dashboard only: do not host sessions")
    w.add_argument("--dashboard", action="store_true", help="start on the dashboard instead of the sessions")
    w.add_argument("--command", default="", help="program a new session runs (default: claude)")
    w.add_argument("--prefix", default=os.environ.get("AGENTWATCH_PREFIX") or "ctrl+b",
                   help="key that starts an agentwatch command inside a session (default: ctrl+b)")

    d = sub.add_parser("demo", help="run a simulated agent team session")
    common(d, live=False)
    d.add_argument("--speed", type=float, default=2.0)
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--at", type=float, default=None, help="jump to a point, 0..1, and freeze")

    r = sub.add_parser("replay", help="replay a file made with watch --record")
    common(r, live=False)
    r.add_argument("file")
    r.add_argument("--speed", type=float, default=4.0)
    r.add_argument("--at", type=float, default=None, help="start at a point, 0..1")

    for name, fn, helptext in (("install-hooks", install_hooks, "add agentwatch hooks to Claude Code settings"),
                               ("uninstall-hooks", uninstall_hooks, "remove agentwatch hooks")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--settings", default=os.path.join(DEFAULT_CLAUDE, "settings.json"))
        if name == "install-hooks":
            sp.add_argument("--dry-run", action="store_true", help="print the hooks JSON instead of writing")
            sp.add_argument("--events", nargs="*", help="only these hook events")
        sp.set_defaults(fn=fn)

    doc = sub.add_parser("doctor", help="show what agentwatch can see and how it maps it")
    doc.add_argument("--events", default=DEFAULT_EVENTS)
    doc.add_argument("--claude-dir", default=DEFAULT_CLAUDE)
    doc.add_argument("--since", default="2h")

    n = sub.add_parser("new", help="start a hosted session without opening agentwatch")
    n.add_argument("folder", nargs="?", default=".")
    n.add_argument("--name", default="", help="session name (default: the folder name)")
    n.add_argument("--command", default="", help="program to run (default: claude)")
    n.set_defaults(fn=sessions_new)
    sub.add_parser("ls", help="list hosted sessions").set_defaults(fn=sessions_ls)
    k = sub.add_parser("kill", help="close a hosted session")
    k.add_argument("name")
    k.set_defaults(fn=sessions_kill)

    h = sub.add_parser("herdr", help="herdr integration")
    hsub = h.add_subparsers(dest="herdr_cmd", required=True)
    ho = hsub.add_parser("open", help="open agentwatch in a herdr pane (needs the herdr plugin)")
    ho.add_argument("--placement", choices=["tab", "split", "popup", "overlay", "zoomed"], default="tab")
    ho.set_defaults(fn=herdr_open)

    w.set_defaults(fn=watch)
    d.set_defaults(fn=demo)
    r.set_defaults(fn=replay)
    doc.set_defaults(fn=doctor)

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in sub.choices and argv[0] not in ("-h", "--help", "--version")):
        argv = ["watch"] + argv
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
