#!/usr/bin/env python3
"""
sandbox_shell.py — shell command sandbox wrapper for Scryer agents.

Usage (drop-in shell replacement):
  SHELL=/path/to/sandbox_shell.py

When invoked as a shell, tmux/bash will call it as:
  sandbox_shell.py -c "command string"

Hard-blocked patterns (non-overridable):
  - rm -rf or destructive delete variants
  - git commands (any subcommand)
  - DELETE FROM (SQL)
  - Server-opening: python -m http.server, python3 -m http.server, nc -l, etc.

Additional per-task blacklist: SANDBOX_BLACKLIST env var, colon-separated regex patterns.

Blocked attempts are logged to SANDBOX_LOG_FILE (default: /tmp/scryer_sandbox.log).
SANDBOX_TICKET_ID env var is stamped on every log entry.
"""

import os
import re
import sys
import json
import subprocess
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Hard-blocked patterns (always enforced, cannot be overridden)
# ---------------------------------------------------------------------------

HARD_BLOCKS = [
    # Destructive deletes
    (r'\brm\s+(-\S*r\S*f|-\S*f\S*r)\b', "destructive rm (rm -rf / rm -fr)"),
    (r'\brm\s+--no-preserve-root\b', "destructive rm with --no-preserve-root"),
    (r'\bshred\b', "shred command"),
    (r'\bwipefs\b', "wipefs command"),

    # Git — all subcommands
    (r'\bgit\b', "git command"),

    # SQL destructive
    (r'(?i)\bDELETE\s+FROM\b', "DELETE FROM SQL statement"),
    (r'(?i)\bDROP\s+(TABLE|DATABASE|INDEX|VIEW)\b', "DROP SQL statement"),
    (r'(?i)\bTRUNCATE\s+TABLE\b', "TRUNCATE TABLE SQL statement"),

    # Opening network servers
    (r'\bpython3?\s+(-m\s+)?http\.server\b', "python http.server"),
    (r'\bnc\s+.*-l\b', "netcat listener"),
    (r'\bncat\s+.*-l\b', "ncat listener"),
    (r'\bsocat\b', "socat"),
    (r'\bsimple-?http-?server\b', "simple http server"),
    (r'\bhttp-server\b', "http-server npm"),

    # Fork bombs
    (r':\(\)\s*\{', "fork bomb pattern"),

    # Writing to /etc, /boot, /sys, /proc
    (r'\b(echo|tee|cat)\s+.*>\s*/etc/', "write to /etc"),
    (r'\b(echo|tee|cat)\s+.*>\s*/boot/', "write to /boot"),
    (r'\bchmod\s+.*777\b', "chmod 777"),
    (r'\bchown\s+.*root\b', "chown root"),
    (r'\bsudo\b', "sudo"),
]

_COMPILED_HARD = [(re.compile(pat), reason) for pat, reason in HARD_BLOCKS]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE = os.environ.get("SANDBOX_LOG_FILE", "/tmp/scryer_sandbox.log")
TICKET_ID = os.environ.get("SANDBOX_TICKET_ID", "")


def _log_blocked(command: str, reason: str) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ticket_id": TICKET_ID,
        "action": "blocked",
        "command": command,
        "reason": reason,
    }
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # Don't crash if we can't write the log


# ---------------------------------------------------------------------------
# Sandbox check
# ---------------------------------------------------------------------------

def _check_command(cmd: str) -> tuple[bool, str]:
    """
    Returns (allowed, reason). If allowed=False, reason is the block reason.
    """
    # Hard blocks
    for pattern, reason in _COMPILED_HARD:
        if pattern.search(cmd):
            return False, reason

    # Per-task blacklist from env (colon-separated regex strings)
    blacklist_env = os.environ.get("SANDBOX_BLACKLIST", "")
    if blacklist_env:
        for raw in blacklist_env.split(":"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                if re.search(raw, cmd):
                    return False, f"per-task blacklist: {raw}"
            except re.error:
                pass  # Ignore malformed patterns

    return True, ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]

    # Pass-through for interactive shell invocation (no -c)
    # We still need to exec a real shell so the terminal works.
    real_shell = os.environ.get("SANDBOX_REAL_SHELL", "/bin/bash")

    if not args:
        # Interactive mode — just exec the real shell
        os.execv(real_shell, [real_shell])

    if args[0] == "-c" and len(args) >= 2:
        command = args[1]
        allowed, reason = _check_command(command)

        if not allowed:
            _log_blocked(command, reason)
            print(f"\x1b[31m[sandbox] BLOCKED: {reason}\x1b[0m", file=sys.stderr)
            print(f"\x1b[33m  command: {command}\x1b[0m", file=sys.stderr)
            sys.exit(1)

        # Execute via real shell
        result = subprocess.run([real_shell, "-c", command])
        sys.exit(result.returncode)

    # Any other invocation (e.g. -i, --login) — forward to real shell
    os.execv(real_shell, [real_shell] + args)


if __name__ == "__main__":
    main()
