#!/usr/bin/env python3
"""agentwatch hook emitter.

Claude Code runs this on every hook event and pipes the event JSON to stdin.
It appends one line to the events file and exits. It must stay fast and must
never fail loudly, so it imports only the standard library and swallows errors.

Events file: $AGENTWATCH_EVENTS, else ~/.agentwatch/events.jsonl
"""
import json
import os
import sys
import time

MAX_STR = 400          # long strings (file contents, command output) are cut
MAX_DEPTH = 4
ENV_HINTS = ("AGENT", "TEAM", "TEAMMATE")


def _trim(value, depth=0):
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else value[:MAX_STR] + "…"
    if depth >= MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        return {k: _trim(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_trim(v, depth + 1) for v in value[:20]]
    return value


def _response_error(resp):
    """Best-effort error flag for PostToolUse responses."""
    if isinstance(resp, dict):
        if resp.get("is_error") or resp.get("error"):
            return True
        if resp.get("interrupted"):
            return True
        code = resp.get("exit_code", resp.get("exitCode", resp.get("returncode")))
        if isinstance(code, int) and code != 0:
            return True
    return False


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {"_unparsed": raw[:MAX_STR]}
    if not isinstance(payload, dict):
        payload = {"_value": payload}

    if "tool_response" in payload:
        payload["_is_error"] = _response_error(payload["tool_response"])

    payload = _trim(payload)
    payload["_ts"] = time.time()
    env = {k: v for k, v in os.environ.items()
           if k.startswith("CLAUDE") and any(h in k for h in ENV_HINTS)}
    if env:
        payload["_env"] = _trim(env)

    path = os.environ.get("AGENTWATCH_EVENTS") or os.path.join(
        os.path.expanduser("~"), ".agentwatch", "events.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        os.write(fd, line)
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # never break the Claude Code session
        pass
    sys.exit(0)
