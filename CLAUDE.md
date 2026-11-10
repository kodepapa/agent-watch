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
  working stretches between calls (gaps under 25s), `▁` idle, `✕` error,
  `◆` advisor, `✓` task done, `┃` task claimed, `▸` prompt.
- Advisor "moments" are **inferred** (before plan, after error, mid-work,
  before done) and the panel says so. Claude Code doesn't report a reason.

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
pip install -e ".[dev]"
python -m pytest                           # 11 tests, ~10s
ruff check .                               # config in pyproject.toml; CI runs both
agentwatch demo                            # simulated session (sim.py)
agentwatch demo --at 0.58 --screenshot out.png --size 132x46
agentwatch install-hooks --dry-run         # see the settings.json change first
agentwatch doctor
python scripts/make_gif.py docs/demo.gif   # animated capture of the live demo
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
