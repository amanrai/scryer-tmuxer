"""
permissions.py — Intermediate representation for agent permissions,
with converters to each agent's native config format.

IR → Claude:  .claude/settings.json
IR → Codex:   .codex/config.toml
IR → Gemini:  .gemini/settings.json
"""

from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel


# ── Enums ────────────────────────────────────────────────────────────────────

class ApprovalMode(str, Enum):
    """How aggressively the agent auto-approves actions."""
    ask_all    = "ask_all"    # prompt before every tool use
    auto_edit  = "auto_edit"  # auto-approve file edits, ask for shell commands
    auto_all   = "auto_all"   # approve everything without prompting


class FSAccess(str, Enum):
    read  = "read"   # read only
    write = "write"  # read + write
    none  = "none"   # blocked entirely


# ── IR components ─────────────────────────────────────────────────────────────

class FSRule(BaseModel):
    """A filesystem permission rule for a path or glob."""
    path: str           # e.g. "**", "./src/**", "~/.ssh", "//etc"
    access: FSAccess


class ShellRule(BaseModel):
    """An allow or deny rule for shell commands."""
    pattern: str        # e.g. "git *", "npm run *", "rm *"
    allow: bool         # True = allow, False = deny


class NetworkConfig(BaseModel):
    enabled: bool             = True
    allowed_domains: list[str] = []   # empty = all domains permitted
    denied_domains:  list[str] = []


class AgentPermissions(BaseModel):
    """
    Intermediate representation for agent permissions.
    All fields are optional / have safe defaults.
    """
    approval_mode: ApprovalMode        = ApprovalMode.ask_all
    filesystem:    list[FSRule]        = []
    shell:         list[ShellRule]     = []
    network:       NetworkConfig       = NetworkConfig()

    @classmethod
    def default_safe(cls) -> "AgentPermissions":
        """Sensible read-heavy, no-shell-auto defaults."""
        return cls(
            approval_mode=ApprovalMode.ask_all,
            filesystem=[
                FSRule(path="**", access=FSAccess.read),
            ],
            shell=[
                ShellRule(pattern="npm *",     allow=True),
                ShellRule(pattern="python3 *", allow=True),
                ShellRule(pattern="pip *",     allow=True),
                ShellRule(pattern="ls *",      allow=True),
                ShellRule(pattern="cat *",     allow=True),
                ShellRule(pattern="curl *",     allow=True),
                ShellRule(pattern="find *",    allow=True),
                ShellRule(pattern="grep *",    allow=True),
                ShellRule(pattern="rg *",      allow=True),
                ShellRule(pattern="rm *",      allow=False),
                ShellRule(pattern="ssh *",     allow=False),
                ShellRule(pattern="scp *",     allow=False),
            ],
            network=NetworkConfig(enabled=True),
        )

    @classmethod
    def auto_edit(cls) -> "AgentPermissions":
        """Auto-approve file edits, ask for shell."""
        p = cls.default_safe()
        p.approval_mode = ApprovalMode.auto_edit
        return p

    @classmethod
    def auto_all(cls) -> "AgentPermissions":
        """Fully autonomous — approve everything."""
        return cls(
            approval_mode=ApprovalMode.auto_all,
            filesystem=[FSRule(path="**", access=FSAccess.write)],
            network=NetworkConfig(enabled=True),
        )


# ── Claude converter ──────────────────────────────────────────────────────────

_CLAUDE_APPROVAL_MODE = {
    ApprovalMode.ask_all:   "default",
    ApprovalMode.auto_edit: "acceptEdits",
    ApprovalMode.auto_all:  "bypassPermissions",
}

def to_claude_settings(p: AgentPermissions) -> dict:
    """
    Convert AgentPermissions → .claude/settings.json dict.

    Claude tool specifier syntax:
      Read(path), Edit(path), Write(path), Bash(pattern)
    """
    allow: list[str] = []
    deny:  list[str] = []

    # Filesystem rules → Read / Edit / Write specifiers
    for rule in p.filesystem:
        path = rule.path
        if rule.access == FSAccess.none:
            deny += [f"Read({path})", f"Edit({path})", f"Write({path})"]
        elif rule.access == FSAccess.read:
            allow.append(f"Read({path})")
            deny  += [f"Edit({path})", f"Write({path})"]
        else:  # write (implies read)
            allow += [f"Read({path})", f"Edit({path})", f"Write({path})"]

    # Shell rules → Bash(pattern) specifiers
    for rule in p.shell:
        spec = f"Bash({rule.pattern})"
        if rule.allow:
            allow.append(spec)
        else:
            deny.append(spec)

    # Network
    if not p.network.enabled:
        deny.append("WebFetch")
        deny.append("WebSearch")

    settings: dict = {
        "permissions": {
            "defaultMode": _CLAUDE_APPROVAL_MODE[p.approval_mode],
        }
    }
    if allow:
        settings["permissions"]["allow"] = allow
    if deny:
        settings["permissions"]["deny"] = deny

    return settings


# ── Codex converter ───────────────────────────────────────────────────────────

_CODEX_APPROVAL_POLICY = {
    ApprovalMode.ask_all:   "untrusted",
    ApprovalMode.auto_edit: "on-request",
    ApprovalMode.auto_all:  "never",
}

_CODEX_SANDBOX_MODE = {
    ApprovalMode.ask_all:   "workspace-write",
    ApprovalMode.auto_edit: "workspace-write",
    ApprovalMode.auto_all:  "danger-full-access",
}

def to_codex_toml(p: AgentPermissions) -> str:
    """
    Convert AgentPermissions → .codex/config.toml string.

    Codex has no per-command allow/deny list; shell rules are noted
    as a comment for informational purposes only.
    """
    lines: list[str] = [
        f'approval_policy = "{_CODEX_APPROVAL_POLICY[p.approval_mode]}"',
        f'sandbox_mode    = "{_CODEX_SANDBOX_MODE[p.approval_mode]}"',
        "",
    ]

    # Filesystem: build a named profile
    lines += ["[permissions.default.filesystem]"]
    if p.filesystem:
        for rule in p.filesystem:
            path = rule.path
            # Map glob patterns to Codex special paths where possible
            if path in ("**", "./**", "./"):
                path = ":project_roots"
            access = rule.access.value
            lines.append(f'"{path}" = "{access}"')
    else:
        lines.append('":project_roots" = "write"')
    lines.append('":tmpdir"        = "write"')
    lines.append("")

    # Network
    lines += ["[permissions.default.network]"]
    lines.append(f"enabled = {'true' if p.network.enabled else 'false'}")
    if p.network.allowed_domains:
        domains = ", ".join(f'"{d}"' for d in p.network.allowed_domains)
        lines.append(f"allowed_domains = [{domains}]")
    if p.network.denied_domains:
        domains = ", ".join(f'"{d}"' for d in p.network.denied_domains)
        lines.append(f"denied_domains = [{domains}]")
    lines.append("")

    lines.append('default_permissions = "default"')

    # Shell rules are informational only in Codex
    if p.shell:
        lines.append("")
        lines.append("# Shell rules (informational — enforced via approval_policy, not per-command):")
        for rule in p.shell:
            verb = "allow" if rule.allow else "deny"
            lines.append(f"# {verb}: {rule.pattern}")

    return "\n".join(lines) + "\n"


# ── Gemini converter ──────────────────────────────────────────────────────────

_GEMINI_APPROVAL_MODE = {
    ApprovalMode.ask_all:   "default",
    ApprovalMode.auto_edit: "auto_edit",
    ApprovalMode.auto_all:  "default",   # yolo is CLI-flag-only; handled separately
}

# Gemini built-in tool names
_GEMINI_READ_TOOLS  = ["read_file", "read_many_files", "glob", "grep", "search_files"]
_GEMINI_WRITE_TOOLS = ["write_file", "create_directory", "move_file", "copy_file"]

def to_gemini_settings(p: AgentPermissions) -> dict:
    """
    Convert AgentPermissions → .gemini/settings.json dict.

    Gemini tool specifier syntax:
      "read_file", "write_file", "run_shell_command(prefix)"
    """
    allowed: list[str] = []
    excluded: list[str] = []

    # Filesystem rules → read/write tool lists
    for rule in p.filesystem:
        if rule.access == FSAccess.none:
            excluded += _GEMINI_READ_TOOLS + _GEMINI_WRITE_TOOLS
        elif rule.access == FSAccess.read:
            allowed  += _GEMINI_READ_TOOLS
            excluded += _GEMINI_WRITE_TOOLS
        else:  # write
            allowed += _GEMINI_READ_TOOLS + _GEMINI_WRITE_TOOLS

    # Shell rules → run_shell_command(prefix) allow/exclude
    for rule in p.shell:
        # Extract the command prefix (everything before the first space or *)
        prefix = rule.pattern.split()[0].rstrip("*").rstrip()
        spec = f"run_shell_command({prefix})" if prefix else "run_shell_command"
        if rule.allow:
            allowed.append(spec)
        else:
            excluded.append(spec)

    # Network
    if not p.network.enabled:
        excluded += ["web_fetch", "web_search"]

    settings: dict = {
        "general": {
            "defaultApprovalMode": _GEMINI_APPROVAL_MODE[p.approval_mode],
        },
        "tools": {},
    }

    # Deduplicate while preserving order
    if allowed:
        settings["tools"]["allowed"]  = list(dict.fromkeys(allowed))
    if excluded:
        settings["tools"]["exclude"]  = list(dict.fromkeys(excluded))

    # auto_all → yolo is CLI-only but we can note it in security
    if p.approval_mode == ApprovalMode.auto_all:
        settings["security"] = {"disableYoloMode": False}

    return settings


# ── Convenience: from raw dict ────────────────────────────────────────────────

def permissions_from_dict(d: dict) -> AgentPermissions:
    """Parse AgentPermissions from a raw API dict (e.g. from StartRequest)."""
    return AgentPermissions.model_validate(d) if d else AgentPermissions.default_safe()
