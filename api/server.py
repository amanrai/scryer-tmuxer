import os
import re
import pty
import fcntl
import struct
import termios
import subprocess
import threading
import select
import time
import shlex
import shutil
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any
import json
import socketio

from permissions import (
    ApprovalMode,
    permissions_from_dict,
    to_claude_settings,
    to_codex_toml,
    to_gemini_settings,
)

TMUX = shutil.which('tmux') or '/usr/bin/tmux'

_NVM_INIT = 'export NVM_DIR="$HOME/.nvm" && [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"'

AGENT_COMMANDS = {
    'claude': 'unset CLAUDECODE && claude',
    'codex':  f'{_NVM_INIT} && nvm use 22 && codex',
    'gemini': f'{_NVM_INIT} && nvm use 22 && gemini',
}

TRIGGER = 'Execute the task.'

SANDBOX_SHELL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sandbox_shell.py')
SANDBOX_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sandbox.log')

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')

# socket_id -> {master, proc, session_name, owned}
sessions = {}

_loop: asyncio.AbstractEventLoop = None

_ANSI_ESCAPE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub('', text)


def _emit_from_thread(event, data, room=None):
    """Thread-safe emit: schedules a coroutine on the main event loop."""
    if _loop:
        asyncio.run_coroutine_threadsafe(sio.emit(event, data, room=room), _loop)


def _read_loop(sid, master_fd):
    while True:
        try:
            r, _, _ = select.select([master_fd], [], [], 1.0)
        except (ValueError, OSError):
            break
        if not r:
            continue
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            break
        if not data:
            break
        text = data.decode('utf-8', errors='replace')
        _emit_from_thread('output', text, room=sid)

        # Write to conversation log if this session has one
        state = sessions.get(sid)
        if state and state.get('conv_log_path'):
            try:
                with open(state['conv_log_path'], 'a', encoding='utf-8') as f:
                    f.write(_strip_ansi(text))
            except OSError:
                pass

    # Only notify the client if this was a natural session death.
    # If _detach() was called first it already popped the sid, so pop returns None here.
    if sessions.pop(sid, None) is not None:
        _emit_from_thread('session_ended', {}, room=sid)


def _preflight_check():
    """Check required system dependencies. Returns dict of name -> bool."""
    results = {}

    # Direct path check for tmux
    results['tmux'] = os.path.isfile(TMUX)

    # which-based checks for system tools
    for name in ('docker', 'git'):
        results[name] = shutil.which(name) is not None

    # Agent CLIs may live under nvm shims — use a login shell to find them
    for name in ('claude', 'codex', 'gemini'):
        r = subprocess.run(
            ['bash', '-lc', f'command -v {name}'],
            capture_output=True,
        )
        results[name] = (r.returncode == 0)

    return results


def _list_tmux_sessions():
    result = subprocess.run(
        [TMUX, 'list-sessions', '-F',
         '#{session_name}\t#{session_created}\t#{session_windows}\t#{session_attached}'],
        capture_output=True, text=True
    )
    sessions_list = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split('\t')
        if len(parts) != 4:
            continue
        name, created_epoch, windows, attached = parts
        try:
            created = time.strftime('%H:%M:%S', time.localtime(int(created_epoch)))
        except (ValueError, OSError):
            created = '?'
        sessions_list.append({
            'name': name,
            'created': created,
            'windows': int(windows),
            'attached': attached == '1',
        })
    return sessions_list


def _attach_pty(sid, session_name, owned, cols, rows):
    """Open a PTY, attach tmux to it, start the read loop."""
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, rows, cols)

    proc = subprocess.Popen(
        [TMUX, 'attach-session', '-t', session_name],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        env={**os.environ, 'TERM': 'xterm-256color'},
    )
    os.close(slave_fd)

    sessions[sid] = {
        'master': master_fd,
        'proc': proc,
        'session_name': session_name,
        'owned': owned,
    }

    t = threading.Thread(target=_read_loop, args=(sid, master_fd), daemon=True)
    t.start()


try:
    import tomllib
except ImportError:
    import tomli as tomllib  # pip install tomli for Python < 3.11

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.toml')
with open(_CONFIG_PATH, 'rb') as _f:
    _CFG = tomllib.load(_f)

AGENT_SESSIONS_DIR = _CFG['paths']['agent_sessions_dir']
AGENT_SKILLS_DIRS  = _CFG['skills_source_dirs']
DEFAULT_PROMPTS    = dict(_CFG.get('default_prompts', {}))
DEFAULT_MODELS     = dict(_CFG.get('default_models', {}))

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_event_loop()
    os.makedirs(AGENT_SESSIONS_DIR, exist_ok=True)
    yield


app = FastAPI(
    lifespan=lifespan,
    title='tmuxer',
    version='1.0.0',
    description='REST API for managing tmux sessions running AI agents.',
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)
templates = Jinja2Templates(directory='templates')
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)


@app.get('/')
async def index(request: Request):
    return templates.TemplateResponse('index.html', {'request': request})


@sio.event
async def connect(sid, environ):
    await sio.emit('session_list', _list_tmux_sessions(), room=sid)
    await sio.emit('preflight_result', _preflight_check(), room=sid)


@sio.event
async def preflight(sid):
    await sio.emit('preflight_result', _preflight_check(), room=sid)


@sio.event
async def disconnect(sid):
    _cleanup(sid)


@sio.event
async def list_sessions(sid):
    await sio.emit('session_list', _list_tmux_sessions(), room=sid)


@sio.event
async def new_session(sid, data):
    session_name = data.get('name') or f'session-{sid[:8]}'
    cols = data.get('cols', 220)
    rows = data.get('rows', 50)

    _detach(sid)
    subprocess.run([TMUX, 'kill-session', '-t', session_name], capture_output=True)
    subprocess.run(
        [TMUX, 'new-session', '-d', '-s', session_name, '-x', str(cols), '-y', str(rows)],
        check=True,
    )
    _attach_pty(sid, session_name, owned=True, cols=cols, rows=rows)
    await sio.emit('attached', {'session': session_name}, room=sid)


@sio.event
async def resume(sid, data):
    agent = data.get('agent', 'claude')
    cols = data.get('cols', 220)
    rows = data.get('rows', 50)
    workdir = data.get('workdir') or None
    if not workdir:
        await sio.emit('workdir_error', {
            'message': (
                'Could not resolve the planning folder for this project. '
                'Make sure Scryer root is configured (⚙ Global Config) and the project folder exists.'
            )
        }, room=sid)
        return
    fresh = data.get('fresh', False)
    startup_input = data.get('startup_input', '')

    # When starting a fresh session, strip all Claude Code session env vars so the
    # new process doesn't inherit and attach to whatever session spawned this server.
    _CLAUDE_UNSET = (
        'unset CLAUDECODE CLAUDE_CODE_SESSION_ID CLAUDE_CODE_ENTRYPOINT '
        'CLAUDE_CODE_IS_NESTED CLAUDE_CODE_SKIP_TELEMETRY 2>/dev/null'
    )

    # Determine whether a previous conversation exists in the workdir to continue
    has_prior_session = os.path.isdir(os.path.join(workdir, '.claude'))

    if agent == 'codex':
        cmd_suffix = f'{_NVM_INIT} && nvm use 22 && codex' + ('' if fresh else (' --continue' if has_prior_session else ''))
    elif agent == 'gemini':
        cmd_suffix = f'{_NVM_INIT} && nvm use 22 && gemini' + ('' if fresh else (' --continue' if has_prior_session else ''))
    else:
        if fresh or not has_prior_session:
            cmd_suffix = f'{_CLAUDE_UNSET} && claude'
        else:
            cmd_suffix = 'unset CLAUDECODE && claude --continue'
    command = f'cd {shlex.quote(workdir)} && {cmd_suffix}'

    session_name = f'resume-{agent}-{sid[:8]}'

    _detach(sid)
    subprocess.run([TMUX, 'kill-session', '-t', session_name], capture_output=True)
    subprocess.run(
        [TMUX, 'new-session', '-d', '-s', session_name, '-x', str(cols), '-y', str(rows)],
        check=True,
    )
    subprocess.run([TMUX, 'send-keys', '-t', session_name, command, 'Enter'], check=True)
    await asyncio.sleep(0.5)

    _attach_pty(sid, session_name, owned=True, cols=cols, rows=rows)
    await sio.emit('attached', {'session': session_name}, room=sid)

    # Inject session_id and scope into the startup_input so the agent knows its session context
    if fresh and startup_input:
        scope_type = data.get('scope_type', '')   # e.g. 'project', 'subproject', 'ticket'
        scope_id   = data.get('scope_id', '')     # numeric entity ID as string
        if scope_type and scope_id:
            scope_note = (
                f'\n\nSession ID: {session_name}\n'
                f'Scope: {scope_type} {scope_id}\n'
                f'Call register_scope(session_id="{session_name}", entity_type="{scope_type}", entity_id={scope_id}) '
                f'as your first MCP call to activate write scoping.'
            )
            startup_input = startup_input + scope_note

    # Set up conversation log file in {workdir}/.planning/conversations/{timestamp}.md
    try:
        conv_dir = os.path.join(workdir, '.planning', 'conversations')
        os.makedirs(conv_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        conv_log_path = os.path.join(conv_dir, f'{ts}.md')
        # Write header
        with open(conv_log_path, 'w', encoding='utf-8') as f:
            f.write(f'# Planning session — {agent} — {ts}\n\n')
        if sid in sessions:
            sessions[sid]['conv_log_path'] = conv_log_path
    except OSError:
        pass  # Non-fatal — logging is best-effort

    if fresh and startup_input:
        def _send_startup():
            time.sleep(3)
            subprocess.run(
                [TMUX, 'send-keys', '-t', session_name, startup_input, 'Enter'],
                capture_output=True,
            )
        threading.Thread(target=_send_startup, daemon=True).start()


@sio.event
async def attach(sid, data):
    session_name = data.get('session')
    cols = data.get('cols', 220)
    rows = data.get('rows', 50)

    if not session_name:
        return

    # Verify session exists before attaching; emit error event if not
    check = subprocess.run([TMUX, 'has-session', '-t', session_name], capture_output=True)
    if check.returncode != 0:
        await sio.emit('session_not_found', {'session': session_name}, room=sid)
        return

    _detach(sid)
    _attach_pty(sid, session_name, owned=False, cols=cols, rows=rows)
    await sio.emit('attached', {'session': session_name}, room=sid)


@sio.event
async def detach(sid):
    _detach(sid)
    await sio.emit('detached', {}, room=sid)
    await sio.emit('session_list', _list_tmux_sessions(), room=sid)


@sio.event
async def kill_session(sid, data):
    session_name = data.get('session')
    if session_name:
        subprocess.run([TMUX, 'kill-session', '-t', session_name], capture_output=True)
    await sio.emit('session_list', _list_tmux_sessions(), room=sid)


@sio.event
async def input(sid, data):
    state = sessions.get(sid)
    if state:
        try:
            os.write(state['master'], data.encode('utf-8'))
        except OSError:
            pass


@sio.event
async def resize(sid, data):
    state = sessions.get(sid)
    if not state:
        return
    cols = data.get('cols', 80)
    rows = data.get('rows', 24)
    _set_winsize(state['master'], rows, cols)
    subprocess.run(
        [TMUX, 'resize-window', '-t', state['session_name'],
         '-x', str(cols), '-y', str(rows)],
        capture_output=True
    )


def _set_winsize(fd, rows, cols):
    winsize = struct.pack('HHHH', rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _detach(sid):
    """Close PTY and terminate attach process, but leave tmux session alive."""
    state = sessions.pop(sid, None)
    if not state:
        return
    try:
        os.close(state['master'])
    except OSError:
        pass
    try:
        state['proc'].terminate()
    except Exception:
        pass


def _cleanup(sid):
    """Detach PTY on disconnect. Tmux sessions are never auto-killed — agents run independently of browser connections."""
    _detach(sid)


# ── REST API ──────────────────────────────────────────────────────────────────

KNOWN_AGENTS = set(AGENT_COMMANDS.keys())


class StartRequest(BaseModel):
    permissions: dict[str, Any] = {}
    starting_prompt: str = ''
    prompt_delay: int = 5   # seconds to wait before sending the starting prompt
    model: str = ''         # optional model override, e.g. "claude-opus-4-5", "o4-mini"


class StartWithCommandInPathRequest(BaseModel):
    path: str
    command: str
    agent: str = ''
    session_name: str = ''
    cols: int = 220
    rows: int = 50


class TrustPathRequest(BaseModel):
    agent: str
    path: str


def _resolve_common_volume_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Path must not be empty")

    normalized = raw.lstrip("/")
    if normalized.startswith("agent-sessions/") or normalized == "agent-sessions":
        suffix = normalized.removeprefix("agent-sessions").lstrip("/")
        return os.path.join(AGENT_SESSIONS_DIR, suffix)

    raise HTTPException(
        status_code=400,
        detail=f"Path '{raw}' is not a supported common-volume-relative path",
    )



@app.get(
    '/agents',
    summary='List supported agents and defaults',
    description=(
        'Returns the static agent defaults used by the UI, including default prompt, '
        'default model, and default safe permissions. This is configuration metadata, '
        'not live workflow or tmux session state.'
    ),
)
async def get_agents():
    """Return per-agent defaults for the UI."""
    from permissions import AgentPermissions
    defaults = AgentPermissions.default_safe()
    return {
        agent: {
            'default_prompt':      DEFAULT_PROMPTS.get(agent, ''),
            'default_model':       DEFAULT_MODELS.get(agent, ''),
            'default_permissions': defaults.model_dump(),
        }
        for agent in KNOWN_AGENTS
    }


def _trust_path(agent: str, path: str) -> None:
    """Add path to the agent's global trusted-projects config."""
    if agent == 'claude':
        claude_json = os.path.expanduser('~/.claude.json')
        try:
            data = json.loads(open(claude_json).read()) if os.path.exists(claude_json) else {}
            projects = data.setdefault('projects', {})
            entry = projects.setdefault(path, {})
            entry['hasTrustDialogAccepted'] = True
            tmp = claude_json + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, claude_json)
        except Exception:
            pass

    elif agent == 'codex':
        codex_config = os.path.expanduser('~/.codex/config.toml')
        header = f'[projects."{path}"]'
        try:
            os.makedirs(os.path.dirname(codex_config), exist_ok=True)
            if os.path.exists(codex_config):
                with open(codex_config, 'r', encoding='utf-8') as f:
                    content = f.read()
            else:
                content = ''

            lines = content.splitlines()
            kept: list[str] = []
            in_target_block = False

            # Remove all existing blocks for this exact project path so we can
            # write one canonical trusted entry without duplicate TOML tables.
            for line in lines:
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    in_target_block = (stripped == header)
                    if in_target_block:
                        continue
                if not in_target_block:
                    kept.append(line)

            new_content = '\n'.join(kept).rstrip()
            block = f'{header}\ntrust_level = "trusted"\n'
            if new_content:
                new_content = f'{new_content}\n\n{block}'
            else:
                new_content = block

            tmp = codex_config + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(new_content)
            os.replace(tmp, codex_config)
        except Exception:
            pass

    elif agent == 'gemini':
        trusted = os.path.expanduser('~/.gemini/trustedFolders.json')
        try:
            data = json.loads(open(trusted).read()) if os.path.exists(trusted) else {}
            data[path] = 'TRUST_FOLDER'
            with open(trusted, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass


def _untrust_path(agent: str, path: str) -> None:
    """Remove path from the agent's global trusted-projects config."""
    if agent == 'claude':
        claude_json = os.path.expanduser('~/.claude.json')
        try:
            data = json.loads(open(claude_json).read()) if os.path.exists(claude_json) else {}
            projects = data.get('projects', {})
            if path in projects:
                del projects[path]
            data['projects'] = projects
            tmp = claude_json + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, claude_json)
        except Exception:
            pass

    elif agent == 'codex':
        codex_config = os.path.expanduser('~/.codex/config.toml')
        header = f'[projects."{path}"]'
        try:
            if os.path.exists(codex_config):
                with open(codex_config, 'r', encoding='utf-8') as f:
                    content = f.read()
            else:
                content = ''

            lines = content.splitlines()
            kept: list[str] = []
            in_target_block = False
            for line in lines:
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    in_target_block = (stripped == header)
                    if in_target_block:
                        continue
                if not in_target_block:
                    kept.append(line)

            new_content = '\n'.join(kept).rstrip()
            tmp = codex_config + '.tmp'
            os.makedirs(os.path.dirname(codex_config), exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(f"{new_content}\n" if new_content else "")
            os.replace(tmp, codex_config)
        except Exception:
            pass

    elif agent == 'gemini':
        trusted = os.path.expanduser('~/.gemini/trustedFolders.json')
        try:
            data = json.loads(open(trusted).read()) if os.path.exists(trusted) else {}
            if path in data:
                del data[path]
            with open(trusted, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass


def _start_agent_session(agent: str, permissions: dict[str, Any], starting_prompt: str = '', prompt_delay: int = 5, model: str = '') -> str:
    """
    Create a detached tmux session running the given agent in a new /agent-sessions/<id> folder.
    Returns the session name. No PTY is attached here — the browser
    connects via the socket `attach` event after getting the session name.
    """
    import uuid
    if agent not in KNOWN_AGENTS:
        raise HTTPException(status_code=400, detail=f"Unknown agent '{agent}'. Choose from: {sorted(KNOWN_AGENTS)}")

    session_id = str(uuid.uuid4())
    expanded = os.path.join(AGENT_SESSIONS_DIR, session_id)
    os.makedirs(expanded, exist_ok=True)

    # Copy skills into the session folder
    _SKILLS_TARGETS = {
        'claude': '.claude/skills',
        'codex':  '.agents/skills',
        'gemini': '.gemini/skills',
    }
    skills_src = AGENT_SKILLS_DIRS.get(agent)
    if skills_src and os.path.isdir(skills_src):
        skills_dst = os.path.join(expanded, _SKILLS_TARGETS[agent])
        os.makedirs(skills_dst, exist_ok=True)
        shutil.copytree(skills_src, skills_dst, dirs_exist_ok=True)

    base_command = AGENT_COMMANDS[agent]
    if model:
        if agent == 'gemini':
            base_command = f'{base_command} --model={shlex.quote(model)}'
        else:
            base_command = f'{base_command} --model {shlex.quote(model)}'

    # Parse the permissions dict into our IR and write the agent's native config file.
    perms = permissions_from_dict(permissions)

    if agent == 'claude':
        _trust_path(agent, expanded)
        config_dir = os.path.join(expanded, '.claude')
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, 'settings.local.json')
        settings = to_claude_settings(perms)
        settings['trustedDirectories'] = [expanded]
        settings['denyPaths'] = ['/app']
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)

    elif agent == 'codex':
        _trust_path(agent, expanded)
        config_dir = os.path.join(expanded, '.codex')
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, 'config.toml')
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(to_codex_toml(perms))

    elif agent == 'gemini':
        _trust_path(agent, expanded)
        config_dir = os.path.join(expanded, '.gemini')
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, 'settings.json')
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(to_gemini_settings(perms), f, indent=2)

    ts = int(time.time())
    session_name = f'{agent}-{ts}'

    sandbox_env = []
    if os.path.isfile(SANDBOX_SHELL):
        sandbox_env = [
            '-e', f'SHELL={SANDBOX_SHELL}',
            '-e', 'SANDBOX_REAL_SHELL=/bin/bash',
            '-e', f'SANDBOX_LOG_FILE={SANDBOX_LOG_FILE}',
        ]

    subprocess.run([TMUX, 'kill-session', '-t', session_name], capture_output=True)
    subprocess.run(
        [TMUX, 'new-session', '-d', '-s', session_name, '-x', '220', '-y', '50'] + sandbox_env,
        check=True,
    )
    # Write prompt to prompt.txt; always passed via $(cat prompt.txt) to avoid quoting issues.
    prompt_file = os.path.join(expanded, 'prompt.txt')
    with open(prompt_file, 'w', encoding='utf-8') as f:
        f.write(starting_prompt or '')

    if agent == 'gemini':
        base_command = f'{base_command} --prompt-interactive="$(cat prompt.txt)"'
    else:
        base_command = f'{base_command} "$(cat prompt.txt)"'

    command = f'cd {shlex.quote(expanded)} && {base_command}'
    subprocess.run([TMUX, 'send-keys', '-t', session_name, command, 'Enter'], check=True)

    return session_name


async def start_agent(agent: str, body: StartRequest):
    """Legacy helper retained for reference. Endpoint removed."""
    session_name = _start_agent_session(agent, body.permissions, body.starting_prompt, body.prompt_delay, body.model)
    return {'session': session_name, 'agent': agent}


async def start_claude(body: StartRequest):
    """Legacy helper retained for reference. Endpoint removed."""
    session_name = _start_agent_session('claude', body.permissions, body.starting_prompt, body.prompt_delay, body.model)
    return {'session': session_name, 'agent': 'claude'}


async def start_codex(body: StartRequest):
    """Legacy helper retained for reference. Endpoint removed."""
    session_name = _start_agent_session('codex', body.permissions, body.starting_prompt, body.prompt_delay, body.model)
    return {'session': session_name, 'agent': 'codex'}


async def start_gemini(body: StartRequest):
    """Legacy helper retained for reference. Endpoint removed."""
    session_name = _start_agent_session('gemini', body.permissions, body.starting_prompt, body.prompt_delay, body.model)
    return {'session': session_name, 'agent': 'gemini'}


@app.post('/start/with-command-in-path')
async def start_with_command_in_path(body: StartWithCommandInPathRequest):
    session_dir = os.path.abspath(_resolve_common_volume_path(body.path))
    if not os.path.isdir(session_dir):
        raise HTTPException(status_code=400, detail=f"Path '{session_dir}' does not exist or is not a directory")
    if not body.command.strip():
        raise HTTPException(status_code=400, detail="Command must not be empty")
    if body.agent and body.agent not in KNOWN_AGENTS:
        raise HTTPException(status_code=400, detail=f"Unknown agent '{body.agent}'. Choose from: {sorted(KNOWN_AGENTS)}")

    session_name = body.session_name.strip() or f"session-{int(time.time())}"

    sandbox_env = []
    if os.path.isfile(SANDBOX_SHELL):
        sandbox_env = [
            '-e', f'SHELL={SANDBOX_SHELL}',
            '-e', 'SANDBOX_REAL_SHELL=/bin/bash',
            '-e', f'SANDBOX_LOG_FILE={SANDBOX_LOG_FILE}',
        ]

    subprocess.run([TMUX, 'kill-session', '-t', session_name], capture_output=True)
    subprocess.run(
        [TMUX, 'new-session', '-d', '-s', session_name, '-x', str(body.cols), '-y', str(body.rows)] + sandbox_env,
        check=True,
    )

    command = f'cd {shlex.quote(session_dir)} && {body.command}'
    subprocess.run([TMUX, 'send-keys', '-t', session_name, command, 'Enter'], check=True)

    return {
        'session': session_name,
        'session_name': session_name,
        'session_dir': session_dir,
        'alive': True,
    }


@app.post(
    '/trust-path',
    summary='Trust a workspace path for an agent',
    description=(
        'Marks the resolved workspace path as trusted for the specified agent. '
        'This is used before launching work in a session so agent CLIs can operate '
        'inside that directory without interactive trust prompts.'
    ),
)
async def trust_path(body: TrustPathRequest):
    if body.agent not in KNOWN_AGENTS:
        raise HTTPException(status_code=400, detail=f"Unknown agent '{body.agent}'. Choose from: {sorted(KNOWN_AGENTS)}")
    session_dir = os.path.abspath(_resolve_common_volume_path(body.path))
    if not os.path.isdir(session_dir):
        raise HTTPException(status_code=400, detail=f"Path '{session_dir}' does not exist or is not a directory")
    _trust_path(body.agent, session_dir)
    return {
        'ok': True,
        'agent': body.agent,
        'session_dir': session_dir,
    }


@app.post(
    '/untrust-path',
    summary='Remove trust for a workspace path',
    description=(
        'Removes the resolved workspace path from the specified agent trust configuration. '
        'This is cleanup for paths that were previously trusted through `/trust-path`; '
        'it does not stop or delete tmux sessions.'
    ),
)
async def untrust_path(body: TrustPathRequest):
    if body.agent not in KNOWN_AGENTS:
        raise HTTPException(status_code=400, detail=f"Unknown agent '{body.agent}'. Choose from: {sorted(KNOWN_AGENTS)}")
    session_dir = os.path.abspath(_resolve_common_volume_path(body.path))
    _untrust_path(body.agent, session_dir)
    return {
        'ok': True,
        'agent': body.agent,
        'session_dir': session_dir,
    }



# ── Session management REST endpoints ─────────────────────────────────────────

@app.get('/sessions')
async def list_sessions_rest():
    """List all live tmux sessions."""
    return _list_tmux_sessions()


@app.get('/sessions/{name}')
async def get_session(name: str):
    """Inspect a specific session. Returns 404 if it does not exist."""
    check = subprocess.run([TMUX, 'has-session', '-t', name], capture_output=True)
    if check.returncode != 0:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")

    result = subprocess.run(
        [TMUX, 'list-sessions', '-F',
         '#{session_name}\t#{session_created}\t#{session_windows}\t#{session_attached}',
         '-f', f'#{{==:#{{session_name}},{name}}}'],
        capture_output=True, text=True,
    )
    for line in result.stdout.strip().splitlines():
        parts = line.split('\t')
        if len(parts) == 4:
            sname, created_epoch, windows, attached = parts
            try:
                created = time.strftime('%H:%M:%S', time.localtime(int(created_epoch)))
            except (ValueError, OSError):
                created = '?'
            return {
                'name': sname,
                'created': created,
                'windows': int(windows),
                'attached': attached == '1',
                'alive': True,
            }
    # has-session said it exists but list-sessions missed it — still alive
    return {'name': name, 'alive': True}


@app.get('/sessions/{name}/output')
async def get_session_output(name: str, lines: int = 200):
    """
    Capture current terminal contents via tmux capture-pane.
    Returns plain text with ANSI codes stripped.
    """
    check = subprocess.run([TMUX, 'has-session', '-t', name], capture_output=True)
    if check.returncode != 0:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")

    result = subprocess.run(
        [TMUX, 'capture-pane', '-p', '-t', name, '-S', str(-lines)],
        capture_output=True, text=True,
    )
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(_strip_ansi(result.stdout))


class InputRequest(BaseModel):
    text: str
    enter: bool = True


@app.post('/sessions/{name}/input')
async def send_session_input(name: str, body: InputRequest):
    """Send text (and optionally Enter) into a tmux session."""
    check = subprocess.run([TMUX, 'has-session', '-t', name], capture_output=True)
    if check.returncode != 0:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")

    keys = [TMUX, 'send-keys', '-t', name, body.text]
    if body.enter:
        keys.append('Enter')
    subprocess.run(keys, check=True)
    return {'ok': True}


@app.delete('/sessions/{name}')
async def delete_session(name: str):
    """Kill a tmux session."""
    subprocess.run([TMUX, 'kill-session', '-t', name], capture_output=True)
    return {'ok': True}


# ── Orchestrated dispatch ─────────────────────────────────────────────────────


class OrchestratedStartRequest(BaseModel):
    process_id: str
    phase: int
    step: str  # skill name
    agent: str = 'claude'
    model: str = ''
    task_title: str = ''
    task_description: str = ''
    workflow_name: str = ''
    project_name: str = ''
    notification_channel: str = 'agent:notifications'
    permissions: dict[str, Any] = {}


LIFECYCLE_SKILL_TEMPLATE = """---
name: {name}
description: {description}
argument-hint: ""
allowed-tools: []
---

{body}
"""

STARTING_TASK_BODY = """# Starting Task

Call this skill FIRST before doing any work. It notifies the orchestrator that you have begun.

When you invoke this skill, simply confirm you have read and understood the task, then proceed with your work.
"""

FINISHING_TASK_BODY = """# Finishing Task

Call this skill LAST after all your work is complete. It notifies the orchestrator that you are done.

When you invoke this skill, briefly summarize what you accomplished.
"""

RFI_BODY = """# Request For Information

Call this skill when you are blocked and cannot proceed without additional input or clarification.

Describe clearly what you need and why you are blocked.
"""


def _write_lifecycle_skills(session_dir: str, agent: str, process_id: str, phase: int, step: str, notification_channel: str):
    """Write lifecycle skill files that post notifications to Valkey via the interaction service."""
    _SKILLS_TARGETS = {
        'claude': '.claude/skills',
        'codex':  '.agents/skills',
        'gemini': '.gemini/skills',
    }
    skills_dir = os.path.join(session_dir, _SKILLS_TARGETS[agent])
    os.makedirs(skills_dir, exist_ok=True)

    lifecycle_skills = {
        'starting-task': ('Signal that you are starting work', STARTING_TASK_BODY, 'start'),
        'finishing-task': ('Signal that you have finished work', FINISHING_TASK_BODY, 'done'),
        'rfi': ('Request additional information when blocked', RFI_BODY, 'rfi'),
    }

    for skill_name, (description, body, event_type) in lifecycle_skills.items():
        content = LIFECYCLE_SKILL_TEMPLATE.format(
            name=skill_name,
            description=description,
            body=body,
        )
        skill_path = os.path.join(skills_dir, f'{skill_name}.md')
        with open(skill_path, 'w', encoding='utf-8') as f:
            f.write(content)

    # Read agents.md template from common volume and write with substitutions
    templates_dir = os.environ.get('TEMPLATES_DIR', '/templates')
    template_path = os.path.join(templates_dir, 'agents.md')
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    agents_md = template.format(
        process_id=process_id,
        phase=phase,
        step=step,
    )
    agents_md = f"{agents_md.rstrip()}\n\n- **Current Date**: {datetime.now().date().isoformat()}\n"
    agents_md_path = os.path.join(session_dir, 'agents.md')
    with open(agents_md_path, 'w', encoding='utf-8') as f:
        f.write(agents_md)


def _build_orchestrated_prompt(task_description: str, step: str, process_id: str, phase: int) -> str:
    """Build the starting prompt for an orchestrated agent."""
    return f"Read agents-{step}.md and then read task.md. Follow the instructions."


def _orchestrated_setup(body: OrchestratedStartRequest) -> dict:
    """Common setup for all orchestrated agent sessions.

    Creates session dir, copies skills + interactor, writes context files,
    creates tmux session with env vars. Returns a dict with everything the
    agent-specific launcher needs.
    """
    import random, string

    agent = body.agent
    skills_targets = {
        'claude': '.claude/skills',
        'codex':  '.agents/skills',
        'gemini': '.gemini/skills',
    }

    # -- 1. Session directory keyed by process_id ----------------------------
    slug = re.sub(r'[^a-z0-9]+', '-', (body.task_title or 'task').lower()).strip('-')
    dir_name = f'{body.process_id}-{slug}'
    session_dir = os.path.join(AGENT_SESSIONS_DIR, dir_name)
    first_dispatch = not os.path.isdir(session_dir)
    os.makedirs(session_dir, exist_ok=True)

    skills_dir = os.path.join(session_dir, skills_targets[agent])
    os.makedirs(skills_dir, exist_ok=True)

    # -- 2. Copy the workflow skill folder into the skills dir as-is -----------
    skills_src = AGENT_SKILLS_DIRS.get(agent)
    if skills_src:
        skill_src_dir = os.path.join(skills_src, body.step)
        if os.path.isdir(skill_src_dir):
            shutil.copytree(skill_src_dir, os.path.join(skills_dir, body.step), dirs_exist_ok=True)

    # -- 3. Shared scaffolding (only on first dispatch for this process) ------
    if first_dispatch:
        # Copy interactor (script + templates, not skills)
        interactor_src = os.environ.get('INTERACTOR_DIR', '/interactor')
        interactor_dst = os.path.join(session_dir, 'interactor')
        if os.path.isdir(interactor_src):
            shutil.copytree(interactor_src, interactor_dst, ignore=shutil.ignore_patterns('skills'))

        # Copy interactor skills (task-start, task-done, etc.) into skills folder
        commands_targets = {
            'claude': '.claude/skills',
            'codex':  '.agents/skills',
            'gemini': '.gemini/skills',
        }
        commands_dir = os.path.join(session_dir, commands_targets[agent])
        os.makedirs(commands_dir, exist_ok=True)
        
        #REMOVED BY AMAN TO ACCOUNT FOR THE NEW STRUCTURE UNDER THE INTERACTOR FOLDER. 
        # interactor_skills_src = os.path.join(interactor_src, 'skills')
        # if os.path.isdir(interactor_skills_src):
        #     for fname in os.listdir(interactor_skills_src):
        #         src_file = os.path.join(interactor_skills_src, fname)
        #         if os.path.isfile(src_file):
                # shutil.copy2(src_file, os.path.join(commands_dir, fname))
        
        
        #ADDED BY AMAN TO ACCOUNT FOR THE NEW STRUCTURE UNDER THE INTERACTOR
        interactor_skills_src = os.path.join(interactor_src, 'skills')
        if os.path.isdir(interactor_skills_src):
            for fname in os.listdir(interactor_skills_src):
                src_path = os.path.join(interactor_skills_src, fname)
                dst_path = os.path.join(commands_dir, fname)

                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
                elif os.path.isfile(src_path):
                    shutil.copy2(src_path, dst_path)

        # Write task.md (shared across all steps)
        with open(os.path.join(session_dir, 'task.md'), 'w', encoding='utf-8') as f:
            f.write(f'# {body.task_title or body.step}\n\n')
            f.write(body.task_description or '')
            f.write('\n')

    # -- 4. Write agents.md (per-step file to avoid parallel clobbering) ------
    templates_dir = os.environ.get('TEMPLATES_DIR', '/templates')
    agents_md_template = os.path.join(templates_dir, 'agents.md')
    if os.path.isfile(agents_md_template):
        with open(agents_md_template, 'r', encoding='utf-8') as f:
            template = f.read()
        agents_md = template.format(
            process_id=body.process_id,
            phase=body.phase,
            step=body.step,
        )
        agents_md = f"{agents_md.rstrip()}\n\n- **Current Date**: {datetime.now().date().isoformat()}\n"
        with open(os.path.join(session_dir, f'agents-{body.step}.md'), 'w', encoding='utf-8') as f:
            f.write(agents_md)

    # -- 5. Write prompt.txt (per-step file) ---------------------------------
    prompt = _build_orchestrated_prompt(body.task_description, body.step, body.process_id, body.phase)
    with open(os.path.join(session_dir, f'prompt-{body.step}.txt'), 'w', encoding='utf-8') as f:
        f.write(prompt)

    # -- 6. Create tmux session with orchestration env vars -------------------
    short_id = ''.join(random.choices(string.ascii_lowercase, k=5))
    session_name = f'{agent}-{body.process_id}-p{body.phase}-{body.step}-{short_id}'

    # Per-step env file (named by step so parallel steps don't clobber each other)
    env_file = os.path.join(session_dir, f'.env.{body.step}')
    with open(env_file, 'w', encoding='utf-8') as f:
        f.write(f'export ORCHESTRATOR_URL="{os.environ.get("ORCHESTRATOR_URL", "http://host.docker.internal:8100")}"\n')
        f.write(f'export INTERACTION_SERVICE_URL="{os.environ.get("INTERACTION_SERVICE_URL", "http://host.docker.internal:8200")}"\n')
        f.write(f'export PROCESS_ID="{body.process_id}"\n')
        f.write(f'export PHASE="{body.phase}"\n')
        f.write(f'export STEP="{body.step}"\n')
        f.write(f'export TASK_TITLE="{body.task_title}"\n')
        f.write(f'export WORKFLOW_NAME="{body.workflow_name}"\n')
        f.write(f'export PROJECT_NAME="{body.project_name}"\n')
        f.write(f'export SESSION_NAME="{session_name}"\n')

    sandbox_env = []
    if os.path.isfile(SANDBOX_SHELL):
        sandbox_env = [
            '-e', f'SHELL={SANDBOX_SHELL}',
            '-e', 'SANDBOX_REAL_SHELL=/bin/bash',
            '-e', f'SANDBOX_LOG_FILE={SANDBOX_LOG_FILE}',
        ]

    subprocess.run([TMUX, 'kill-session', '-t', session_name], capture_output=True)
    subprocess.run(
        [TMUX, 'new-session', '-d', '-s', session_name, '-x', '220', '-y', '50'] + sandbox_env,
        check=True,
    )

    # Source the env file in the tmux session so all commands inherit the vars
    subprocess.run([TMUX, 'send-keys', '-t', session_name, f'source {shlex.quote(env_file)}', 'Enter'], check=True)

    return {
        'session_dir': session_dir,
        'skills_dir': skills_dir,
        'session_name': session_name,
        'prompt_file': f'prompt-{body.step}.txt',
        'agents_file': f'agents-{body.step}.md',
    }


def _orchestrated_start_claude(body: OrchestratedStartRequest, ctx: dict) -> None:
    """Claude-specific config and launch for orchestrated sessions."""
    session_dir = ctx['session_dir']
    skills_dir = ctx['skills_dir']
    commands_dir = os.path.join(session_dir, '.claude', 'commands')

    # -- Discover all top-level skill folders in the skills dir ---------------
    skill_allows = []
    if os.path.isdir(skills_dir):
        for entry in os.listdir(skills_dir):
            skill_path = os.path.join(skills_dir, entry)
            if os.path.isdir(skill_path):
                skill_allows.append(f'Skill({entry})')

    # -- Discover all top-level command folders in the commands dir -----------
    command_allows = []
    if os.path.isdir(commands_dir):
        for entry in os.listdir(commands_dir):
            command_path = os.path.join(commands_dir, entry)
            if os.path.isdir(command_path):
                command_allows.append(f'Skill({entry})')

    # -- Config: trust dir, explicit allow/deny permissions -------------------
    config_dir = os.path.join(session_dir, '.claude')
    os.makedirs(config_dir, exist_ok=True)
    _trust_path('claude', session_dir)
    settings = {
        'permissions': {
            'defaultMode': 'acceptEdits',
            'allow': [
                f'Read({session_dir}/**)',
                f'Edit({session_dir}/**)',
                f'Write({session_dir}/**)',
                'Bash(bash interactor/interactor.sh *)',
                f'Bash(bash {session_dir}/interactor/interactor.sh *)',
                'Bash(curl *)',
                'Bash(jq *)',
                'Bash(cat *)',
                'Bash(ls *)',
                'Bash(grep *)',
                'Bash(find *)',
                'Bash(mkdir *)',
                'WebFetch',
                'WebSearch',
            ] + skill_allows + command_allows,
            'deny': [
                'Bash(rm *)',
                'Bash(ssh *)',
                'Bash(scp *)',
            ],
        },
        'trustedDirectories': [session_dir],
        'denyPaths': ['/app'],
    }
    with open(os.path.join(config_dir, 'settings.local.json'), 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)

    # -- Launch ---------------------------------------------------------------
    base_command = AGENT_COMMANDS['claude']
    if body.model:
        base_command = f'{base_command} --model {shlex.quote(body.model)}'
    base_command = f'{base_command} "$(cat {shlex.quote(ctx["prompt_file"])})"'
    command = f'cd {shlex.quote(session_dir)} && {base_command}'
    subprocess.run([TMUX, 'send-keys', '-t', ctx['session_name'], command, 'Enter'], check=True)


def _orchestrated_start_codex(body: OrchestratedStartRequest, ctx: dict) -> None:
    """Codex-specific config and launch for orchestrated sessions."""
    session_dir = ctx['session_dir']

    # -- Config ---------------------------------------------------------------
    _trust_path('codex', session_dir)
    perms = permissions_from_dict(body.permissions)
    perms.approval_mode = ApprovalMode.auto_all
    config_dir = os.path.join(session_dir, '.codex')
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, 'config.toml'), 'w', encoding='utf-8') as f:
        f.write(to_codex_toml(perms))

    # -- Launch ---------------------------------------------------------------
    base_command = AGENT_COMMANDS['codex']
    if body.model:
        base_command = f'{base_command} --model {shlex.quote(body.model)}'
    base_command = f'{base_command} "$(cat {shlex.quote(ctx["prompt_file"])})"'
    command = f'cd {shlex.quote(session_dir)} && {base_command}'
    subprocess.run([TMUX, 'send-keys', '-t', ctx['session_name'], command, 'Enter'], check=True)


def _orchestrated_start_gemini(body: OrchestratedStartRequest, ctx: dict) -> None:
    """Gemini-specific config and launch for orchestrated sessions."""
    session_dir = ctx['session_dir']

    # -- Config ---------------------------------------------------------------
    perms = permissions_from_dict(body.permissions)
    config_dir = os.path.join(session_dir, '.gemini')
    os.makedirs(config_dir, exist_ok=True)
    _trust_path('gemini', session_dir)
    with open(os.path.join(config_dir, 'settings.json'), 'w', encoding='utf-8') as f:
        json.dump(to_gemini_settings(perms), f, indent=2)

    # -- Launch ---------------------------------------------------------------
    base_command = AGENT_COMMANDS['gemini']
    if body.model:
        base_command = f'{base_command} --model={shlex.quote(body.model)}'
    base_command = f'{base_command} --prompt-interactive="$(cat {shlex.quote(ctx["prompt_file"])})"'
    command = f'cd {shlex.quote(session_dir)} && {base_command}'
    subprocess.run([TMUX, 'send-keys', '-t', ctx['session_name'], command, 'Enter'], check=True)


_ORCHESTRATED_LAUNCHERS = {
    'claude': _orchestrated_start_claude,
    'codex':  _orchestrated_start_codex,
    'gemini': _orchestrated_start_gemini,
}


async def orchestrated_start(body: OrchestratedStartRequest):
    """Start an agent session for orchestrated workflow execution."""
    agent = body.agent
    if agent not in KNOWN_AGENTS:
        raise HTTPException(status_code=400, detail=f"Unknown agent '{agent}'. Choose from: {sorted(KNOWN_AGENTS)}")

    # Common setup: session dir, skills, interactor, context files, tmux session
    ctx = _orchestrated_setup(body)

    # Agent-specific: config, permissions, launch
    _ORCHESTRATED_LAUNCHERS[agent](body, ctx)

    return {
        'session_name': ctx['session_name'],
        'agent': agent,
        'process_id': body.process_id,
        'phase': body.phase,
        'step': body.step,
        'session_dir': ctx['session_dir'],
    }


if __name__ == '__main__':
    import uvicorn
    from dotenv import load_dotenv
    load_dotenv()
    port = int(os.environ.get('PORT', 5678))
    ssl_cert = os.environ.get('TLS_CERT')
    ssl_key  = os.environ.get('TLS_KEY')
    uvicorn.run(
        socket_app,
        host='0.0.0.0',
        port=port,
        ssl_certfile=ssl_cert or None,
        ssl_keyfile=ssl_key or None,
    )
