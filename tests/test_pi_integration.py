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
if 'WAIT' in sys.argv[-1]:
    subprocess.Popen(['/bin/bash', '-c', 'setsid /bin/bash -c "while true; do date +%s%N > /workspace/heartbeat; sleep 0.05; done"'], start_new_session=True)
    time.sleep(120)
print(json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'Done'}],'stopReason':'stop','usage':{'input':12,'output':3},'model':'fixture-model'}}), flush=True)
'''


MEDIA_PROBE_PI = r'''
import json, pathlib
inbox = pathlib.Path('/workspace/inbox')
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
