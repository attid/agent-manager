"""Filesystem policy and configuration. No credentials are persisted here."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil


class RunnerError(Exception):
    pass


def within(path, parent):
    return path == parent or parent in path.parents


def overlap(a, b):
    return within(a, b) or within(b, a)


@dataclass
class Config:
    agent_id: str
    project: Path
    state_dir: Path
    pi_command: list[str]
    runtime_paths: list[Path]
    readonly_paths: list[Path]
    instructions: list[Path]
    tools: list[str]
    network: bool
    model: str | None
    provider: str | None
    credential_env: list[str]
    workdir: str
    models_file: Path | None = None
    pi_auth_provider: str | None = None
    project_read_only: bool = False
    skills: list[str] = field(default_factory=list)
    allow_git_commit: bool = False
    knowledge_dir: Path | None = None
    skills_dir: Path | None = None
    backend: dict[str, str] | None = None
    inbox_dir: Path | None = None
    attachments_dir: Path | None = None

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text())
        def absolute(value):
            p = Path(value).expanduser()
            if not p.is_absolute():
                raise RunnerError(f'Expected an absolute path: {value}')
            return p.resolve()
        project = absolute(data['project'])
        state = absolute(data['state_dir'])
        if not project.is_dir() or project == Path('/'):
            raise RunnerError('project must be an existing directory other than /')
        if within(Path.home().resolve(), project):
            raise RunnerError('project cannot expose the real HOME or its parent')
        if overlap(project, state):
            raise RunnerError('state_dir must be outside the project')
        runtime = [absolute(p) for p in data.get('runtime_paths', [])]
        readonly = [absolute(p) for p in data.get('readonly_paths', [])]
        for p in runtime + readonly:
            if not p.exists():
                raise RunnerError(f'Missing dependency: {p}')
            # Narrow tool installations under HOME are valid; HOME itself is not.
            if p == Path('/') or within(Path.home().resolve(), p) or overlap(p, project) or overlap(p, state):
                raise RunnerError(f'Overbroad or overlapping dependency: {p}')
            if p in [Path('/etc'), Path('/run'), Path('/var'), Path('/tmp'), Path('/home')] or any(within(p, Path(root)) for root in ('/proc', '/sys', '/dev', '/run')):
                raise RunnerError(f'System data directory cannot be a dependency: {p}')
            if any(part in {'.ssh', '.aws', '.gnupg', '.git'} for part in p.parts):
                raise RunnerError(f'Credential or Git directory cannot be a dependency: {p}')
        command = data.get('pi_command', ['pi'])
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise RunnerError('pi_command must be a nonempty array of strings')
        exe = shutil.which(command[0])
        if not exe:
            raise RunnerError(f'Pi executable not found: {command[0]}')
        command = [str(Path(exe).resolve()), *command[1:]]
        for arg in command:
            if arg.startswith('/'):
                p = Path(arg).resolve()
                if not within(p, Path('/usr')) and not any(within(p, r) for r in runtime):
                    raise RunnerError(f'pi_command path needs an explicit runtime_paths entry: {p}')
        workdir = data.get('workdir', '.')
        wd = (project / workdir).resolve()
        if not within(wd, project) or not wd.is_dir():
            raise RunnerError('workdir must resolve to a directory inside project')
        instructions = [absolute(p) for p in data.get('instructions', [])]
        if not all(p.is_file() for p in instructions):
            raise RunnerError('instructions must refer to readable files')
        models_file = absolute(data['models_file']) if data.get('models_file') else None
        if models_file and not models_file.is_file():
            raise RunnerError('models_file must be an existing file')
        knowledge_dir = absolute(data['knowledge_dir']) if data.get('knowledge_dir') else None
        skills_dir = absolute(data['skills_dir']) if data.get('skills_dir') else None
        # Optional explicit media mounts. ``inbox_dir`` replaces the implicit
        # ``<project>/inbox`` scan with a conversation/turn-scoped directory;
        # ``attachments_dir`` exposes earlier same-conversation assets
        # read-only without treating them as current attachments.
        inbox_dir = absolute(data['inbox_dir']) if data.get('inbox_dir') else None
        attachments_dir = absolute(data['attachments_dir']) if data.get('attachments_dir') else None
        for label, directory in (('knowledge_dir', knowledge_dir), ('skills_dir', skills_dir),
                                 ('inbox_dir', inbox_dir), ('attachments_dir', attachments_dir)):
            if directory is not None and not directory.is_dir():
                raise RunnerError(f'{label} must be an existing directory')
        if inbox_dir is not None and (inbox_dir == Path('/') or within(project, inbox_dir)):
            raise RunnerError('inbox_dir must not expose the project or /')
        if attachments_dir is not None and attachments_dir == Path('/'):
            raise RunnerError('attachments_dir must not be /')
        backend = data.get('backend')
        if backend is not None:
            if not isinstance(backend, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in backend.items()
            ):
                raise RunnerError('backend must be an object of string values')
        return cls(data['agent_id'], project, state, command, runtime, readonly,
                   instructions, data.get('tools', ['read', 'bash', 'edit', 'write']),
                   data.get('network', True), data.get('model'), data.get('provider'),
                   data.get('credential_env', []), str(wd.relative_to(project)), models_file,
                   data.get('pi_auth_provider'), data.get('project_read_only', False),
                   data.get('skills', []), data.get('allow_git_commit', False),
                   knowledge_dir, skills_dir, backend, inbox_dir, attachments_dir)


def sandbox_command(config, home, session):
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise RunnerError('bubblewrap is required; refusing an unsandboxed launch')
    c = config
    argv = [bwrap, '--die-with-parent', '--new-session', '--unshare-pid',
            '--unshare-ipc', '--unshare-uts', '--unshare-cgroup-try', '--cap-drop', 'ALL',
            '--clearenv', '--ro-bind', '/usr', '/usr']
    if not c.network:
        argv += ['--unshare-net']
    for name in ('bin', 'sbin', 'lib', 'lib64'):
        p = Path('/') / name
        if p.is_symlink():
            argv += ['--symlink', os.readlink(p), str(p)]
        elif p.exists():
            argv += ['--ro-bind', str(p), str(p)]
    argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
             '--dir', '/etc', '--dir', '/run', '--dir', '/home']
    # Only public OS configuration, never all of /etc.
    for name in ('ld.so.cache', 'ssl/certs', 'ca-certificates', 'resolv.conf', 'hosts', 'nsswitch.conf'):
        p = Path('/etc') / name
        if p.exists():
            argv += ['--ro-bind', str(p.resolve()), str(p)]
    for p in c.runtime_paths + c.readonly_paths:
        argv += ['--ro-bind', str(p), str(p)]
    argv += ['--ro-bind' if c.project_read_only else '--bind', str(c.project), '/workspace', '--bind', str(home), '/home/runner',
             '--bind', str(session), '/session']
    if c.models_file:
        argv += ['--ro-bind', str(c.models_file), '/home/runner/.pi/agent/models.json']
    argv += ['--ro-bind', str(Path(__file__).with_name('policy.mjs')), '/runner/policy.mjs']
    # Knowledge and skills are explicit per-instance mounts. The Runner never
    # invents them from the host HOME directory.
    if c.knowledge_dir is not None:
        argv += ['--ro-bind', str(c.knowledge_dir), '/knowledge']
    if c.skills_dir is not None:
        argv += ['--ro-bind', str(c.skills_dir), '/skills']
    # The current-turn inbox view replaces the project's shared inbox inside
    # the sandbox. Earlier same-conversation assets stay read-only and are
    # never passed to the model as if they arrived with this turn.
    if c.inbox_dir is not None:
        argv += ['--ro-bind', str(c.inbox_dir), '/workspace/inbox']
    if c.attachments_dir is not None:
        argv += ['--ro-bind', str(c.attachments_dir), '/attachments']
    dotgit = c.project / '.git'
    if dotgit.is_dir():
        if dotgit.is_symlink():
            raise RunnerError('Symlinked .git requires an ordinary checkout')
        git_bind = '--bind' if c.allow_git_commit else '--ro-bind'
        argv += [git_bind, str(dotgit), '/workspace/.git']
    elif dotgit.exists():
        # Linked worktrees need their external common directory exposed. Do not
        # silently mount it, since it may expose other projects or credentials.
        raise RunnerError('Linked worktree .git file is unsupported; use a standalone checkout')
    path = ':'.join(dict.fromkeys([str(Path(c.pi_command[0]).parent), '/usr/bin', '/bin']))
    argv += ['--setenv', 'HOME', '/home/runner', '--setenv', 'PATH', path,
             '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'TMPDIR', '/tmp',
             '--setenv', 'PI_CODING_AGENT_DIR', '/home/runner/.pi/agent',
             '--setenv', 'PI_OFFLINE', '1', '--setenv', 'PI_TELEMETRY', '0',
             '--setenv', 'GIT_CONFIG_NOSYSTEM', '1', '--setenv', 'GIT_CONFIG_GLOBAL', '/dev/null',
             '--setenv', 'GIT_TERMINAL_PROMPT', '0', '--chdir', '/workspace/' + c.workdir]
    for name in c.credential_env:
        if name not in os.environ:
            raise RunnerError(f'Missing credential environment variable: {name}')
        # The CLI passes all bwrap options through an anonymous fd, not argv.
        argv += ['--setenv', name, os.environ[name]]
    return argv


def snapshot(project):
    result = {}
    for root, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = [d for d in dirs if d != '.git']
        for name in files + [d for d in dirs if (Path(root) / d).is_symlink()]:
            p = Path(root) / name
            relative = str(p.relative_to(project))
            try:
                if p.is_symlink():
                    value = 'link:' + os.readlink(p)
                elif p.is_file():
                    h = hashlib.sha256()
                    with p.open('rb') as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                            h.update(chunk)
                    value = h.hexdigest() + ':' + oct(p.stat().st_mode & 0o777)
                else:
                    continue
                result[relative] = value
            except FileNotFoundError:
                continue
    return result


def changed_files(before, after):
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
