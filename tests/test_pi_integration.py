"""Exercise the real Linux sandbox without spending LLM tokens."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from pi_runner.cli import execute, inspect


FAKE_PI = r'''
import json, os, pathlib, subprocess, sys, time
p = pathlib.Path('/workspace')
assert os.environ['HOME'] == '/home/runner'
assert 'RUNNER_TEST_SECRET' not in os.environ
assert not pathlib.Path(SENTINEL).exists()
assert not pathlib.Path(HOST_HOME + '/.ssh').exists()
assert subprocess.run(['/bin/bash', '-c', 'test ! -e ' + SENTINEL]).returncode == 0
assert subprocess.run(['/usr/bin/python3', '-c', 'import pathlib; assert not pathlib.Path(' + repr(SENTINEL) + ').exists()']).returncode == 0
assert (p / 'AGENTS.md').read_text() == 'Write result.txt'
if (p / '.git').exists():
    assert subprocess.run(['git', 'log', '-1'], stdout=subprocess.DEVNULL).returncode == 0
    assert subprocess.run(['git', 'commit', '--allow-empty', '-m', 'forbidden'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
(p / 'result.txt').write_text('changed')
session = pathlib.Path('/session/session.jsonl')
with session.open('a') as f: f.write(json.dumps({'type':'session','id':'test-session'}) + '\n')
task_arg = sys.argv[-1]
task_text = pathlib.Path(task_arg[1:]).read_text(encoding='utf-8') if task_arg.startswith('@') else task_arg
if 'WAIT' in task_text:
    subprocess.Popen(['/bin/bash', '-c', 'setsid /bin/bash -c "while true; do date +%s%N > /workspace/heartbeat; sleep 0.05; done"'], start_new_session=True)
    time.sleep(120)
print(json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'Done'}],'stopReason':'stop','usage':{'input':12,'output':3},'model':'fixture-model'}}), flush=True)
'''


MEDIA_PROBE_PI = r'''
import json, pathlib
inbox = pathlib.Path('/inbox')
attachments = pathlib.Path('/attachments')
def names(directory):
    return sorted(item.name for item in directory.iterdir()) if directory.is_dir() else []
probe = {
    'inbox': names(inbox),
    'inbox_contents': {
        item.name: item.read_bytes().decode('utf-8', 'replace')
        for item in inbox.iterdir()
        if item.is_file()
    },
    'legacy_visible': (inbox / 'legacy.jpg').exists(),
    'attachments': names(attachments),
}
pathlib.Path('/workspace/media_probe.json').write_text(json.dumps(probe))
print(json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'media probe'}],'stopReason':'stop','usage':{'input':1,'output':1},'model':'fixture-model'}}), flush=True)
'''


LARGE_PROMPT_PROBE_PI = r'''
import json
import pathlib
import sys

task_arg = sys.argv[-1]
assert task_arg == '@/session/task.txt', f"Expected '@/session/task.txt', got {task_arg!r}"

session_task_file = pathlib.Path('/session/task.txt')
assert session_task_file.is_file(), "/session/task.txt does not exist or is not a file"

content = session_task_file.read_text(encoding='utf-8')
byte_len = len(content.encode('utf-8'))
assert byte_len == 135_168, f"Expected 135168 bytes, got {byte_len}"
assert content.startswith("PROMPT_START:\n"), "Prompt header corrupted"
assert content.endswith("\n:PROMPT_END"), "Prompt footer corrupted"

probe = {
    'task_arg': task_arg,
    'file_exists': True,
    'byte_length': byte_len,
    'header_ok': True,
    'footer_ok': True,
}
pathlib.Path('/workspace/large_prompt_probe.json').write_text(json.dumps(probe))

print(json.dumps({
    'type': 'message_end',
    'message': {
        'role': 'assistant',
        'content': [{'type': 'text', 'text': 'Large prompt verified'}],
        'stopReason': 'stop',
        'usage': {'input': 135168, 'output': 10},
        'model': 'fixture-model'
    }
}), flush=True)
'''


UNSHADOWED_PROBE_PI = r'''
import json
import pathlib

ws_inbox = pathlib.Path('/workspace/inbox')
repo_doc = ws_inbox / 'my_doc.txt'
repo_doc_exists = repo_doc.is_file()
repo_doc_content = repo_doc.read_text(encoding='utf-8') if repo_doc_exists else None

root_inbox = pathlib.Path('/inbox')
attachment_file = root_inbox / 'external_attachment.txt'
attachment_exists = attachment_file.is_file()
attachment_content = attachment_file.read_text(encoding='utf-8') if attachment_exists else None

shadow_leak_in_workspace = (ws_inbox / 'external_attachment.txt').exists()
repo_leak_in_root_inbox = (root_inbox / 'my_doc.txt').exists()

root_outbox = pathlib.Path('/outbox')
outbox_mounted = root_outbox.is_dir()
if outbox_mounted:
    (root_outbox / 'agent_artifact.log').write_text('Generated output in root outbox', encoding='utf-8')

probe = {
    'repo_doc_exists': repo_doc_exists,
    'repo_doc_content': repo_doc_content,
    'workspace_inbox_files': sorted(p.name for p in ws_inbox.iterdir()) if ws_inbox.is_dir() else [],
    'root_inbox_exists': root_inbox.is_dir(),
    'attachment_exists': attachment_exists,
    'attachment_content': attachment_content,
    'root_inbox_files': sorted(p.name for p in root_inbox.iterdir()) if root_inbox.is_dir() else [],
    'shadow_leak_in_workspace': shadow_leak_in_workspace,
    'repo_leak_in_root_inbox': repo_leak_in_root_inbox,
    'outbox_mounted': outbox_mounted,
}
pathlib.Path('/workspace/unshadowed_probe_result.json').write_text(json.dumps(probe))

print(json.dumps({
    'type': 'message_end',
    'message': {
        'role': 'assistant',
        'content': [{'type': 'text', 'text': 'Unshadowed workspace probe finished'}],
        'stopReason': 'stop',
        'usage': {'input': 1, 'output': 1},
        'model': 'fixture-model'
    }
}), flush=True)
'''


INBOX_REGRESSION_PROBE_PI = r'''
import json
import pathlib
import sys

# Extract file arguments passed between '--' and '@/session/task.txt'
argv = sys.argv
file_args = []
if '--' in argv:
    idx = argv.index('--')
    for arg in argv[idx + 1:]:
        if arg == '@/session/task.txt':
            break
        file_args.append(arg)

root_inbox = pathlib.Path('/inbox')
ws_inbox = pathlib.Path('/workspace/inbox')

probe = {
    'file_args': file_args,
    'argv': argv,
    'inbox_mounted': root_inbox.exists(),
    'root_inbox_files': sorted(p.name for p in root_inbox.iterdir()) if root_inbox.is_dir() else [],
    'ws_inbox_exists': ws_inbox.exists(),
    'ws_inbox_files': sorted(p.name for p in ws_inbox.iterdir()) if ws_inbox.is_dir() else [],
}
pathlib.Path('/workspace/inbox_regression_probe.json').write_text(json.dumps(probe), encoding='utf-8')

print(json.dumps({
    'type': 'message_end',
    'message': {
        'role': 'assistant',
        'content': [{'type': 'text', 'text': 'Inbox regression probe finished'}],
        'stopReason': 'stop',
        'usage': {'input': 1, 'output': 1},
        'model': 'fixture-model'
    }
}), flush=True)
'''


class SandboxTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which('bwrap'):
            self.skipTest('bubblewrap is not installed')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'project'
        self.repo.mkdir()
        (self.repo / 'AGENTS.md').write_text('Write result.txt')
        self.sentinel = self.root / 'neighbor-secret'
        self.sentinel.write_text('private')
        self.fake = self.root / 'fake_pi.py'
        self.fake.write_text('SENTINEL = ' + repr(str(self.sentinel)) + '\nHOST_HOME = ' + repr(str(Path.home())) + '\n' + FAKE_PI)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps(dict(agent_id='test', project=str(self.repo),
            state_dir=str(self.state), pi_command=['/usr/bin/python3', str(self.fake)],
            runtime_paths=[str(self.fake)])))
        self.task = self.root / 'task.md'
        self.task.write_text('Do the task')

    def test_run_and_resume_real_sandbox(self):
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'Initial'], check=True)
        os.environ['RUNNER_TEST_SECRET'] = 'must-not-leak'
        self.addCleanup(os.environ.pop, 'RUNNER_TEST_SECRET', None)
        result = execute(self.config, self.task)
        self.assertEqual(result['status'], 'completed', Path(result['errors_path']).read_text())
        self.assertEqual(result['changed_files'], ['result.txt'])
        self.assertEqual(result['usage'], {'input': 12, 'output': 3})
        self.assertEqual(Path(result['summary_path']).read_text(), 'Done\n')
        self.assertTrue(Path(result['session_path']).exists())
        resumed = execute(self.config, resume=result['session_id'])
        self.assertEqual(resumed['status'], 'completed', Path(resumed['errors_path']).read_text())
        self.assertEqual(resumed['attempt'], 2)
        self.assertEqual(resumed['session_id'], result['session_id'])
        self.assertEqual(len(Path(result['session_path']).read_text().splitlines()), 2)

    def test_large_prompt_executes_via_session_file(self):
        """Task prompt > 132KB passes via @/session/task.txt without Errno 7."""
        # 132 KiB = 135,168 bytes. Linux MAX_ARG_STRLEN is 128 KiB (131,072 bytes).
        prompt_size = 135_168
        header = "PROMPT_START:\n"
        footer = "\n:PROMPT_END"
        padding = "X" * (prompt_size - len(header) - len(footer))
        large_prompt = header + padding + footer
        self.assertEqual(len(large_prompt.encode("utf-8")), prompt_size)

        large_task = self.root / "large_task.md"
        large_task.write_text(large_prompt, encoding="utf-8")

        probe_script = self.root / "large_prompt_pi.py"
        probe_script.write_text(LARGE_PROMPT_PROBE_PI)

        config = json.loads(self.config.read_text())
        config["pi_command"] = ["/usr/bin/python3", str(probe_script)]
        config["runtime_paths"] = [str(probe_script)]
        self.config.write_text(json.dumps(config))

        # Must execute without OSError: [Errno 7] Argument list too long
        result = execute(self.config, large_task)
        self.assertEqual(
            result["status"], "completed", Path(result["errors_path"]).read_text()
        )
        self.assertEqual(result["exit_code"], 0)

        # Inspect probe results recorded from inside the container
        probe_file = self.repo / "large_prompt_probe.json"
        self.assertTrue(probe_file.exists(), "Probe output was not generated by sandbox")
        probe = json.loads(probe_file.read_text())
        self.assertEqual(probe["task_arg"], "@/session/task.txt")
        self.assertTrue(probe["file_exists"])
        self.assertEqual(probe["byte_length"], 135_168)
        self.assertTrue(probe["header_ok"])
        self.assertTrue(probe["footer_ok"])

        # Inspect host-side session files
        session_dir = self.state / "sessions" / result["session_id"]
        host_task_file = session_dir / "pi" / "task.txt"
        self.assertTrue(host_task_file.exists())
        self.assertEqual(host_task_file.read_text(encoding="utf-8"), large_prompt)
        self.assertEqual(
            (session_dir / "task.md").read_text(encoding="utf-8"), large_prompt
        )

    def test_unshadowed_workspace_preserves_repo_inbox(self):
        """Repository inbox/ is unshadowed; attachments mount at /inbox outside workspace."""
        # Setup repository with an internal inbox/ directory and repo document
        repo_inbox = self.repo / "inbox"
        repo_inbox.mkdir(parents=True, exist_ok=True)
        repo_doc = repo_inbox / "my_doc.txt"
        repo_doc.write_text("Internal repository documentation.", encoding="utf-8")

        # Setup external turn-scoped attachments outside the repository
        turn_inbox = self.root / "turn-attachments"
        turn_inbox.mkdir(parents=True, exist_ok=True)
        (turn_inbox / "external_attachment.txt").write_text(
            "External attachment data.", encoding="utf-8"
        )

        turn_outbox = self.root / "turn-outbox"
        turn_outbox.mkdir(parents=True, exist_ok=True)

        probe_script = self.root / "unshadowed_probe_pi.py"
        probe_script.write_text(UNSHADOWED_PROBE_PI)

        config = json.loads(self.config.read_text())
        config["pi_command"] = ["/usr/bin/python3", str(probe_script)]
        config["runtime_paths"] = [str(probe_script)]
        config["inbox_dir"] = str(turn_inbox)
        config["outbox_dir"] = str(turn_outbox)
        self.config.write_text(json.dumps(config))

        result = execute(self.config, self.task)
        self.assertEqual(
            result["status"], "completed", Path(result["errors_path"]).read_text()
        )

        probe_file = self.repo / "unshadowed_probe_result.json"
        self.assertTrue(probe_file.exists())
        probe = json.loads(probe_file.read_text())

        # 1. Assert repository inbox files are visible and unshadowed inside /workspace/inbox
        self.assertTrue(probe["repo_doc_exists"])
        self.assertEqual(probe["repo_doc_content"], "Internal repository documentation.")
        self.assertEqual(probe["workspace_inbox_files"], ["my_doc.txt"])
        self.assertFalse(probe["shadow_leak_in_workspace"])

        # 2. Assert external attachments are mounted at /inbox (root), outside /workspace
        self.assertTrue(probe["root_inbox_exists"])
        self.assertTrue(probe["attachment_exists"])
        self.assertEqual(probe["attachment_content"], "External attachment data.")
        self.assertEqual(probe["root_inbox_files"], ["external_attachment.txt"])
        self.assertFalse(probe["repo_leak_in_root_inbox"])

        # 3. Assert external outbox is mounted at /outbox (root) and received output
        self.assertTrue(probe["outbox_mounted"])
        self.assertTrue((turn_outbox / "agent_artifact.log").exists())
        self.assertEqual(
            (turn_outbox / "agent_artifact.log").read_text(encoding="utf-8"),
            "Generated output in root outbox",
        )

    def test_repo_inbox_unaffected_when_inbox_dir_is_none(self):
        """End-to-end sandbox verification: when c.inbox_dir is None:

        1. file_args passed to pi is strictly empty [].
        2. /inbox is not mounted in the sandbox.
        3. /workspace/inbox files remain unshadowed and intact.
        4. Host c.project / 'inbox' files are NOT moved to archive/.
        """
        repo_inbox = self.repo / 'inbox'
        repo_inbox.mkdir(parents=True, exist_ok=True)
        repo_doc = repo_inbox / 'repo_file.txt'
        repo_doc.write_bytes(b'PNG_DATA_PROJECT_REPOSITORY')
        repo_txt = repo_inbox / 'notes.txt'
        repo_txt.write_text('Repository notes data', encoding='utf-8')

        probe_script = self.root / 'inbox_probe.py'
        probe_script.write_text(INBOX_REGRESSION_PROBE_PI, encoding='utf-8')

        config = json.loads(self.config.read_text(encoding='utf-8'))
        config['pi_command'] = ['/usr/bin/python3', str(probe_script)]
        config['runtime_paths'] = [str(probe_script)]
        # Ensure inbox_dir is explicitly omitted / None
        config.pop('inbox_dir', None)
        self.config.write_text(json.dumps(config), encoding='utf-8')

        result = execute(self.config, self.task)
        self.assertEqual(
            result['status'], 'completed',
            Path(result['errors_path']).read_text(encoding='utf-8')
        )

        probe_file = self.repo / 'inbox_regression_probe.json'
        self.assertTrue(probe_file.is_file(), "Probe output file was not created")
        probe = json.loads(probe_file.read_text(encoding='utf-8'))

        # 1. file_args passed to pi must be strictly empty
        self.assertEqual(probe['file_args'], [])
        self.assertFalse(any('@/inbox' in arg for arg in probe['argv']))
        self.assertFalse(any('@/workspace/inbox' in arg for arg in probe['argv']))

        # 2. /inbox is not mounted in sandbox root
        self.assertFalse(probe['inbox_mounted'])

        # 3. /workspace/inbox contains the repository files unshadowed
        self.assertTrue(probe['ws_inbox_exists'])
        self.assertEqual(probe['ws_inbox_files'], ['notes.txt', 'repo_file.txt'])

        # 4. Host filesystem: repo files must NOT be moved to archive/
        self.assertTrue(repo_doc.is_file(), "repo_file.txt was moved or deleted from repo/inbox!")
        self.assertEqual(repo_doc.read_bytes(), b'PNG_DATA_PROJECT_REPOSITORY')
        self.assertTrue(repo_txt.is_file(), "notes.txt was moved or deleted from repo/inbox!")
        self.assertEqual(repo_txt.read_text(encoding='utf-8'), 'Repository notes data')
        self.assertFalse((repo_inbox / 'archive').exists(), "archive/ was erroneously created in repo!")

    def test_attachments_passed_and_never_archived_when_inbox_dir_is_provided(self):
        """End-to-end sandbox verification: when c.inbox_dir is provided:

        1. file_args passed to pi contains @/inbox/{item.name}.
        2. /inbox is mounted read-only at sandbox root.
        3. /workspace/inbox remains unshadowed and distinct.
        4. Neither host turn attachments nor repo inbox files are moved to archive/.
        """
        # Turn-scoped external attachments
        turn_inbox = self.root / 'turn-attachments'
        turn_inbox.mkdir(parents=True, exist_ok=True)
        turn_img = turn_inbox / 'external_photo.png'
        turn_img.write_bytes(b'EXTERNAL_PHOTO_DATA')
        turn_txt = turn_inbox / 'spec.txt'
        turn_txt.write_text('External specification', encoding='utf-8')

        # Project repository inbox
        repo_inbox = self.repo / 'inbox'
        repo_inbox.mkdir(parents=True, exist_ok=True)
        repo_doc = repo_inbox / 'internal_doc.txt'
        repo_doc.write_text('Internal repository doc', encoding='utf-8')

        probe_script = self.root / 'inbox_probe_provided.py'
        probe_script.write_text(INBOX_REGRESSION_PROBE_PI, encoding='utf-8')

        config = json.loads(self.config.read_text(encoding='utf-8'))
        config['pi_command'] = ['/usr/bin/python3', str(probe_script)]
        config['runtime_paths'] = [str(probe_script)]
        config['inbox_dir'] = str(turn_inbox)
        self.config.write_text(json.dumps(config), encoding='utf-8')

        result = execute(self.config, self.task)
        self.assertEqual(
            result['status'], 'completed',
            Path(result['errors_path']).read_text(encoding='utf-8')
        )

        probe_file = self.repo / 'inbox_regression_probe.json'
        self.assertTrue(probe_file.is_file(), "Probe output file was not created")
        probe = json.loads(probe_file.read_text(encoding='utf-8'))

        # 1. file_args contains external attachments only
        self.assertEqual(probe['file_args'], ['@/inbox/external_photo.png', '@/inbox/spec.txt'])
        self.assertNotIn('@/inbox/internal_doc.txt', probe['file_args'])

        # 2. /inbox is mounted and contains external attachments
        self.assertTrue(probe['inbox_mounted'])
        self.assertEqual(probe['root_inbox_files'], ['external_photo.png', 'spec.txt'])

        # 3. /workspace/inbox is distinct and contains repo files
        self.assertTrue(probe['ws_inbox_exists'])
        self.assertEqual(probe['ws_inbox_files'], ['internal_doc.txt'])

        # 4. Host filesystem: attachments in turn_inbox are never moved to archive/
        self.assertTrue(turn_img.is_file(), "external_photo.png was moved or deleted!")
        self.assertTrue(turn_txt.is_file(), "spec.txt was moved or deleted!")
        self.assertFalse((turn_inbox / 'archive').exists(), "archive/ was erroneously created in turn_inbox!")

        # 5. Host filesystem: repo files are untouched
        self.assertTrue(repo_doc.is_file(), "internal_doc.txt was moved or deleted!")
        self.assertFalse((repo_inbox / 'archive').exists(), "archive/ was erroneously created in repo!")

    def test_stop_kills_detached_descendants_and_resume(self):
        self.task.write_text('WAIT')
        proc = subprocess.Popen([sys.executable, '-m', 'pi_runner', 'run', '--agent', str(self.config), '--task', str(self.task)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 10
        while not (self.repo / 'heartbeat').exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue((self.repo / 'heartbeat').exists())
        directories = list((self.state / 'sessions').iterdir())
        directory = directories[0]
        stopped = subprocess.run([sys.executable, '-m', 'pi_runner', 'stop', '--state-dir', str(self.state), '--session', directory.name], capture_output=True, text=True)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(json.loads(out)['status'], 'stopped', err)
        heartbeat = (self.repo / 'heartbeat').read_text()
        time.sleep(0.25)
        self.assertEqual((self.repo / 'heartbeat').read_text(), heartbeat)
        self.assertEqual(inspect(directory)['status'], 'stopped')
        resumed = execute(self.config, resume=directory.name)
        self.assertEqual(resumed['status'], 'completed')

    def test_explicit_turn_inbox_replaces_legacy_project_inbox(self):
        legacy_inbox = self.repo / 'inbox'
        legacy_inbox.mkdir()
        (legacy_inbox / 'legacy.jpg').write_bytes(b'legacy-bytes')
        (legacy_inbox / 'voice.ogg').write_bytes(b'legacy-voice')

        first_view = self.root / 'view-1'
        first_view.mkdir()
        (first_view / 'photo.jpg').write_bytes(b'current-photo-1')
        first_store = self.root / 'store-1'
        first_store.mkdir()
        (first_store / 'prior.txt').write_bytes(b'prior-1')

        media_pi = self.root / 'media_pi.py'
        media_pi.write_text(MEDIA_PROBE_PI)
        config = json.loads(self.config.read_text())
        config['pi_command'] = ['/usr/bin/python3', str(media_pi)]
        config['runtime_paths'] = [str(media_pi)]
        config['inbox_dir'] = str(first_view)
        config['attachments_dir'] = str(first_store)
        self.config.write_text(json.dumps(config))

        result = execute(self.config, self.task)
        self.assertEqual(
            result['status'], 'completed',
            Path(result['errors_path']).read_text(),
        )
        probe = json.loads((self.repo / 'media_probe.json').read_text())
        self.assertEqual(probe['inbox'], ['photo.jpg'])
        self.assertEqual(probe['inbox_contents'], {'photo.jpg': 'current-photo-1'})
        self.assertFalse(probe['legacy_visible'])
        self.assertEqual(probe['attachments'], ['prior.txt'])
        # The historical shared inbox is untouched outside the child sandbox.
        self.assertEqual((legacy_inbox / 'legacy.jpg').read_bytes(), b'legacy-bytes')
        self.assertEqual((legacy_inbox / 'voice.ogg').read_bytes(), b'legacy-voice')

        # A different conversation mounts only its own read-only store.
        second_view = self.root / 'view-2'
        second_view.mkdir()
        (second_view / 'photo.jpg').write_bytes(b'current-photo-2')
        second_store = self.root / 'store-2'
        second_store.mkdir()
        (second_store / 'other.txt').write_bytes(b'other-2')
        config['inbox_dir'] = str(second_view)
        config['attachments_dir'] = str(second_store)
        self.config.write_text(json.dumps(config))

        second = execute(self.config, self.task)
        self.assertEqual(
            second['status'], 'completed',
            Path(second['errors_path']).read_text(),
        )
        second_probe = json.loads((self.repo / 'media_probe.json').read_text())
        self.assertEqual(second_probe['inbox'], ['photo.jpg'])
        self.assertEqual(
            second_probe['inbox_contents'], {'photo.jpg': 'current-photo-2'}
        )
        self.assertFalse(second_probe['legacy_visible'])
        self.assertEqual(second_probe['attachments'], ['other.txt'])

    def test_missing_bwrap_fails_closed(self):
        old = os.environ.get('PATH')
        os.environ['PATH'] = '/nonexistent'
        try:
            result = execute(self.config, self.task)
        finally:
            os.environ['PATH'] = old or ''
        self.assertEqual(result['status'], 'failed')
        self.assertFalse((self.repo / 'result.txt').exists())
        self.assertIn('bubblewrap is required', result['error'])

    def test_broken_bwrap_fails_closed(self):
        executable = self.root / 'bwrap'
        executable.write_text('#!/bin/sh\necho "isolation unavailable" >&2\nexit 23\n')
        executable.chmod(0o700)
        old = os.environ['PATH']
        os.environ['PATH'] = str(self.root)
        try:
            result = execute(self.config, self.task)
        finally:
            os.environ['PATH'] = old
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 23)
        self.assertFalse((self.repo / 'result.txt').exists())

    def test_crash_kills_children_and_marks_interrupted(self):
        self.task.write_text('WAIT')
        proc = subprocess.Popen([sys.executable, '-m', 'pi_runner', 'run', '--agent', str(self.config), '--task', str(self.task)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 10
        while not (self.repo / 'heartbeat').exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue((self.repo / 'heartbeat').exists())
        directory = next((self.state / 'sessions').iterdir())
        # Concurrent runs are rejected before either can change the project.
        other = subprocess.run([sys.executable, '-m', 'pi_runner', 'run', '--agent', str(self.config), '--task', str(self.task)], capture_output=True)
        self.assertNotEqual(other.returncode, 0)
        self.assertIn(b'Another Runner is active', other.stderr)
        proc.kill()
        proc.communicate(timeout=10)
        time.sleep(0.15)
        heartbeat = (self.repo / 'heartbeat').read_text()
        time.sleep(0.2)
        self.assertEqual((self.repo / 'heartbeat').read_text(), heartbeat)
        interrupted = inspect(directory)
        self.assertEqual(interrupted['status'], 'interrupted')
        self.assertIn('result.txt', interrupted['changed_files'])
        result = execute(self.config, resume=directory.name)
        self.assertEqual(result['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
