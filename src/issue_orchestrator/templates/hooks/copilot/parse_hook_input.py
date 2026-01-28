#!/usr/bin/env python3
"""
Extract the shell command from hook JSON input.

Reads JSON from stdin and prints the command string to stdout.

Supported formats:
- Copilot CLI: {"toolName": "bash", "toolArgs": "{\"command\": \"git push ...\"}"}
- Claude Code: {"tool_input": {"command": "git push ..."}}
- Cursor:      {"command": "git push ..."}

If parsing fails or the key is absent, prints an empty string.
"""

from __future__ import annotations

import json
import sys


def extract_command(raw: str) -> str:
    """Return the command string from hook JSON input."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""

    cmd = ""

    # Copilot CLI format: toolArgs is a JSON string
    tool_args = data.get("toolArgs")
    if isinstance(tool_args, str):
        try:
            args_data = json.loads(tool_args)
            if isinstance(args_data, dict):
                cmd = args_data.get("command", "")
        except (json.JSONDecodeError, TypeError):
            pass

    # Claude Code format fallback
    if not cmd:
        tool_input = data.get("tool_input")
        if isinstance(tool_input, dict):
            cmd = tool_input.get("command", "")

    # Cursor fallback
    if not cmd:
        cmd = data.get("command", "")

    return cmd if isinstance(cmd, str) else ""


def main() -> int:
    raw = sys.stdin.read()
    print(extract_command(raw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
