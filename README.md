# agentwatch

A live terminal dashboard for Claude Code sessions and agent teams: the lead,
each teammate and subagent, the shared task list, advisor calls, a per-agent
timeline, a log tail and token usage. Built with Python and Textual.

## Install

```sh
pip install -e .                 # or: pip install -e ".[screenshots]" for PNG export
agentwatch demo                  # simulated team session, no Claude Code needed
```

## Watch your own sessions

```sh
agentwatch install-hooks         # adds async hooks to ~/.claude/settings.json (backs it up first)
claude --model sonnet --advisor opus   # in another terminal, as usual
agentwatch                       # live monitor
```

`install-hooks --dry-run` prints the JSON instead of writing it, and
`--settings .claude/settings.local.json` installs for one project only.
`agentwatch uninstall-hooks` removes exactly what it added.

The hook script (`agentwatch/hooks/emit.py`) only appends one line to
`~/.agentwatch/events.jsonl` and exits. It runs as an async hook, uses only the
standard library, trims long fields, and never returns an error to Claude Code.

Boxes, bars and the timeline use Unicode box-drawing and block characters,
which every modern terminal font covers. If yours shows gaps or question marks,
pass `--ascii` (or set `AGENTWATCH_ASCII=1`) for plain ASCII.

## Keys

`q` quit, `p` pause, `t` switch team, `w` timeline window (fit, 2m, 10m, 30m, 2h),
`+` / `-` replay speed.

## Commands

| command | what it does |
|---|---|
| `agentwatch` | live monitor (`--team`, `--since 30m`, `--record file.jsonl`, `--advisor-model "opus 5.5"`) |
| `agentwatch demo` | simulated session (`--speed 4`, `--at 0.6` to freeze at 60%) |
| `agentwatch replay file.jsonl` | play back a recording (`--speed`, `--at`) |
| `agentwatch doctor` | show raw hook keys, resolved agents, teams, tasks and advisor calls |
| `... --screenshot out.png` | render one frame to PNG or SVG and exit (`--size 132x46`) |

## Where each panel's data comes from

| panel | source |
|---|---|
| lead, team cards, log, timeline | hook events: tool calls, failures, subagents, idle, stop, tasks |
| task list | `~/.claude/tasks/<team>/*.json`, one file per task |
| team members and models | `~/.claude/teams/<team>/config.json` |
| advisor calls and advice | `server_tool_use` blocks named `advisor` in the session transcript |
| tokens read, context size, model | `usage` and `model` on assistant messages in the transcript |

Advisor "moments" (before plan, after error, mid-work, before done) are
inferred from what the agent was doing when it called the advisor. Claude Code
does not report a reason, so the panel labels them as inferred.

## What is verified and what is assumed

Verified against Claude Code's docs: the hook event names, async hooks, the
`PostToolUseFailure` `error` field, `SubagentStart`/`SubagentStop` `agent_id`
and `agent_type`, and the team and task folder locations.

Assumed, and isolated in `agentwatch/normalize.py` so they are easy to fix:

- **Teammate names in hook payloads.** agentwatch looks for `teammate_name` or
  `agent_name`, then for `CLAUDE_*AGENT*`/`*TEAMMATE*` environment variables,
  then team-config session ids. Sessions it can't name show as `session-xxxxxx`.
- **Task file fields** (`subject`, `status`, `owner`, `blockedBy`). Agent team
  files are not a documented schema and may change between releases.
- **Advisor transcript blocks.** Detected by name, and advice text is shown only
  when the result block carries text.

After your first real session, run `agentwatch doctor`. It prints the keys each
hook event actually sent and how every session was resolved, which tells you
whether any of the above needs adjusting.

## Layout

```
agentwatch/
  hooks/emit.py   hook script Claude Code runs
  normalize.py    raw payloads, team/task files, transcripts -> Events
  sources.py      live collector (tails files) and replay collector
  model.py        Event and the State reducer
  panels.py       one widget per panel, each draws its own ASCII box
  app.py          Textual layout, refresh loop, keys
  sim.py          demo session writer
  shot.py         SVG/PNG screenshots
```

Run the tests with `pytest`.
