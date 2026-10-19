import json
import os
from pathlib import Path
import tempfile
import unittest

from pi_runner.credentials import resolve_pi_credential
from pi_runner.core import RunnerError
from pi_runner.cli import execute


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        old = os.environ['PATH']
        os.environ['PATH'] = str(self.root) + ':' + old
        self.addCleanup(os.environ.__setitem__, 'PATH', old)

    def fake_cli(self, response, exit_code=0):
        cli = self.root / 'pi'
        cli.write_text('#!/usr/bin/python3\nimport json, sys\n'
                       'assert sys.argv[1:] == ["auth", "check", "--provider", "example", "--json", "--credentials", "--no-refresh"]\n'
                       + 'print(' + repr(json.dumps(response)) + ')\n'
                       + f'sys.exit({exit_code})\n')
        cli.chmod(0o700)

    def test_readonly_check_resolves_existing_pi_key(self):
        self.fake_cli({'status': 'ready', 'credentials': 'fixture-secret'})
        self.assertEqual(resolve_pi_credential('example'), 'fixture-secret')

    def test_error_never_includes_credential_output(self):
        self.fake_cli({'status': 'not_ready', 'credentials': 'fixture-secret'}, 1)
        with self.assertRaises(RunnerError) as caught:
            resolve_pi_credential('example')
        self.assertNotIn('fixture-secret', str(caught.exception))

    def test_selected_key_reaches_pi_without_persisting_or_mounting_global_home(self):
        self.fake_cli({'status': 'ready', 'credentials': 'fixture-secret'})
        repo = self.root / 'repo'
        repo.mkdir()
        program = self.root / 'fixture.py'
        program.write_text('''import errno, json, os, pathlib
assert os.environ['RUNNER_PI_API_KEY'] == 'fixture-secret'
models = json.loads(pathlib.Path('/home/runner/.pi/agent/models.json').read_text())
assert models['providers']['example']['apiKey'] == '$RUNNER_PI_API_KEY'
assert models['providers']['example']['baseUrl'] == 'https://example.invalid'
assert not pathlib.Path('/home/runner/.pi/agent/auth.json').exists()
try:
    pathlib.Path('/workspace/forbidden.txt').write_text('must not be written')
except OSError as exc:
    assert exc.errno == errno.EROFS
else:
    raise AssertionError('project is writable during the audit')
print(json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'Credential received'}],'stopReason':'stop'}}))
''')
        models = self.root / 'models.json'
        models.write_text(json.dumps({'providers': {'example': {'baseUrl': 'https://example.invalid'}}}))
        config = self.root / 'agent.json'
        state = self.root / 'state'
        config.write_text(json.dumps(dict(agent_id='test', project=str(repo), state_dir=str(state),
            pi_command=['/usr/bin/python3', str(program)], runtime_paths=[str(program)],
            models_file=str(models), provider='example', pi_auth_provider='example', project_read_only=True)))
        task = self.root / 'task.md'
        task.write_text('Check credential transport without printing it')
        result = execute(config, task)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        for path in state.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'fixture-secret', path.read_bytes(), str(path))
