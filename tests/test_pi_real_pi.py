"""Real Pi, real bubblewrap, deterministic in-process model (no network)."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from pi_runner.cli import execute


class RealPiTests(unittest.TestCase):
    def test_real_pi_tools_and_policy(self):
        # The managed installer layout is detected for this optional integration.
        install = Path.home() / '.pi/agent/install'
        current = install / 'current-version'
        if not current.exists() or not shutil.which('bwrap'):
            self.skipTest('Managed Pi installation and bubblewrap required')
        release = install / 'releases' / current.read_text().strip()
        cli = (release / 'node_modules/.bin/pi').resolve()
        node = Path(shutil.which('node')).resolve()
        fixture = Path(__file__).with_name('fixture_provider.mjs').resolve()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'repo'
            repo.mkdir()
            (repo / 'AGENTS.md').write_text('RUNNER_AGENT_INSTRUCTIONS: create result.txt and run isolation_probe.py')
            sentinel = root / 'neighbor-secret'
            sentinel.write_text('private')
            probe = f'''import pathlib, subprocess
assert not pathlib.Path({str(sentinel)!r}).exists()
assert not pathlib.Path({str(Path.home() / '.ssh')!r}).exists()
assert subprocess.run(['bash', '-c', 'test ! -e {sentinel}']).returncode == 0
assert subprocess.run(['git', 'log', '-1'], stdout=subprocess.DEVNULL).returncode == 0
assert subprocess.run(['python3', '-c', "open('/workspace/.git/config', 'a').write('forbidden')"], stderr=subprocess.DEVNULL).returncode != 0
print('isolation verified')
'''
            (repo / 'isolation_probe.py').write_text(probe)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'Initial'], check=True)
            config = root / 'config.json'
            config.write_text(json.dumps(dict(agent_id='real-pi', project=str(repo), state_dir=str(root / 'state'),
                pi_command=[str(node), str(cli), '--extension', str(fixture)],
                runtime_paths=[str(node), str(release), str(fixture)], model='fixture', provider='runner-fixture')))
            task = root / 'task.md'
            task.write_text('Execute the fixture task')
            result = execute(config, task)
            self.assertEqual(result['status'], 'completed', Path(result['errors_path']).read_text() + '\n' + str(result.get('error')))
            self.assertEqual((repo / 'result.txt').read_text(), 'changed by real Pi\n')
            events = [json.loads(line) for line in Path(result['events_path']).read_text().splitlines()]
            tools = [e for e in events if e.get('type') == 'tool_execution_end']
            self.assertEqual(len(tools), 4)
            self.assertFalse(tools[1]['isError'], str(tools[1]))
            self.assertTrue(tools[-1]['isError'], 'git push must be blocked by the real Pi hook')
            data = json.loads(config.read_text())
            data['model'] = 'fixture-next'
            config.write_text(json.dumps(data))
            resumed = execute(config, resume=result['session_id'])
            self.assertEqual(resumed['status'], 'completed', Path(resumed['errors_path']).read_text())
            self.assertEqual(resumed['attempt'], 2)
            self.assertEqual(resumed['model'], 'fixture-next')
            self.assertEqual(resumed['session_path'], result['session_path'])


if __name__ == '__main__':
    unittest.main()
