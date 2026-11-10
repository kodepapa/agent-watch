# agentwatch — project context

Live terminal dashboard for Claude Code sessions and agent teams. Python + Textual.
This file carries over the decisions from the chat where the project was designed
and built, so a new session can continue without that history.

## Origin and goal

Inspired by marketing graphics on X that show Claude Code "agent teams" as a
terminal dashboard (advisor panel, shared task list, teammate cards, session
timeline, log tail). Those graphics are staged animations, not real tools.
The goal here is a **real live monitor** that shows the same things from data
Claude Code actually exposes. Do not add panels that would need invented data.

Stack decision: Python + Textual (user's preference when frameworks are equally
capable). Textual's stock widgets look too "modern", so every panel draws its
own box as Rich `Text` via `theme.box()`.

## Data flow

```
tmux (mux.py) ──> term.py: the hosted sessions themselves, drawn and typed into directly

hooks/emit.py ──> ~/.agentwatch/events.jsonl ─┐
~/.claude/teams/<team>/config.json ───────────┤
~/.claude/tasks/<team>/*.json ────────────────┼─> sources.py ─> normalize.py ─> Event ─> model.State ─> panels
session transcripts (transcript_path) ────────┘        (LiveCollector / ReplayCollector)
```

- `emit.py` runs as an **async** hook on every event, stdlib only, trims long
  fields, appends one JSON line, always exits 0. Keep it that way: it runs
  inside the user's Claude Code sessions.
- Every source produces `Event`s; the UI reads only `State`. Live, demo and
  replay all render through the same path. Keep it that way.
- `--record` writes normalized events; `replay` plays them back with a virtual clock.

## Conventions

- **All guesses about Claude Code field names live in `normalize.py`.** Nowhere else.
- One color per agent everywhere, via `theme.agent_color(name, hint)`. Team
  config colors win over auto-assigned ones (an early auto color gets bumped).
- Glyphs come from `theme.G` (Unicode box drawing and block characters).
  `--ascii` / `AGENTWATCH_ASCII=1` swaps in ASCII. Never hardcode a glyph in a panel.
- Boxes: rounded (`╭─╮`) for normal panels, heavy (`┏━┓`) for lead and advisor.
- Labels are lowercase. Palette lives in `theme.py` (slate background `#1c1f26`,
  warm ink `#d8d4c8`, lead amber, advisor straw, error red, ok green).
- Timeline cells: block heights by tool calls per bucket (`▄▆█`), `▂` for
  working between calls, amber `▂` while the agent needs the user, `▁` idle,
  `✕` error, `◆` advisor, `✓` task done, `┃` task claimed, `▸` prompt.
- Timeline rules (`panels.activity`, shared by the timeline and the session
  list strip). Keep them: cells are absolute time buckets, so marks never
  slide within a cell; "fit" picks seconds-per-cell from fixed `STEPS`; a row
  carries its state between marks (work from prompt/tool until stop/idle,
  wait from `wait` until `go`) and paints the live state up to now; marks are
  sorted before drawing because sources arrive out of order.
- Claude Code sends no Stop when the user interrupts. `interrupt` events and
  idle-prompt notifications end a stale "working" state.
- Advisor "moments" are **inferred** (before plan, after error, mid-work,
  before done) and the panel says so. Claude Code doesn't report a reason.

## Hosted sessions (the sessions view)

The user wants agentwatch to be the place the sessions live, replacing herdr:
see all sessions, switch, and chat in them without leaving the app.

- `mux.py` wraps tmux: one detached tmux session per Claude Code session, on a
  private server (`tmux -L agentwatch -f /dev/null`). tmux is the terminal
  emulator and what keeps sessions alive after agentwatch exits. No Textual
  imports there. Sessions start with `AGENTWATCH_SESSION=<name>` and with
  `TMUX` unset (so Claude Code keeps teammates in-process instead of splitting
  panes we do not draw).
- `term.py`: `TerminalView` draws `capture-pane -e` output with
  `Text.from_ansi` and forwards keys (`mux.translate`) through a sender
  thread; a capture thread polls fast while busy, slow while quiet.
  `SessionList` is the sidebar.
- `app.py`: `#workspace` (sessions view) and `#dashboard` are siblings; one is
  displayed at a time. `handle_prefix` implements the herdr-style prefix
  (`ctrl+b`, then c/1-9/n/p/d/x/q). The terminal widget stops every key event,
  so dashboard bindings (q, p, t, w) never fire while typing in a session.
- `hooks/emit.py` reports `_aw_session`; `Resolver.name_for` names a hosted
  session after its tab, and a `term` event sets `Agent.term`.
- `needs you` without herdr: `Notification` hooks with
  `notification_type` `permission_prompt` / `elicitation_dialog` set
  `Agent.hook_blocked`; the next tool/prompt/stop event clears it. These
  names are assumptions (in `normalize.py`), not yet seen in a real payload.
- tmux gotchas found the hard way: `set -g window-size manual` crashes tmux
  3.4 on the next `new-session`; control characters in `-F` formats are
  escaped (hence the `" :: "` separator); `default-terminal` is
  `screen-256color` because macOS has no `tmux-256color` terminfo.
- Not built: splits, mouse pass-through, a persistent tmux control-mode
  connection (each key and capture is a tmux subprocess today).

## herdr integration

herdr (https://herdr.dev, source github.com/herdrdev/herdr) is the user's
terminal multiplexer for parallel agent sessions. Integration points:

- `hooks/emit.py` adds `_herdr` (pane, tab, workspace ids from the `HERDR_*`
  env vars herdr sets in every pane) to each event. `normalize.hook_events`
  turns that into a `herdr_pane` event, once per agent.
- `herdr.py` is the socket client (newline-delimited JSON over a Unix socket,
  path from `HERDR_SOCKET_PATH`, `HERDR_SESSION`, or `~/.config/herdr/herdr.sock`).
  `HerdrWatcher` polls `agent.list` + `workspace.list` on a thread; the
  collector emits a `herdr` event when the snapshot changes. So herdr data
  still flows through Events and State like every other source.
- `State._sync_herdr` matches herdr agents to ours by pane id, else by Claude
  session id (herdr's `agent_session`, an id or a transcript path), and sets
  `Agent.pane`, `.workspace`, `.blocked`.
- UI: `needs you` badge, pane id + jump number (`panels.pane_tag`), keys `1`-`9`
  call `agent.focus` (fallback `pane.focus`). When opened as a popup or overlay
  (`AGENTWATCH_HERDR_PLACEMENT`), the app exits after a jump.
- `herdr-plugin.toml` at the repo root makes the repo a herdr plugin
  (`herdr plugin install kodepapa/agent-watch`). Its actions run
  `agentwatch herdr open --placement ...`, which calls `plugin.pane.open`.

herdr field names come from its published schema
(`docs/next/api/herdr-api.schema.json`, protocol 22), so they live in
`herdr.py`, not `normalize.py`. Not yet run against a live herdr: tests use
the stand-in server in `tests/test_herdr.py`. Unverified in particular: that
focus lands on the target pane after a popup closes, and the manifest
validating under `herdr plugin install`.

## Verified vs assumed

Verified against Claude Code docs: hook event names (SessionStart, SessionEnd,
UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, SubagentStart,
SubagentStop, Stop, Notification, PreCompact, TeammateIdle, TaskCreated,
TaskCompleted), async hooks, `PostToolUseFailure.error`, `SubagentStart/Stop`
`agent_id`/`agent_type`, team and task folder locations, the `--advisor` flag.

Assumed (not yet checked against a real session):
1. Which field carries a teammate's name in hook payloads (`NAME_KEYS`,
   env vars, team-config session ids). Unresolved sessions show as `session-xxxxxx`.
2. Task file fields: `subject`, `status`, `owner`, `blockedBy` (undocumented schema).
3. Advisor in transcripts: a `*tool_use` block whose name contains `advisor`,
   result in a `*result` block with text.

**First real task:** run a real agent-team session with hooks installed, then
`agentwatch doctor`. It prints the raw keys per hook event and how each session
resolved. Fix any of the three assumptions in `normalize.py` and add a test.

## Commands

```sh
uv sync --extra dev                               # uv only, no pip; uv.lock is committed
uv run pytest                                     # 35 tests, ~15s; two need tmux
uv run ruff check .                               # config in pyproject.toml; CI runs both
uv run agentwatch demo                            # simulated session (sim.py)
uv run agentwatch demo --at 0.58 --screenshot out.png --size 132x46
uv run agentwatch install-hooks --dry-run         # see the settings.json change first
uv run agentwatch doctor
uv run python scripts/make_gif.py docs/demo.gif   # animated capture of the live demo
```

Screenshots: `shot.py` renders Textual's compositor output to PNG with Pillow
and draws box/block characters as geometry so lines meet at cell edges.
Bundled font: JetBrains Mono (OFL, `agentwatch/fonts/`).
The README images live in `docs/` (`screenshot.png`, `demo.gif`); regenerate
them with the two commands above when the UI changes.

## Releasing

`Formula/agentwatch.rb` makes this repo its own Homebrew tap. It pins a source
tarball by commit and its sha256, plus one `resource` per Python dependency
(sdists from PyPI). On a release: bump `__version__`, then update `url`,
`version` and `sha256` in the formula to the new commit or tag tarball, and
refresh the resources if dependencies changed (`brew update-python-resources`).

## Known gaps and ideas (not started)

- Phase bar (plan > spawn > claim > work > merge > review > ship) was left out
  on purpose: not a real Claude Code state. Could be inferred from events.
- Subagent transcripts (`agent_transcript_path`) are followed but not
  separately verified.
- "tokens read" sums input + cache read + cache creation per assistant message,
  so it grows fast; it is not a cost figure.
- No Windows testing (emitter guards the `fcntl` import).
- History load defaults to the last 2h (`--since`).
