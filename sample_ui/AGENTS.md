# tmuxer — Agent Integration Guide

tmuxer is a server that manages tmux sessions and live terminal attach.
It exposes a REST API plus Socket.IO for realtime IO.

---

## Architecture

```
Orchestrator Agent
      │
      │  REST + Socket.IO
      ▼
 tmuxer server  :5678
      │
      ├── POST /start/with-command-in-path  →  creates tmux session  →  command runs inside
      ├── GET  /sessions        →  list all live sessions
      ├── GET  /sessions/{name} →  inspect a session
      ├── POST /sessions/{name}/input   →  send text/keys into the session
      ├── GET  /sessions/{name}/output  →  poll current screen contents
      ├── DELETE /sessions/{name}       →  kill the session
      └── /socket.io/                   →  live attach, input, resize, detach
```

A session is a tmux session with a command running inside it.
The caller starts sessions, polls their output to track progress,
sends input when needed, or attaches live over Socket.IO.

---

## REST API Reference

### Agent Defaults

```
GET /agents
```

Returns per-agent default paths, prompts, and the default permissions IR.
Useful for understanding what defaults will be applied before overriding them.

**Response:**
```json
{
  "claude": {
    "default_path": "/Users/you/Code/TestingAgents/Claude",
    "default_prompt": "read CLAUDE.md",
    "default_permissions": { ... }
  },
  "codex":  { ... },
  "gemini": { ... }
}
```

---

### Start a Session

```
POST /start/with-command-in-path
```

Starts a new tmux session, changes into the resolved common-volume path, and runs the given command.

The `path` must be a supported common-volume-relative path. Right now that means:

- `agent-sessions`
- `agent-sessions/<subpath>`

**Request body:**
```json
{
  "path": "agent-sessions",
  "command": "claude",
  "session_name": "example-session",
  "cols": 220,
  "rows": 50
}
```

`command` must not be empty.

**Response:**
```json
{
  "session": "example-session",
  "session_name": "example-session",
  "session_dir": "/agent-sessions",
  "alive": true
}
```

The `session` name is what you use in all subsequent calls.

---

### Live Attach

Realtime attach is not a raw `/ws/{name}` WebSocket route.

The current implementation uses Socket.IO on:

```
/socket.io/
```

Typical flow:

1. create or identify a tmux session over REST
2. connect a Socket.IO client to `http://localhost:5678`
3. emit `attach` with `session`, `cols`, and `rows`
4. receive `output`
5. emit `input`, `resize`, `detach`, or `list_sessions` as needed

---

### List Sessions

```
GET /sessions
```

Returns all live tmux sessions on the machine.

**Response:**
```json
[
  {
    "name": "claude-1741234567",
    "created": "06:21:03",
    "windows": 1,
    "attached": false
  }
]
```

---

### Inspect a Session

```
GET /sessions/{name}
```

Returns metadata for a specific session. Returns `404` if the session does not exist.
Use this to check whether an agent session is still alive.

**Response:**
```json
{
  "name": "claude-1741234567",
  "created": "06:21:03",
  "windows": 1,
  "attached": false,
  "alive": true
}
```

---

### Poll Output

```
GET /sessions/{name}/output?lines=200
```

Captures the current terminal contents of the session using `tmux capture-pane`.
This is the primary way an orchestrator tracks what an agent is doing.

- `lines` (optional, default 200): how many lines of scrollback to include.
- Returns plain text — the raw terminal output, ANSI codes stripped.

**Response:** `text/plain`
```
Reading AGENTS.md...
Found 3 tickets. Starting with ticket #1.
...
```

Poll this on a schedule (e.g. every 5–10 seconds) to track agent progress.
When the output stops changing between polls, the agent has likely finished or is waiting.

---

### Send Input

```
POST /sessions/{name}/input
```

Sends text (and optionally a key press) into the session via `tmux send-keys`.
Use this to answer prompts, send follow-up instructions, or submit a command.

**Request body:**
```json
{
  "text": "yes",
  "enter": true
}
```

- `text`: the text to type into the session.
- `enter` (default `true`): whether to follow with a Return keypress.

**Response:**
```json
{ "ok": true }
```

---

### Kill a Session

```
DELETE /sessions/{name}
```

Kills the tmux session and the agent process running inside it.

**Response:**
```json
{ "ok": true }
```

---

## Permissions IR

The `permissions` field uses a shared intermediate representation (IR) that gets
converted to each agent's native config format before launch.

### Full schema

```json
{
  "approval_mode": "ask_all",

  "filesystem": [
    { "path": "**",     "access": "write" },
    { "path": "~/.ssh", "access": "none"  }
  ],

  "shell": [
    { "pattern": "git *",     "allow": true  },
    { "pattern": "npm *",     "allow": true  },
    { "pattern": "python3 *", "allow": true  },
    { "pattern": "pip *",     "allow": true  },
    { "pattern": "ls *",      "allow": true  },
    { "pattern": "cat *",     "allow": true  },
    { "pattern": "rm *",      "allow": false }
  ],

  "network": {
    "enabled": true,
    "allowed_domains": [],
    "denied_domains":  []
  }
}
```

**Field reference:**

| Field | Type | Default | Description |
|---|---|---|---|
| `approval_mode` | string | `"ask_all"` | How aggressively the agent auto-approves actions |
| `filesystem` | array of `FSRule` | `[{"path":"**","access":"write"}]` | Ordered list of path rules |
| `filesystem[].path` | string | — | Glob or path. `**` = everything, `~/.ssh` = home-relative, `//etc` = absolute |
| `filesystem[].access` | string | — | `"read"`, `"write"`, or `"none"` |
| `shell` | array of `ShellRule` | see defaults | Ordered list of shell command rules |
| `shell[].pattern` | string | — | Command glob, e.g. `"git *"`, `"npm run *"` |
| `shell[].allow` | bool | — | `true` = allow, `false` = deny |
| `network.enabled` | bool | `true` | Whether web fetch/search tools are available |
| `network.allowed_domains` | string[] | `[]` | Allowlist of domains. Empty = all permitted |
| `network.denied_domains` | string[] | `[]` | Domains to block regardless of allowlist |

All fields are optional. Omitting `permissions` entirely applies `default_safe()`:
one write-all filesystem rule, the shell rules above, and network enabled.

---

### Native config conversion

| IR field | Claude | Codex | Gemini |
|---|---|---|---|
| `approval_mode` | `permissions.defaultMode` in `.claude/settings.json` | `approval_policy` in `.github/codex/home/config.toml` | `general.defaultApprovalMode` in `.gemini/settings.json` |
| `filesystem[]` | `Read/Edit/Write(path)` rules | `[permissions.default.filesystem]` table | `tools.allowed/exclude` read/write tools |
| `shell[]` | `Bash(pattern)` rules | informational (no per-command list in Codex) | `run_shell_command(prefix)` allow/exclude |
| `network` | `WebFetch`/`WebSearch` deny rules | `[permissions.default.network]` block | `web_fetch`/`web_search` exclude |

### Approval modes

| Value | Meaning |
|---|---|
| `ask_all` | Agent prompts before every tool use |
| `auto_edit` | Agent auto-approves file edits, asks for shell commands |
| `auto_all` | Agent approves everything without prompting |

### Filesystem access values

| Value | Meaning |
|---|---|
| `read` | Read-only |
| `write` | Read and write |
| `none` | Blocked entirely |

---

## Typical Orchestration Workflow

```
1.  POST /start/claude   { path, starting_prompt, permissions }
    → { session: "claude-1741234567" }

2.  Loop:
      GET /sessions/claude-1741234567/output
      → check if output contains a completion signal or has stopped changing

3.  If agent needs input:
      POST /sessions/claude-1741234567/input  { text: "...", enter: true }

4.  When done:
      DELETE /sessions/claude-1741234567
```

---

## Parallel Agent Pattern

An orchestrator can run multiple agents concurrently across different directories:

```
POST /start/claude  { path: "~/projects/backend",  starting_prompt: "implement auth" }
POST /start/codex   { path: "~/projects/frontend", starting_prompt: "implement login UI" }
POST /start/gemini  { path: "~/projects/docs",     starting_prompt: "write API docs" }

→ poll all three sessions concurrently
→ consolidate when all finish
```

---

## Detecting Completion

There is no explicit "done" signal from agents. Strategies for detecting completion:

1. **Output stability** — poll every N seconds; if the output hasn't changed for M consecutive
   polls, the agent has likely finished or is waiting.

2. **Sentinel strings** — instruct the agent in the starting prompt to print a known string
   (e.g. `DONE:`) when it finishes. Poll for that string.

3. **Session liveness** — `GET /sessions/{name}` returns `404` if the session was killed
   or exited naturally.

4. **File-based signalling** — instruct the agent to write a `done.json` or similar file
   to the workdir on completion. Poll for its existence via the filesystem or a separate
   endpoint.

The sentinel string approach is the most reliable for autonomous orchestration.

---

## Example: curl

All requests require either `X-API-Key` (orchestrator) or `Authorization: Bearer <token>` (agent).
Use `-k` to skip certificate verification if the mkcert CA is not yet installed.

```bash
export TMUXER_KEY="your-api-key-here"

# Start a Claude session (master key required)
curl -sk -X POST https://localhost:5678/start/claude \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $TMUXER_KEY" \
  -d '{
    "path": "~/Code/my-project",
    "starting_prompt": "Read AGENTS.md. When done, print DONE:",
    "permissions": { "approval_mode": "auto_edit" }
  }'

# Register (agent exchanges one-time code for a Bearer token — no auth header needed)
curl -sk -X POST https://localhost:5678/register \
  -H 'Content-Type: application/json' \
  -d '{ "code": "<one-time-code-from-prompt>" }'

# Poll output (master key or Bearer token)
curl -sk https://localhost:5678/sessions/claude-1741234567/output?lines=50 \
  -H "X-API-Key: $TMUXER_KEY"

# Send input (master key or Bearer token)
curl -sk -X POST https://localhost:5678/sessions/claude-1741234567/input \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $TMUXER_KEY" \
  -d '{ "text": "yes", "enter": true }'

# Kill (master key or Bearer token)
curl -sk -X DELETE https://localhost:5678/sessions/claude-1741234567 \
  -H "X-API-Key: $TMUXER_KEY"
```
