"""Foreground CLI with durable attempts and a process-scoped sandbox."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import uuid

from .core import Config, RunnerError, sandbox_command, snapshot, changed_files, is_network_restricted
from .credentials import resolve_pi_credential


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def process_stamp(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def skill_arguments(config):
    """Return the explicit skill selection for one launch."""
    if config.skills:
        arguments = []
        for name in config.skills:
            arguments += ['--skill', f'/skills/{name}']
        return arguments
    return ['--no-skills']


def build_pi_command(config, *, instruction_text=''):
    """Build the Pi command for one execution.

    The active tool list is exactly ``config.tools``. The Runner never adds
    platform tools that the profile did not request, so a restricted profile
    cannot acquire the platform surface through the launcher.
    """
    command = config.pi_command + [
        '--mode', 'json', '--print', '--session', '/session/session.jsonl',
        '--no-extensions', '--no-mcp', *skill_arguments(config),
        '--no-prompt-templates', '--no-themes', '--extension', '/runner/policy.mjs',
        '--tools', ','.join(config.tools),
    ]
    if config.model:
        command += ['--model', config.model]
    if config.provider:
        command += ['--provider', config.provider]
    if instruction_text:
        command += ['--append-system-prompt', instruction_text]
    return command


def session_dir(state, session_id):
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError()
    except ValueError:
        raise RunnerError('Invalid session ID') from None
    directory = state / 'sessions' / session_id
    if not directory.is_dir():
        raise RunnerError(f'Session not found: {session_id}')
    return directory


def inbox_file_args(directory):
    """Return file arguments and archive candidates for one inbox view.

    The size limits are the existing Runner compatibility rule, not a new
    policy: larger files still remain visible in the mounted inbox but are
    not inlined as arguments. The function is pure filesystem inspection so
    the explicit-inbox selection can be tested without starting a sandbox.
    """
    file_args = []
    files_to_archive = []
    if not directory.is_dir():
        return file_args, files_to_archive
    for item in sorted(directory.iterdir()):
        if not item.is_file():
            continue
        ext = item.suffix.lower()
        size = item.stat().st_size
        is_image = ext in ('.png', '.jpg', '.jpeg', '.webp', '.gif')
        if (is_image and size <= 15_000_000) or (not is_image and size <= 500_000):
            file_args.append(f'@/inbox/{item.name}')
        files_to_archive.append(item)
    return file_args, files_to_archive


def resolve_inbox_args(config):
    """Determine CLI file arguments and archive candidates for the given Runner Config.

    When config.inbox_dir is None, no attachments are mounted into /inbox, so
    file_args is strictly [] and files_to_archive is strictly []. Any internal
    repository inbox directory (<project>/inbox) is treated purely as repository
    source files and is never scanned, passed to pi, or archived.

    When config.inbox_dir is provided, files are formatted as '@/inbox/<name>'
    and files_to_archive is strictly [] (external attachments are never archived).
    """
    if config.inbox_dir is not None:
        file_args, _ = inbox_file_args(config.inbox_dir)
        files_to_archive = []
    else:
        file_args = []
        files_to_archive = []
    return file_args, files_to_archive


def inspect(directory, project_read_only=False):
    result = json.loads((directory / 'result.json').read_text())
    if result['status'] == 'running' and process_stamp(result['runner_pid']) != result['runner_stamp']:
        result['status'] = 'interrupted'
        result['exit_code'] = None
        result['error'] = 'Runner disappeared; session can be resumed'
        if project_read_only or result.get('project_read_only'):
            result['changed_files'] = []
        else:
            before = directory / 'attempts' / str(result['attempt']) / 'before.json'
            if before.exists():
                previous = json.loads(before.read_text())
                result['changed_files'] = changed_files(
                    previous, snapshot(Path(result['project']), previous=previous)
                )
    return result


def acquire(stack, path, directory=False):
    stream = stack.enter_context(path.open('rb' if directory else 'a+')) if not directory else None
    if directory:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, fd)
    else:
        fd = stream.fileno()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RunnerError('Another Runner is active for this state directory or project') from None


def process_pi_stream(stream, result, errors, transient_errors, usage, events_file=None):
    last_assistant_error = None
    summary = ''
    final_response = False
    for raw in stream:
        line = raw if isinstance(raw, str) else raw.decode('utf-8', errors='replace')
        if events_file is not None:
            if isinstance(raw, bytes):
                events_file.write(raw)
            else:
                events_file.write(raw.encode('utf-8'))
            events_file.flush()
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            errors.append('Invalid JSON output from Pi')
            continue
        if not isinstance(event, dict):
            errors.append('Pi event must be a JSON object')
            continue
        if event.get('type') == 'session':
            result['pi_session_id'] = event.get('id')
        if event.get('type') == 'auto_retry_end' and event.get('success'):
            last_assistant_error = None
        if event.get('type') == 'message_end':
            message = event.get('message', {})
            if message.get('role') == 'assistant':
                stop_reason = message.get('stopReason')
                if stop_reason == 'stop':
                    final_response = True
                    last_assistant_error = None
                elif stop_reason in ('error', 'aborted'):
                    final_response = False
                    err = message.get('errorMessage', stop_reason)
                    last_assistant_error = err
                    transient_errors.append(err)
                text = '\n'.join(x.get('text', '') for x in message.get('content', []) if x.get('type') == 'text')
                if text:
                    summary = text
                for key, value in message.get('usage', {}).items():
                    if isinstance(value, (float, int)):
                        usage[key] = usage.get(key, 0) + value
                result['model'] = message.get('model', result.get('model'))
                result['provider'] = message.get('provider', result.get('provider'))
    if last_assistant_error and not final_response:
        errors.append(last_assistant_error)
    return summary, final_response


def execute(config_path, task=None, resume=None, turn_id=None):
    config_path = config_path.resolve()
    c = Config.load(config_path)
    c.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ExitStack() as stack:
        acquire(stack, c.state_dir / 'runner.lock')
        acquire(stack, c.project, directory=True)
        if resume:
            directory = session_dir(c.state_dir, resume)
            previous = inspect(directory, project_read_only=c.project_read_only)
            if previous['agent_id'] != c.agent_id or previous['project'] != str(c.project):
                raise RunnerError('Resume configuration must retain the same role and project')
            session_id = resume
            (directory / 'pi' / 'clarification.json').unlink(missing_ok=True)
            task_text = task.read_text(encoding='utf-8') if task else 'Continue the saved task. Inspect current files before resuming interrupted work.'
            attempt_number = previous.get('attempt', 1) + 1
        else:
            session_id = str(uuid.uuid4())
            directory = c.state_dir / 'sessions' / session_id
            directory.mkdir(parents=True, mode=0o700)
            task_text = task.read_text(encoding='utf-8')
            (directory / 'task.md').write_text(task_text, encoding='utf-8')
            (directory / 'config-path.txt').write_text(str(config_path))
            attempt_number = 1
        attempt = directory / 'attempts' / str(attempt_number)
        attempt.mkdir(parents=True, mode=0o700)
        (attempt / 'task.md').write_text(task_text, encoding='utf-8')
        home = c.state_dir / 'home'
        home.mkdir(mode=0o700, exist_ok=True)
        # Only transcript storage, not runner metadata, is writable inside Pi.
        pi_dir = directory / 'pi'
        pi_dir.mkdir(mode=0o700, exist_ok=True)
        (pi_dir / 'task.txt').write_text(task_text, encoding='utf-8')
        if is_network_restricted(c.network):
            from .proxy import EgressProxy
            uds_path = pi_dir / 'egress_proxy.sock'
            proxy = EgressProxy(uds_path, allowed_hosts=c.allowed_hosts)
            proxy.start()
            stack.callback(proxy.stop)
            forwarder_src = Path(__file__).with_name('forwarder.py')
            if forwarder_src.is_file():
                shutil.copy2(forwarder_src, pi_dir / 'forwarder.py')
        # The policy extension scopes deferred outgoing requests with this
        # identity. A resumed session must never inherit the previous turn's
        # identity, so an absent value removes the marker.
        turn_file = pi_dir / 'turn.json'
        if turn_id:
            save(turn_file, {'turn_id': str(turn_id)})
        else:
            turn_file.unlink(missing_ok=True)
        result = dict(session_id=session_id, agent_id=c.agent_id, status='running',
                      started_at=now(), ended_at=None, session_path=str(pi_dir / 'session.jsonl'),
                      summary_path=str(directory / 'summary.md'), exit_code=None, usage=None,
                      changed_files=[], model=c.model, provider=c.provider, project=str(c.project),
                      project_read_only=c.project_read_only,
                      runner_pid=os.getpid(), runner_stamp=process_stamp(os.getpid()), attempt=attempt_number,
                      events_path=str(attempt / 'events.jsonl'), errors_path=str(attempt / 'stderr.log'))
        save(directory / 'result.json', result)
        if c.project_read_only:
            before = {}
        else:
            before = snapshot(c.project)
            save(attempt / 'before.json', before)
        proc = None
        stopped = False
        summary = ''
        final_response = False
        errors = []
        transient_errors = []
        last_assistant_error = None
        usage = {}
        files_to_archive = []
        def terminate(signum, frame):
            nonlocal stopped
            stopped = True
            if proc is not None and proc.poll() is None:
                # Killing the namespace supervisor also destroys descendants,
                # including children that created a new session/process group.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for sig in (signal.SIGTERM, signal.SIGINT):
            old = signal.signal(sig, terminate)
            stack.callback(signal.signal, sig, old)
        try:
            argv = sandbox_command(c, home, pi_dir)
            inherited_fds = []
            if c.pi_auth_provider:
                key = resolve_pi_credential(c.pi_auth_provider)
                models = json.loads(c.models_file.read_text()) if c.models_file else {}
                provider = c.provider or c.pi_auth_provider
                models.setdefault('providers', {}).setdefault(provider, {})['apiKey'] = '$RUNNER_PI_API_KEY'
                models_fd = os.memfd_create('runner-model-auth', os.MFD_CLOEXEC)
                stack.callback(os.close, models_fd)
                os.write(models_fd, json.dumps(models).encode())
                os.lseek(models_fd, 0, os.SEEK_SET)
                inherited_fds.append(models_fd)
                argv += ['--ro-bind-data', str(models_fd), '/home/runner/.pi/agent/models.json',
                         '--setenv', 'RUNNER_PI_API_KEY', key]
            if c.allow_git_commit:
                (pi_dir / 'allow_git_commit').touch()
            # bwrap --args keeps credential values off the process command line.
            fd = os.memfd_create('runner-bwrap-args', os.MFD_CLOEXEC)
            stack.callback(os.close, fd)
            arguments = argv[1:]
            instruction_text = '\n\n'.join(p.read_text() for p in c.instructions)
            # Backend settings are explicit per-instance role configuration.
            # There is no compiled-in production address or administrative
            # token fallback: a standalone filesystem-only role simply has no
            # platform backend.
            if c.backend:
                (pi_dir / "backend_api.json").write_text(json.dumps(c.backend, indent=2))
            command = build_pi_command(c, instruction_text=instruction_text)
            # An explicit inbox_dir is the conversation/turn-scoped view mounted at
            # /inbox. The project repository at /workspace remains clean and un-shadowed.
            # When inbox_dir is None, no attachments are passed to pi and no files are archived.
            if c.inbox_dir is not None:
                file_args, _ = inbox_file_args(c.inbox_dir)
                files_to_archive = []
            else:
                file_args = []
                files_to_archive = []

            command += ['--', *file_args, '@/session/task.txt']
            if is_network_restricted(c.network):
                command = ['/usr/bin/python3', '/runner/forwarder.py', *command]
            os.write(fd, b'\0'.join(s.encode() for s in arguments) + b'\0')
            os.lseek(fd, 0, os.SEEK_SET)
            with (attempt / 'stderr.log').open('wb') as stderr, (attempt / 'events.jsonl').open('wb') as events:
                if stopped:
                    raise RunnerError('Stopped before sandbox startup')
                proc = subprocess.Popen([argv[0], '--args', str(fd), '--', *command], stdout=subprocess.PIPE,
                                        stderr=stderr, stdin=subprocess.DEVNULL, start_new_session=True,
                                        pass_fds=(fd, *inherited_fds), env={})
                # Close the signal race between the pre-start check and Popen.
                if stopped:
                    terminate(signal.SIGTERM, None)
                summary, final_response = process_pi_stream(
                    proc.stdout, result, errors, transient_errors, usage, events_file=events
                )
                result['exit_code'] = proc.wait()
                proc.stdout.close()
            if stopped:
                result['status'] = 'stopped'
            elif result['exit_code'] or errors or not summary or not final_response:
                result['status'] = 'failed'
                if (not summary or not final_response) and not errors:
                    errors.append('Pi exited without a final assistant response; see stderr log')
            else:
                result['status'] = 'completed'
        except Exception as exc:
            was_stopped = stopped
            terminate(signal.SIGTERM, None)
            if proc is not None:
                result['exit_code'] = proc.wait()
            result['status'] = 'stopped' if was_stopped else 'failed'
            errors.append(str(exc))
        finally:
            result['ended_at'] = now()
            result['changed_files'] = (
                [] if c.project_read_only
                else changed_files(before, snapshot(c.project, previous=before))
            )
            result['usage'] = usage or None
            if errors:
                result['error'] = '\n'.join(errors)
            if transient_errors:
                result['transient_errors'] = transient_errors
            if summary:
                (directory / 'summary.md').write_text(summary + '\n')
            elif not (directory / 'summary.md').exists():
                (directory / 'summary.md').write_text('No final assistant response. See result.json and attempt logs.\n')

            # Extract structured session artifacts produced by custom tools
            if (pi_dir / 'attachments.json').exists():
                try:
                    result['attachments'] = json.loads((pi_dir / 'attachments.json').read_text())
                except Exception:
                    pass
            if (pi_dir / 'created_agents.json').exists():
                try:
                    result['created_agents'] = json.loads((pi_dir / 'created_agents.json').read_text())
                except Exception:
                    pass
            if (pi_dir / 'saved_projects.json').exists():
                try:
                    result['saved_projects'] = json.loads((pi_dir / 'saved_projects.json').read_text())
                except Exception:
                    pass
            if (pi_dir / 'clarification.json').exists():
                try:
                    result['clarification'] = json.loads((pi_dir / 'clarification.json').read_text())
                    if result['status'] == 'completed':
                        result['status'] = 'waiting_for_input'
                except Exception:
                    pass

            if files_to_archive:
                archive_dir = c.project / 'inbox' / 'archive'
                try:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    for item in files_to_archive:
                        try:
                            dest = archive_dir / item.name
                            if dest.exists():
                                dest.unlink()
                            shutil.move(str(item), str(dest))
                        except Exception:
                            pass
                except Exception:
                    pass

            save(attempt / 'result.json', result)
            save(directory / 'result.json', result)
        return result


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog='runner')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--agent', required=True, type=Path, help='Role configuration JSON path')
    run.add_argument('--task', required=True, type=Path)
    run.add_argument('--turn-id', help='Conversation turn identity for deferred requests')
    for name in ('resume', 'inspect', 'stop'):
        p = sub.add_parser(name)
        p.add_argument('--session', required=True)
        p.add_argument('--state-dir', required=True, type=Path)
        if name == 'resume':
            p.add_argument('--agent', type=Path, help='Override original role config (e.g. change model)')
            p.add_argument('--task', type=Path, help='Optional continuation task')
            p.add_argument('--turn-id', help='Conversation turn identity for deferred requests')
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            result = execute(args.agent, args.task, turn_id=args.turn_id)
        else:
            directory = session_dir(args.state_dir.expanduser().resolve(), args.session)
            if args.command == 'resume':
                config = args.agent or Path((directory / 'config-path.txt').read_text())
                if Config.load(config).state_dir != args.state_dir.expanduser().resolve():
                    raise RunnerError('Resume state directory does not match configuration')
                result = execute(config, args.task, args.session, turn_id=args.turn_id)
            else:
                result = inspect(directory)
                if args.command == 'stop' and result['status'] == 'running':
                    os.kill(result['runner_pid'], signal.SIGTERM)
                    result = dict(result, status='stop_requested')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] in ('completed', 'stop_requested', 'stopped', 'waiting_for_input') or args.command == 'inspect' else 1
    except (RunnerError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
