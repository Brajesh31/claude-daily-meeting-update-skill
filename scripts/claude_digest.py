#!/usr/bin/env python3
"""Summarize Claude Code JSONL sessions for a local standup digest.

Examples:
  python3 claude_digest.py --format json
  python3 claude_digest.py --date today --format markdown
  python3 claude_digest.py --project ~/work/api --format json

The script reads only local session files under ~/.claude/projects. It does not
send data to any network service. Review output before sharing it with others.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any


SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
]


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value)).astimezone()
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.astimezone()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def message_text(message: Any) -> str:
    if isinstance(message, str):
        raw = message
    elif isinstance(message, dict):
        content = message.get("content", "")
        if isinstance(content, str):
            raw = content
        elif isinstance(content, list):
            pieces = []
            for part in content:
                if isinstance(part, str):
                    pieces.append(part)
                elif isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}:
                    pieces.append(str(part.get("text", "")))
            raw = "\n".join(pieces)
        else:
            raw = ""
    else:
        raw = ""
    text = re.sub(r"\s+", " ", raw).strip()
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:500]


def role_of(record: dict[str, Any]) -> str:
    role = record.get("type")
    message = record.get("message")
    if role in {"user", "assistant"}:
        return str(role)
    if isinstance(message, dict) and message.get("role") in {"user", "assistant"}:
        return str(message["role"])
    return ""


def target_day(value: str) -> date:
    today = datetime.now().astimezone().date()
    if value == "today":
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be 'today', 'yesterday', or YYYY-MM-DD") from exc


def discover_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.glob("**/*.jsonl"))


def collect_sessions(root: Path, wanted_day: date, project_filter: str | None) -> list[dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    for path in discover_files(root):
        try:
            relative = str(path.parent)
            contents = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        records: list[tuple[datetime, str, str, dict[str, Any]]] = []
        for line in contents:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            stamp = parse_timestamp(record.get("timestamp") or record.get("createdAt"))
            if stamp is None or stamp.date() != wanted_day:
                continue
            role = role_of(record)
            text = message_text(record.get("message", record.get("content", "")))
            if role and text:
                records.append((stamp, role, text, record))

        if not records:
            continue

        cwd = next((str(r.get("cwd")) for _, _, _, r in records if r.get("cwd")), relative)
        if project_filter:
            target = str(Path(project_filter).expanduser()).lower()
            if target not in cwd.lower() and target not in relative.lower():
                continue

        session_id = next((str(r.get("sessionId")) for _, _, _, r in records if r.get("sessionId")), path.stem)
        key = session_id
        item = sessions.setdefault(key, {
            "session_id": session_id,
            "project": cwd,
            "session_file": str(path),
            "message_count": 0,
            "user_prompts": [],
            "assistant_responses": [],
            "started_at": min(x[0] for x in records).isoformat(),
            "last_activity": max(x[0] for x in records).isoformat(),
        })
        item["message_count"] += len(records)
        for _, role, text, _record in records:
            bucket = "user_prompts" if role == "user" else "assistant_responses"
            if text and text not in item[bucket] and len(item[bucket]) < 5:
                item[bucket].append(text)
        last = max(x[0] for x in records).isoformat()
        if last > item["last_activity"]:
            item["last_activity"] = last
    return sorted(sessions.values(), key=lambda x: x["last_activity"], reverse=True)


def render_markdown(day: date, sessions: list[dict[str, Any]]) -> str:
    lines = [f"# Claude Code Session Digest, {day.isoformat()}", ""]
    if not sessions:
        lines.append("No matching Claude Code sessions were found.")
        return "\n".join(lines)
    for session in sessions:
        lines += [
            f"## {session['project']}",
            f"- Session: {session['session_id']}",
            f"- Messages: {session['message_count']}",
            f"- Last activity: {session['last_activity']}",
        ]
        if session["user_prompts"]:
            lines.append("- User prompts:")
            lines.extend(f"  - {item}" for item in session["user_prompts"][:3])
        if session["assistant_responses"]:
            lines.append("- Assistant responses:")
            lines.extend(f"  - {item}" for item in session["assistant_responses"][:3])
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="yesterday", help="today, yesterday, or YYYY-MM-DD")
    parser.add_argument("--project", help="optional project path or substring filter")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--claude-home", type=Path, default=Path.home() / ".claude")
    args = parser.parse_args()

    try:
        day = target_day(args.date)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    root = args.claude_home.expanduser() / "projects"
    sessions = collect_sessions(root, day, args.project)
    if args.format == "json":
        print(json.dumps({"date": day.isoformat(), "sessions": sessions}, indent=2))
    else:
        print(render_markdown(day, sessions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
