# Tmuxer — Handoff Document

_Last updated: 2026-03-18_

---

## What Tmuxer Is

A containerized API + UI for spawning and managing AI coding agents (Claude, Codex, Gemini) in isolated tmux sessions. The browser connects via WebSocket to a live PTY attached to the agent's tmux session. All three agents run inside a single Docker container alongside the API server.

---

## Current Architecture

```
nginx:80
  ├── /socket.io/  →  api:5678  (WebSocket)
  ├── /start, /agents, /sessions, /health  →  api:5678  (REST)
  └── /  →  ui:5679  (login page + terminal UI)

api container (Ubuntu 22.04)
  - FastAPI + python-socketio
  - Claude Code, Codex CLI, Gemini CLI (via NVM + Node 22)
  - tmux for session management

ui container (Ubuntu 22.04)
  - FastAPI + itsdangerous session cookies
  - Dark-themed login page (UI_USERNAME / UI_PASSWORD from .env)
  - Proxies API calls to api:5678
```

### Volume mounts (api container)
| Host | Container | Notes |
|---|---|---|
| `~/.claude` | `/root/.claude` | Claude auth + config |
| `~/.claude.json` | `/mnt/claude.json` | Copied to `/root/.claude.json` at startup via entrypoint.sh |
| `~/.codex` | `/root/.codex` | Codex auth + config |
| `~/.gemini` | `/root/.gemini` | Gemini auth + config |
| `~/Code/AgentWork` | `/agent-sessions` | Persistent agent session folders |
| `~/Code/AgentSkills` | `/skills` (ro) | Skills source dirs per agent |
| `infra/settings.local.json` | `/app/.claude/settings.local.json` (ro) | Claude project trust for /app |

---

## Session Isolation

Each agent spawn creates `/agent-sessions/<uuid>/` with:
- Per-agent native config file (`.claude/settings.local.json`, `.github/codex/home/config.toml`, `.gemini/settings.json`)
- `trustedDirectories: [<session_path>]`, `denyPaths: ['/app']` — agent cannot modify its own server code
- Skills copied from `/skills/<agent>/` into the agent-specific destination:
  - Claude → `.claude/commands/`
  - Codex → `.agents/skills/`
  - Gemini → `.gemini/skills/`

Caller can pass a UUID at spawn time — session topology (shared vs isolated) is the orchestrator's responsibility. Tmuxer does not generate UUIDs on behalf of callers unless none is provided.

---

## Configuration

`api/config.toml` — edit this file to change paths, default prompts, and default models. No code changes needed:

```toml
[paths]
agent_sessions_dir = "/agent-sessions"

[skills_source_dirs]
claude = "/skills/claude"
codex  = "/skills/codex"
gemini = "/skills/gemini"

[default_prompts]
claude = "use the message sender skill to start a conversation with the user. identify yourself including your model name"
codex  = "use the message sender skill to start a conversation with the user. identify yourself including your model name"
gemini = "use the message sender skill to start a conversation with the user. identify yourself including your model name"

[default_models]
claude = "claude-sonnet-4-6"
codex  = "gpt-5.4"
gemini = "gemini-2.5-pro"
```

`.env` (project root):
```
PORT=5678
UI_PORT=5679
TMUXER_API_KEY=...
TMUXER_JWT_SECRET=...
UI_USERNAME=admin
UI_PASSWORD=changeme
UI_SESSION_SECRET=replace-this-with-a-random-string
```

---

## Recent Changes (2026-03-18)

- **Default prompts and models moved to `config.toml`** — previously hardcoded in `server.py` as `DEFAULT_PROMPTS` and `DEFAULT_MODELS` dicts. Now read from `[default_prompts]` and `[default_models]` sections in `api/config.toml`. The `/agents` endpoint and UI modal pull from these values at runtime.
- **All agents now share the same default starting prompt** — agents are instructed to use the message sender skill and identify themselves by model name on startup.

---

## Known Issues / Unresolved

### 1. Non-root user in container (deferred)
All agents run as root. `--dangerously-skip-permissions` is blocked as root by Claude Code, so `auto_all` approval mode is unavailable. Default is `ask_all`. Non-root user attempted but PATH issues with NVM were not resolved.

### 2. API endpoints are unauthenticated
The API (port 5678) is only protected by nginx being the sole public entry point. On the internal Docker network, `api:5678` is open. Fine for now; needs auth if the network topology changes.

### 3. `AGENT_DIRS` / `SCRYER_ROOT` / `RESUME_COMMANDS` are stale
`server.py` still contains references to `~/Code/TestingAgents/` and `~/Code/plane.so` from before containerisation. These are legacy paths used by the old `spawn` and `resume` socket events. The new `_start_agent_session` path is clean. The old paths should be audited and removed or updated.

### 4. `api/` is COPYed not mounted as :ro volume
Currently the container bakes `api/` in at build time. The intent (noted in memory) is to mount it as `:ro` so code changes don't require a rebuild. Not yet done.

### 5. Gemini `task.md` not passed as CLI arg
`AGENT_CONTEXT_FILES` notes that Gemini's context file is `task.md` and "passed as CLI arg instead" — but this is not implemented. Gemini currently gets no auto-loaded context file on session start.

---

## Open Items / Next Steps

### Immediate
- [ ] Wire `relative_path` into `StartRequest` — caller passes e.g. `backend/auth` and agent starts in `/agent-sessions/<uuid>/backend/auth/`
- [ ] Wire caller-supplied UUID into `StartRequest` — orchestrators manage their own session IDs
- [ ] Fix Gemini `task.md` — pass as `--prompt_file` or equivalent CLI arg at spawn

### Near-term
- [ ] Git worktree integration — `StartRequest` accepts optional `repo_url`; server clones to `/repos/<name>/` (separate volume `~/Code/AgentRepos`) and runs `git worktree add /agent-sessions/<uuid>/` before spawning agent
- [ ] `~/Code/AgentRepos` volume — separate from AgentWork, holds bare/full clones as worktree sources
- [ ] Skill inline upload — `StartRequest` accepts skill content directly (base64 or multipart); orchestrator owns the skill library, tmuxer just writes files

### Longer-term / Out of scope for tmuxer
- Skill versioning (git-backed skill registry — orchestrator concern)
- Git frontend (branch/PR/merge UI — orchestrator concern)
- AgentHub-style DAG coordination — not needed for interactive single-agent use
- HTTPS/SSL termination on nginx
- Auth on internal API endpoints

---

## Rebuild

```bash
cd infra
./rebuild.sh          # incremental (uses layer cache)
./rebuild.sh --force  # full rebuild, no cache
```
