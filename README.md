# agentwatch

[![ci](https://github.com/kodepapa/agent-watch/actions/workflows/ci.yml/badge.svg)](https://github.com/kodepapa/agent-watch/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)

**One terminal for all your Claude Code sessions, with a live dashboard of what they are doing.**

Run your sessions inside agentwatch, switch between them, and chat in each one
directly. Then see what the agents are doing while they do it: the lead, every teammate and
subagent, the shared task list, advisor calls, a per-agent timeline, a log tail
and token usage. Everything on screen comes from data Claude Code already
exposes (hooks, team and task files, session transcripts). Nothing is staged.

![agentwatch showing a five-agent team session](docs/screenshot.png)

<details>
<summary>Watch it move (demo session, sped up)</summary>

![animated agentwatch demo](docs/demo.gif)

</details>

## What you get

- **Your sessions in one place**: every Claude Code session runs inside
  agentwatch with its real terminal UI. A list on the left shows each one as
  working, idle or `needs you`; switch with a key and type straight into it.
  Sessions keep running when you close agentwatch.
- **Lead and team cards**: who is working, idle or done, what each agent is
  running right now, call and error counts, context size and model.
- **Task list**: the team's shared tasks with owner, state and what each
  blocked task is waiting on.
- **Advisor panel**: every advisor call, who asked, and the advice that came back.
- **Timeline**: one row per agent showing activity, errors, task claims and
  completions over the last minutes or hours.
- **Log and tokens**: a readable event tail (including repeated errors) and
  tokens read per agent.
- **Record and replay**: save a session to a file and play it back later at any speed.
- **Screenshots**: render any frame to PNG or SVG without a terminal.

## Quick start

Requires Python 3.10 or newer. Try the simulated session first; it needs no
Claude Code setup.

```sh
uvx --from git+https://github.com/kodepapa/agent-watch agentwatch demo
```

That runs the demo with [uv](https://docs.astral.sh/uv/) without installing
anything. To watch real sessions, install it so the hooks have a stable path:

```sh
uv tool install git+https://github.com/kodepapa/agent-watch
```

Or with Homebrew (macOS and Linux):

```sh
brew tap kodepapa/agent-watch https://github.com/kodepapa/agent-watch
brew install kodepapa/agent-watch/agentwatch
```

For PNG export, install with the `screenshots` extra (not included in the
Homebrew build):
`uv tool install "agentwatch[screenshots] @ git+https://github.com/kodepapa/agent-watch"`.

## Run your sessions inside agentwatch

```sh
agentwatch install-hooks         # adds async hooks to ~/.claude/settings.json (backs it up first)
agentwatch                       # opens the sessions view
```

Press `ctrl+b` then `c` to start Claude Code in a folder. It runs in the main
area with its normal interface, and everything you type goes to it. Start as
many as you like and switch between them:

| keys | what happens |
|---|---|
| `ctrl+b` `c` | new session (asks for the folder) |
| `ctrl+b` `1`-`9`, `n`, `p` | switch session; clicking one in the list works too |
| `ctrl+b` `d` | dashboard and back |
| `ctrl+b` `x` | close the current session (asks first) |
| `ctrl+b` `pgup` / `pgdn`, mouse wheel | scroll back |
| `ctrl+b` `q` | leave agentwatch; sessions keep running |
| `ctrl+b` `ctrl+b` | send a real `ctrl+b` to Claude Code |

The session list is the at-a-glance view. For every session it shows the state
(working, idle, done, or `needs you` when Claude Code is stopped on a
permission prompt or a question), how long it has been in that state, what it
is running right now, and a strip of its last 10 minutes of activity. On the dashboard, the number keys jump back to a session.

Sessions live in [tmux](https://github.com/tmux/tmux) behind the scenes, on a
private tmux server that does not touch your own tmux setup. That is what keeps
agents working after you close agentwatch: run `agentwatch` again and they are
all still there. tmux has to be installed (`brew install tmux`,
`apt install tmux`); without it agentwatch opens as a dashboard only.

`agentwatch new [folder]` starts a session from a script or another terminal,
`agentwatch ls` lists them and `agentwatch kill <name>` closes one.
`--command` runs something other than `claude`, `--prefix ctrl+a` changes the
prefix key, and `--dashboard` starts on the dashboard.

Limits of this first version: one session is visible at a time (no splits),
mouse clicks are not passed to Claude Code, and to select text with the mouse
you hold `shift` (or `option` in iTerm2) like in any full-screen terminal app.

You can still run `claude` in any other terminal. With the hooks installed it
shows up on the dashboard, but not in the session list.

`install-hooks --dry-run` prints the JSON instead of writing it, and
`--settings .claude/settings.local.json` installs for one project only.
`agentwatch uninstall-hooks` removes exactly what it added.

### What the hook does

The hook script ([`agentwatch/hooks/emit.py`](agentwatch/hooks/emit.py), 85
lines) appends one JSON line per event to `~/.agentwatch/events.jsonl` and
exits. It runs as an async hook, uses only the standard library, trims long
fields to 400 characters, and never returns an error to Claude Code.
Everything stays on your machine: agentwatch makes no network calls.

## Use it as a herdr plugin

[herdr](https://herdr.dev) is a terminal multiplexer for coding agents. If
herdr is where your sessions live, keep it that way and add agentwatch as a
pane that shows what every agent is doing:

```sh
uv tool install git+https://github.com/kodepapa/agent-watch   # the plugin runs the `agentwatch` command
agentwatch install-hooks                                      # optional, see below
herdr plugin install kodepapa/agent-watch
```

Bind a key in your herdr config:

```toml
[[keys.command]]
key = "prefix+a"
type = "plugin_action"
command = "kodepapa.agentwatch.popup"    # or .side, or .open
description = "agents"
```

| action | what opens |
|---|---|
| `kodepapa.agentwatch.popup` | the agents view as a popup: press a number and you are in that agent's pane, popup closed |
| `kodepapa.agentwatch.side` | the agents view in a split beside the current pane, staying open |
| `kodepapa.agentwatch.open` | the full dashboard (tasks, advisor, timeline, log, tokens) in a tab |

The agents view lists every agent herdr knows about, with no setup. For each
one: its state (`working`, `idle`, `done`, or `needs you` when it is stopped
on an approval or a question), and its folder. `1`-`9` or `up`/`down` +
`enter` focus that agent's pane in herdr.

With the agentwatch hooks installed (restart your Claude sessions afterwards)
each Claude Code agent also gets:

- how long it has been in its current state
- what it is running right now
- a strip of its last 10 minutes: tool calls, errors, and stretches spent
  waiting for you in amber
- a shared timeline under the list (`w` changes its window)

Sessions are named after their folder. The hook finds the pane through the
`HERDR_PANE_ID` herdr sets in every pane; herdr's own Claude integration
(`herdr integration install claude`) is a second route. `agentwatch doctor`
lists every agent herdr sees and which session it was matched to.

Outside herdr, plain `agentwatch` also lists herdr's agents next to the
sessions it hosts itself, and lets you view and type into them. It only
mirrors those panes: it never resizes or closes them, and `home`, `end`,
`page up/down` and `delete` are not passed on because herdr's API has no names
for them. `--no-herdr` turns the connection off.

### Terminal fonts

Boxes, bars and the timeline use Unicode box-drawing and block characters,
which every modern terminal font covers. If yours shows gaps or question marks,
pass `--ascii` (or set `AGENTWATCH_ASCII=1`) for plain ASCII.

## Keys

On the dashboard: `q` quit, `p` pause, `t` switch team, `w` timeline window (fit, 2m, 10m, 30m, 2h),
`+` / `-` replay speed, `1`-`9` jump to an agent's pane (inside herdr).

## Commands

| command | what it does |
|---|---|
| `agentwatch` | sessions and live dashboard (`--team`, `--since 30m`, `--record file.jsonl`, `--advisor-model "opus 5.5"`, `--no-sessions`) |
| `agentwatch new`, `ls`, `kill` | start, list and close hosted sessions from outside |
| `agentwatch demo` | simulated session (`--speed 4`, `--at 0.6` to freeze at 60%) |
| `agentwatch replay file.jsonl` | play back a recording (`--speed`, `--at`) |
| `agentwatch doctor` | show raw hook keys, resolved agents, teams, tasks, advisor calls and herdr panes |
| `agentwatch --glance` | the compact agents view the herdr plugin opens |
| `agentwatch herdr open` | open agentwatch in a herdr pane (`--view glance` or `dashboard`, `--placement tab`, `split`, `popup`) |
| `... --screenshot out.png` | render one frame to PNG or SVG and exit (`--size 132x46`) |

## Where each panel's data comes from

| panel | source |
|---|---|
| lead, team cards, log, timeline | hook events: tool calls, failures, subagents, idle, stop, tasks |
| task list | `~/.claude/tasks/<team>/*.json`, one file per task |
| team members and models | `~/.claude/teams/<team>/config.json` |
| advisor calls and advice | `server_tool_use` blocks named `advisor` in the session transcript |
| tokens read, context size, model | `usage` and `model` on assistant messages in the transcript |
| session list, `needs you` | the private tmux server, plus `Notification` hooks for permission prompts and questions |
| herdr pane ids and jump keys | herdr's socket API (`agent.list`, `agent.focus`) and `HERDR_PANE_ID` in the hook |

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
  mux.py          hosted sessions: tmux wrapper (create, capture, send keys)
  term.py         terminal view and session list widgets
  herdr.py        herdr socket client: panes, blocked agents, focus
  normalize.py    raw payloads, team/task files, transcripts -> Events
  sources.py      live collector (tails files) and replay collector
  model.py        Event and the State reducer
  panels.py       one widget per panel, each draws its own box
  app.py          Textual layout, refresh loop, keys
  sim.py          demo session writer
  shot.py         SVG/PNG screenshots
```

## Status

agentwatch is alpha (0.1.0). It is developed and tested against a simulated
session; the three assumptions listed above have not yet been confirmed against
a real agent-team run. The herdr integration is written against herdr's
published API schema (protocol 22, herdr 0.9.5) and tested with a stand-in
server, not yet against a live herdr. Hosted sessions are tested against a real tmux
and the Claude Code start screen, but not through a long working session. If a panel looks wrong on your sessions, the output of
`agentwatch doctor` in an issue is the most useful thing you can send.

Tested on Linux. macOS should work; Windows is untested.

## Development

```sh
git clone https://github.com/kodepapa/agent-watch && cd agent-watch
uv sync --extra dev
uv run pytest                    # 41 tests, about 20s (two need tmux)
uv run ruff check .
uv run agentwatch demo --at 0.58 --screenshot docs/screenshot.png   # regenerate the screenshot
uv run python scripts/make_gif.py docs/demo.gif                     # regenerate the GIF
```

Issues and pull requests are welcome. Two rules keep the project honest: every
guess about Claude Code's field names lives in `normalize.py`, and no panel
shows data that Claude Code doesn't actually provide.

## License

MIT, see [LICENSE](LICENSE). The bundled JetBrains Mono font (used only for PNG
screenshots) is under the SIL Open Font License, see
[`agentwatch/fonts/OFL.txt`](agentwatch/fonts/OFL.txt).

agentwatch is an independent project and is not affiliated with or endorsed by
Anthropic. Claude and Claude Code are trademarks of Anthropic.
