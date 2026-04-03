# scryer-tmuxer

Agent-first tmux session manager for Scryer.

The point of `scryer-tmuxer` is not to be a generic remote shell service. It exists to make agent execution reliable inside a container:

- start agent sessions in tmux
- keep those sessions attachable
- provide a stable API for launching, inspecting, and interacting with them
- manage the agent-specific trust configuration needed to let Claude, Codex, and Gemini operate inside mounted workspaces

## What It Is

`scryer-tmuxer` runs a container that bundles:

- tmux
- an API on port `5678`
- WebSocket terminal attach support
- installed agent CLIs
- a small UI/login layer for terminal access

The main use case is:

1. a caller prepares a workspace path
2. the caller tells tmuxer which agent to run and what command to start
3. tmuxer launches that command inside a tmux session
4. the caller or user can attach to the live session later

## Design Principle

This service is built for agents.

That means the important primitives are:

- trusted workspace paths
- predictable per-agent startup
- durable tmux sessions
- attach/read/input APIs

It is intentionally opinionated around agent workflows.

## The Convenience Escape Hatch

There is one deliberately general-purpose endpoint:

- `POST /start/with-command-in-path`

This endpoint exists as a convenience escape hatch.

It lets a caller:

- choose a common-volume-relative path
- choose an agent
- choose a session name
- provide an arbitrary startup command

So while tmuxer is built for agents, `POST /start/with-command-in-path` can be used to run essentially anything in the container, as long as the caller is operating inside a valid mounted path and understands the environment.

That is useful for:

- orchestrators
- hook runners
- scripted session startup
- transitional workflows while higher-level abstractions are still evolving

It should be treated as a convenience endpoint, not as the core product definition.

## Core API Surface

Main REST endpoints:

- `POST /start/with-command-in-path`
- `POST /trust-path`
- `POST /untrust-path`
- `GET /agents`
- `GET /sessions`
- `GET /sessions/{name}`
- `GET /sessions/{name}/output`
- `POST /sessions/{name}/input`
- `DELETE /sessions/{name}`

WebSocket support exists for live terminal attach and interactive session IO.

## Trust Model

Agent CLIs often maintain their own trusted-project configuration. Tmuxer exposes:

- `POST /trust-path`
- `POST /untrust-path`

These do not start or stop sessions.

They manage whether a workspace path is trusted for a given agent so that agent tooling can operate there without interactive trust prompts.

## Intended Caller Model

Tmuxer is best treated as infrastructure for a higher-level orchestrator.

The orchestrator should decide:

- which workspace path to use
- which session name to use
- which command to run
- whether sessions are shared or one-per-step

Tmuxer should stay focused on:

- session lifecycle
- trust management
- terminal IO

## Network Position

Tmuxer should live on an internal Docker/network boundary, not as a broadly exposed public API.

If a caller can reach tmuxer, it can:

- launch commands
- attach to sessions
- send input into sessions

That is powerful by design and should be treated accordingly.

## Notes

- For implementation details and current operational notes, see [HANDOFF.md](./HANDOFF.md).
