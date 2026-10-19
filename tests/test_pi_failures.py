import json
from pathlib import Path
import tempfile
import unittest

from pi_runner.cli import execute


class ResultTests(unittest.TestCase):
    def run_fixture(self, code):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = root / 'repo'
        repo.mkdir()
        program = root / 'pi.py'
        program.write_text(code)
        config = root / 'agent.json'
        config.write_text(json.dumps(dict(agent_id='fixture', project=str(repo), state_dir=str(root / 'state'),
            pi_command=['/usr/bin/python3', str(program)], runtime_paths=[str(program)])))
        task = root / 'task.md'
        task.write_text('test')
        return execute(config, task)

    def test_zero_exit_provider_error_is_failed(self):
        result = self.run_fixture('import json\nprint(json.dumps({"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"provider unavailable"}}))')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('provider unavailable', result['error'])

    def test_incomplete_tool_turn_is_not_success(self):
        result = self.run_fixture('import json\nprint(json.dumps({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Working"}],"stopReason":"toolUse"}}))')
        self.assertEqual(result['status'], 'failed')

    def test_invalid_protocol_retains_diagnostics(self):
        result = self.run_fixture('print("not json")')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Invalid JSON', result['error'])
        self.assertEqual(Path(result['events_path']).read_text(), 'not json\n')
