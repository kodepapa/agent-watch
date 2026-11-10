"""Turn raw Claude Code data into `Event`s.

All guesses about field names live in this file, so when a Claude Code
release changes a payload, this is the only place to adjust. Run
`agentwatch doctor` to see the raw keys your version actually sends.
"""
from __future__ import annotations

import os
import re
from datetime import datetime

from .model import LEAD, Event

NAME_KEYS = ("teammate_name", "agent_name", "teammateName", "agentName")
ENV_NAME_HINTS = ("AGENT_NAME", "TEAMMATE_NAME", "TEAMMATE")


def parse_iso(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e12 else 1)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pretty_model(model: str) -> str:
    if not model:
        return ""
    m = re.match(r"claude-([a-z]+)-(\d+)-(\d{1,2})(?:-\d{8})?$", model)
    if m:
        return f"{m.group(1)} {m.group(2)}.{m.group(3)}"
    m = re.match(r"claude-([a-z]+)-(\d+)(?:-\d{8})?$", model)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    m = re.match(r"claude-(\d+)-(\d+)-([a-z]+)", model)
    if m:
        return f"{m.group(3)} {m.group(1)}.{m.group(2)}"
    return model.replace("claude-", "")


def short_path(path: str, cwd: str = "") -> str:
    if not isinstance(path, str):
        return ""
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    parts = path.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else path


def tool_detail(tool: str, inp, cwd: str = "") -> str:
    tool = tool or "tool"
    if tool.startswith("mcp__"):
        tool = tool.split("__")[-1]
    if not isinstance(inp, dict):
        return tool
    for key in ("file_path", "notebook_path", "path"):
        if isinstance(inp.get(key), str):
            return f"{tool} {short_path(inp[key], cwd)}"
    if isinstance(inp.get("command"), str):
        return f"{tool} {inp['command'].strip().splitlines()[0][:60]}" if inp["command"].strip() else tool
    if isinstance(inp.get("pattern"), str):
        return f"{tool} {inp['pattern'][:40]}"
    for key in ("to", "recipient"):
        if isinstance(inp.get(key), str):
            return f"{tool} to {inp[key]}"
    if isinstance(inp.get("url"), str):
        host = re.sub(r"^https?://", "", inp["url"]).split("/")[0]
        return f"{tool} {host}"
    for key in ("description", "subject", "query", "prompt"):
        if isinstance(inp.get(key), str):
            return f"{tool} {inp[key].strip().splitlines()[0][:48]}" if inp[key].strip() else tool
    return tool


def _first_line(value, n=60) -> str:
    if isinstance(value, dict):
        value = value.get("error") or value.get("stderr") or value.get("message") or ""
    if not isinstance(value, str):
        return ""
    for line in value.strip().splitlines():
        if line.strip():
            return line.strip()[:n]
    return ""


# Notifications that mean "Claude is stopped until you answer" (a permission prompt or a
# question), as opposed to "Claude is done and idle". Assumed from Claude Code's docs.
ATTENTION_TYPES = ("permission_prompt", "elicitation_dialog")
ATTENTION_HINTS = ("needs your permission", "needs your approval", "needs your attention")


class Resolver:
    """Maps sessions and agent ids to display names."""

    def __init__(self):
        self.session_names: dict[str, str] = {}
        self.agent_ids: dict[str, str] = {}      # agent_id -> display name
        self.lead_session = ""
        self.panes: dict[str, str] = {}          # display name -> herdr pane id
        self.terms: dict[str, str] = {}          # display name -> hosted session name
        self._type_counts: dict[str, int] = {}

    def learn_member(self, agent_id: str, name: str) -> None:
        if agent_id:
            self.agent_ids[agent_id] = name

    def subagent_name(self, agent_id: str, agent_type: str) -> str:
        if agent_id in self.agent_ids:
            return self.agent_ids[agent_id]
        base = (agent_type or "subagent").lower().replace(" ", "-")
        n = self._type_counts.get(base, 0) + 1
        self._type_counts[base] = n
        name = base if n == 1 else f"{base}-{n}"
        if agent_id:
            self.agent_ids[agent_id] = name
        return name

    def name_for(self, p: dict) -> str:
        for key in NAME_KEYS:
            if isinstance(p.get(key), str) and p[key]:
                return self._bind(p, p[key])
        env = p.get("_env") or {}
        for key, val in env.items():
            if any(h in key for h in ENV_NAME_HINTS) and isinstance(val, str) and val:
                return self._bind(p, val)
        agent_id = p.get("agent_id")
        if (agent_id and agent_id in self.agent_ids
                and p.get("hook_event_name") not in ("SubagentStart", "SubagentStop")):
            return self.agent_ids[agent_id]
        session = p.get("session_id", "")
        if session in self.session_names:
            return self.session_names[session]
        hosted = p.get("_aw_session")
        if isinstance(hosted, str) and hosted:
            # A session agentwatch hosts is called what its tab is called.
            return self._bind(p, hosted)
        if not self.lead_session and session:
            self.lead_session = session
            self.session_names[session] = LEAD
            return LEAD
        if not session:
            return LEAD
        name = f"session-{session[:6]}"
        self.session_names[session] = name
        return name

    def _bind(self, p: dict, name: str) -> str:
        session = p.get("session_id", "")
        if session and session not in self.session_names:
            self.session_names[session] = name
        return name


def hook_events(p: dict, r: Resolver) -> list[Event]:
    """One raw hook payload -> zero or more Events."""
    events = _hook_events(p, r)
    hosted = p.get("_aw_session")
    if hosted and events:
        for agent in dict.fromkeys(ev.agent for ev in events):
            if r.terms.get(agent) != hosted:
                r.terms[agent] = hosted
                events.append(Event(ts=events[0].ts, kind="term", agent=agent,
                                    session=events[0].session, data={"name": hosted}))
    pane = (p.get("_herdr") or {}).get("pane_id")
    if pane and events:
        # The hook saw herdr's pane id in its environment: tell the model once per agent.
        for agent in dict.fromkeys(ev.agent for ev in events):
            if r.panes.get(agent) != pane:
                r.panes[agent] = pane
                events.append(Event(ts=events[0].ts, kind="herdr_pane", agent=agent,
                                    session=events[0].session, data={"pane_id": pane}))
    return events


def _hook_events(p: dict, r: Resolver) -> list[Event]:
    name = p.get("hook_event_name", "")
    ts = float(p.get("_ts") or 0) or 0.0
    session = p.get("session_id", "")
    cwd = p.get("cwd", "")
    agent = r.name_for(p)
    base = dict(ts=ts, agent=agent, session=session)
    tool = p.get("tool_name", "")
    inp = p.get("tool_input")

    if name == "SessionStart":
        return [Event(kind="session_start", detail=str(p.get("source", "")), **base)]
    if name == "SessionEnd":
        return [Event(kind="session_end", **base)]
    if name == "UserPromptSubmit":
        prompt = p.get("prompt", "")
        return [Event(kind="prompt", detail=_first_line(prompt, 48), **base)]
    if name == "PreToolUse":
        return [Event(kind="tool_start", detail=tool_detail(tool, inp, cwd),
                      data={"tool": tool}, **base)]
    if name == "PostToolUse":
        err = bool(p.get("_is_error"))
        text = _first_line(p.get("tool_response")) if err else ""
        return [Event(kind="tool_end", detail=tool_detail(tool, inp, cwd),
                      data={"tool": tool, "error": err, "error_text": text,
                            "error_sig": f"{tool}:{text}" if err else ""}, **base)]
    if name == "PostToolUseFailure":
        if p.get("is_interrupt"):
            return [Event(kind="notification", detail=f"interrupted · {tool}", **base)]
        text = _first_line(p.get("error"))
        return [Event(kind="tool_end", detail=tool_detail(tool, inp, cwd),
                      data={"tool": tool, "error": True, "error_text": text,
                            "error_sig": f"{tool}:{text}"}, **base)]
    if name == "SubagentStart":
        sub = r.subagent_name(p.get("agent_id", ""), p.get("agent_type", ""))
        return [Event(kind="subagent_start", detail=str(p.get("agent_type", "")),
                      data={"subagent": sub}, **base)]
    if name == "SubagentStop":
        sub = r.subagent_name(p.get("agent_id", ""), p.get("agent_type", ""))
        return [Event(kind="subagent_stop", data={"subagent": sub}, **base)]
    if name == "TeammateIdle":
        who = p.get("teammate_name") or agent
        return [Event(kind="idle", **{**base, "agent": who})]
    if name in ("Stop", "SubagentStop"):
        return [Event(kind="stop", **base)]
    if name == "Notification":
        message = str(p.get("message") or "")
        waiting = (p.get("notification_type") in ATTENTION_TYPES
                   or any(hint in message.lower() for hint in ATTENTION_HINTS))
        return [Event(kind="notification", detail=_first_line(message, 60),
                      data={"attention": waiting}, **base)]
    if name in ("PreCompact",):
        return [Event(kind="compact", **base)]
    if name in ("TaskCreated", "TaskCompleted"):
        task = p.get("task") if isinstance(p.get("task"), dict) else p
        tid = task.get("task_id") or task.get("taskId") or task.get("id") or ""
        subject = task.get("task_subject") or task.get("subject") or task.get("title") or ""
        who = p.get("teammate_name") or task.get("owner") or agent
        kind = "task_created" if name == "TaskCreated" else "task_completed"
        return [Event(kind=kind, data={"id": str(tid), "subject": subject},
                      **{**base, "agent": who})]
    return []


# ---- team and task files ---------------------------------------------------

STATUS_MAP = {"done": "completed", "complete": "completed", "completed": "completed",
              "in_progress": "in_progress", "in-progress": "in_progress", "active": "in_progress",
              "pending": "pending", "open": "pending", "todo": "pending"}


def team_event(team: str, cfg: dict, ts: float, r: Resolver, known: set) -> Event:
    lead_id = cfg.get("leadAgentId") or cfg.get("lead_agent_id") or cfg.get("lead") or ""
    members = []
    for m in cfg.get("members") or []:
        if not isinstance(m, dict):
            continue
        raw_name = m.get("name") or m.get("agentId") or "member"
        agent_id = m.get("agentId") or m.get("agent_id") or ""
        atype = str(m.get("agentType") or m.get("agent_type") or "")
        is_lead = (lead_id and lead_id in (agent_id, raw_name)) or "lead" in atype.lower() \
            or raw_name in ("team-lead", "lead")
        name = LEAD if is_lead else raw_name
        r.learn_member(agent_id, name)
        if m.get("sessionId") or m.get("session_id"):
            r.session_names[m.get("sessionId") or m.get("session_id")] = name
        members.append({"name": name, "role": LEAD if is_lead else "teammate",
                        "model": pretty_model(str(m.get("model") or "")),
                        "color": str(m.get("color") or "")})
    new = [m["name"] for m in members if m["name"] not in known and m["role"] != LEAD]
    known.update(m["name"] for m in members)
    return Event(ts=ts, kind="team", data={"team": cfg.get("name") or team,
                                          "members": members, "new_members": new})


def task_event(task: dict, ts: float, live: bool) -> Event:
    tid = task.get("id") or task.get("taskId") or ""
    status = STATUS_MAP.get(str(task.get("status", "pending")).lower(), "pending")
    owner = task.get("owner") or task.get("assignee") or ""
    owner = LEAD if owner in ("team-lead",) else owner
    return Event(ts=ts, kind="task_upsert", agent=owner, data={
        "id": str(tid),
        "subject": task.get("subject") or task.get("title") or task.get("content") or "",
        "status": status, "owner": owner,
        "blocked_by": task.get("blockedBy") or task.get("blocked_by") or [],
        "live": live})


# ---- transcripts -----------------------------------------------------------

def _block_text(block) -> str:
    content = block.get("content", block.get("text", ""))
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or content.get("advice") or "")
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


class TranscriptParser:
    """Stateful per-transcript parser (dedupes repeated message ids)."""

    def __init__(self, agent: str, session: str):
        self.agent = agent
        self.session = session
        self.seen_ids: set = set()
        self.advisor_ids: set = set()

    def parse(self, line: dict) -> list[Event]:
        out: list[Event] = []
        ts = parse_iso(line.get("timestamp")) or 0.0
        msg = line.get("message") if isinstance(line.get("message"), dict) else {}
        base = dict(ts=ts, agent=self.agent, session=self.session)
        mid = msg.get("id") or line.get("uuid")
        if line.get("type") == "assistant" and msg and mid not in self.seen_ids:
            self.seen_ids.add(mid)
            if len(self.seen_ids) > 20000:
                self.seen_ids.clear()
            usage = msg.get("usage") or {}
            tin = sum(int(usage.get(k) or 0) for k in
                      ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            out.append(Event(kind="model", data={"model": pretty_model(msg.get("model", "")),
                                                 "tokens_in": tin, "ctx_tokens": tin}, **base))
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type", ""))
            bname = str(block.get("name", ""))
            if btype.endswith("tool_use") and "advisor" in (bname + btype).lower():
                bid = block.get("id", "")
                if bid in self.advisor_ids:
                    continue
                self.advisor_ids.add(bid)
                model = ""
                inp = block.get("input")
                if isinstance(inp, dict):
                    model = pretty_model(str(inp.get("model") or ""))
                out.append(Event(kind="advisor_call", data={"id": bid, "model": model}, **base))
            elif btype.endswith("result") and ("advisor" in btype.lower()
                                               or block.get("tool_use_id") in self.advisor_ids):
                out.append(Event(kind="advisor_result",
                                 data={"id": block.get("tool_use_id", ""),
                                       "text": _block_text(block)}, **base))
        return out


def project_dir_for(cwd: str, claude_dir: str) -> str:
    """Claude Code stores transcripts under projects/<cwd with / replaced by ->."""
    return os.path.join(claude_dir, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
