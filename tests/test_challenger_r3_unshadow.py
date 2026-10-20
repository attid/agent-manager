"""Empirical adversarial challenge test suite for Requirement R3.

Adversarially tests:
1. Unshadowing of /workspace when both /workspace/inbox and /workspace/outbox
   exist in repository with complex directory structures, nested subdirectories,
   and identical filenames colliding with external attachments and outbox artifacts.
2. Complete data isolation:
   - /workspace/inbox/file1.txt preserves repository content.
   - /inbox/file1.txt contains external turn attachment content.
   - /workspace/outbox/file1.txt preserves initial repository content and is never polluted.
   - /outbox/ receives agent output and persists to host turn outbox.
3. Strict read-only enforcement:
   - /inbox is mounted with --ro-bind; overwrite, create, delete, mkdir, rename, chmod all fail.
4. Project read-only mode interaction:
   - When project_read_only=True, /workspace (including /workspace/outbox) is immutable,
     while /outbox remains writable and /inbox remains read-only.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from pi_runner.cli import execute


ADVERSARIAL_PROBE_PI = r'''
import json
import os
import pathlib
import sys

results = {
    'checks': {},
    'ro_bind_checks': {},
    'outbox_checks': {},
    'argv_checks': {},
}

def record(category, name, condition, details=""):
    results[category][name] = {
        'passed': bool(condition),
        'details': details
    }

# 1. Workspace Inbox Unshadowing
ws_inbox = pathlib.Path('/workspace/inbox')
ws_file1 = ws_inbox / 'file1.txt'
record('checks', 'ws_file1_exists', ws_file1.is_file())
ws_file1_val = ws_file1.read_text(encoding='utf-8') if ws_file1.is_file() else ""
record('checks', 'ws_file1_content_intact',
       ws_file1_val == 'REPO_INBOX_FILE1_CONTENT_UNIQUE_ABC123',
       f"got: {ws_file1_val!r}")

ws_deep = ws_inbox / 'nested' / 'deep' / 'subdoc.txt'
record('checks', 'ws_deep_subdoc_exists', ws_deep.is_file())
ws_deep_val = ws_deep.read_text(encoding='utf-8') if ws_deep.is_file() else ""
record('checks', 'ws_deep_subdoc_content_intact',
       ws_deep_val == 'REPO_DEEP_SUBDOC_CONTENT_XYZ',
       f"got: {ws_deep_val!r}")

# 2. Root /inbox Mount Isolation
root_inbox = pathlib.Path('/inbox')
inbox_file1 = root_inbox / 'file1.txt'
record('checks', 'inbox_file1_exists', inbox_file1.is_file())
inbox_file1_val = inbox_file1.read_text(encoding='utf-8') if inbox_file1.is_file() else ""
record('checks', 'inbox_file1_content_is_attachment',
       inbox_file1_val == 'ATTACHMENT_FILE1_CONTENT_UNIQUE_DEF456',
       f"got: {inbox_file1_val!r}")

record('checks', 'unshadowed_not_equal',
       ws_file1_val != inbox_file1_val and bool(ws_file1_val) and bool(inbox_file1_val),
       f"ws: {ws_file1_val!r} vs inbox: {inbox_file1_val!r}")

record('checks', 'no_attachment_in_workspace',
       not (ws_inbox / 'attachment_unique.png').exists())
record('checks', 'no_repo_nested_in_inbox',
       not (root_inbox / 'nested').exists())

# 3. Strict Read-Only (/inbox ro-bind) Verification
try:
    with open('/inbox/file1.txt', 'w', encoding='utf-8') as f:
        f.write('OVERWRITE_TEST')
    record('ro_bind_checks', 'overwrite_prevented', False, "OVERWRITE_SUCCEEDED")
except (OSError, PermissionError) as exc:
    record('ro_bind_checks', 'overwrite_prevented', True, f"Blocked: {type(exc).__name__} ({exc})")

try:
    with open('/inbox/new_intruder.txt', 'w', encoding='utf-8') as f:
        f.write('NEW_FILE_TEST')
    record('ro_bind_checks', 'create_file_prevented', False, "CREATE_SUCCEEDED")
except (OSError, PermissionError) as exc:
    record('ro_bind_checks', 'create_file_prevented', True, f"Blocked: {type(exc).__name__} ({exc})")

try:
    os.remove('/inbox/file1.txt')
    record('ro_bind_checks', 'delete_file_prevented', False, "DELETE_SUCCEEDED")
except (OSError, PermissionError) as exc:
    record('ro_bind_checks', 'delete_file_prevented', True, f"Blocked: {type(exc).__name__} ({exc})")

try:
    os.mkdir('/inbox/intruder_dir')
    record('ro_bind_checks', 'mkdir_prevented', False, "MKDIR_SUCCEEDED")
except (OSError, PermissionError) as exc:
    record('ro_bind_checks', 'mkdir_prevented', True, f"Blocked: {type(exc).__name__} ({exc})")

try:
    os.rename('/inbox/file1.txt', '/inbox/renamed.txt')
    record('ro_bind_checks', 'rename_prevented', False, "RENAME_SUCCEEDED")
except (OSError, PermissionError) as exc:
    record('ro_bind_checks', 'rename_prevented', True, f"Blocked: {type(exc).__name__} ({exc})")

try:
    os.chmod('/inbox/file1.txt', 0o777)
    record('ro_bind_checks', 'chmod_prevented', False, "CHMOD_SUCCEEDED")
except (OSError, PermissionError) as exc:
    record('ro_bind_checks', 'chmod_prevented', True, f"Blocked: {type(exc).__name__} ({exc})")

# 4. Outbox Verification
ws_outbox = pathlib.Path('/workspace/outbox')
root_outbox = pathlib.Path('/outbox')

record('outbox_checks', 'ws_outbox_file1_initial_exists', (ws_outbox / 'file1.txt').is_file())
record('outbox_checks', 'root_outbox_is_dir', root_outbox.is_dir())

# Agent writes to /outbox
write_success = False
try:
    (root_outbox / 'file1.txt').write_text('AGENT_GENERATED_OUTBOX_FILE1', encoding='utf-8')
    (root_outbox / 'agent_report.json').write_text(json.dumps({'status': 'agent_ok'}), encoding='utf-8')
    art_dir = root_outbox / 'artifacts'
    art_dir.mkdir(exist_ok=True)
    (art_dir / 'summary.txt').write_text('NESTED_ARTIFACT_CONTENT', encoding='utf-8')
    write_success = True
except Exception as exc:
    record('outbox_checks', 'outbox_write_exception', False, str(exc))

record('outbox_checks', 'outbox_writes_succeeded', write_success)

# Verify /workspace/outbox is NOT polluted
ws_outbox_file1_val = (ws_outbox / 'file1.txt').read_text(encoding='utf-8') if (ws_outbox / 'file1.txt').is_file() else ""
record('outbox_checks', 'workspace_outbox_unpolluted',
       ws_outbox_file1_val == 'REPO_OUTBOX_FILE1_ORIGINAL',
       f"got: {ws_outbox_file1_val!r}")
record('outbox_checks', 'no_agent_report_in_workspace',
       not (ws_outbox / 'agent_report.json').exists())
record('outbox_checks', 'no_artifacts_in_workspace',
       not (ws_outbox / 'artifacts').exists())

# 5. Argument Check
record('argv_checks', 'session_file_arg',
       sys.argv[-1] == '@/session/task.txt',
       f"got: {sys.argv[-1]!r}")
record('argv_checks', 'inbox_attachment_in_argv',
       '@/inbox/file1.txt' in sys.argv,
       f"argv: {sys.argv}")
record('argv_checks', 'no_workspace_inbox_in_argv',
       not any('@/workspace/inbox' in a for a in sys.argv),
       f"argv: {sys.argv}")

pathlib.Path('/workspace/adversarial_probe_results.json').write_text(
    json.dumps(results, indent=2), encoding='utf-8'
)

print(json.dumps({
    'type': 'message_end',
    'message': {
        'role': 'assistant',
        'content': [{'type': 'text', 'text': 'Adversarial verification finished'}],
        'stopReason': 'stop',
        'usage': {'input': 1, 'output': 1},
        'model': 'fixture-model'
    }
}), flush=True)
'''

READONLY_WS_PROBE_PI = r'''
import json
import os
import pathlib

probe = {}

# In read-only mode, /workspace cannot be written to
try:
    with open('/workspace/should_fail.txt', 'w') as f:
        f.write('fail')
    probe['workspace_writable'] = True
except (OSError, PermissionError) as exc:
    probe['workspace_writable'] = False
    probe['workspace_write_error'] = str(exc)

# But /outbox must be writable
try:
    with open('/outbox/read_only_mode_out.txt', 'w') as f:
        f.write('OUTBOX_WRITABLE_EVEN_WHEN_WS_READONLY')
    probe['outbox_writable'] = True
except Exception as exc:
    probe['outbox_writable'] = False
    probe['outbox_error'] = str(exc)

# /inbox must still be read-only
try:
    with open('/inbox/should_fail.txt', 'w') as f:
        f.write('fail')
    probe['inbox_writable'] = True
except (OSError, PermissionError) as exc:
    probe['inbox_writable'] = False
    probe['inbox_write_error'] = str(exc)

pathlib.Path('/outbox/ro_probe.json').write_text(json.dumps(probe, indent=2))

print(json.dumps({
    'type': 'message_end',
    'message': {
        'role': 'assistant',
        'content': [{'type': 'text', 'text': 'Read-only workspace probe finished'}],
        'stopReason': 'stop',
        'usage': {'input': 1, 'output': 1},
        'model': 'fixture-model'
    }
}), flush=True)
'''


class ChallengerR3UnshadowTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which('bwrap'):
            self.skipTest('bubblewrap is not installed')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # 1. Complex repository tree in /workspace
        self.repo = self.root / 'project'
        self.repo.mkdir()
        (self.repo / 'AGENTS.md').write_text('Write result.txt', encoding='utf-8')

        # Repo inbox with subdirectories and files
        self.repo_inbox = self.repo / 'inbox'
        self.repo_inbox.mkdir()
        (self.repo_inbox / 'file1.txt').write_text(
            'REPO_INBOX_FILE1_CONTENT_UNIQUE_ABC123', encoding='utf-8'
        )
        (self.repo_inbox / 'shared.json').write_text(
            json.dumps({'origin': 'repo_workspace', 'tag': 'original'}),
            encoding='utf-8',
        )
        nested_dir = self.repo_inbox / 'nested' / 'deep'
        nested_dir.mkdir(parents=True)
        (nested_dir / 'subdoc.txt').write_text(
            'REPO_DEEP_SUBDOC_CONTENT_XYZ', encoding='utf-8'
        )

        # Repo outbox with existing files
        self.repo_outbox = self.repo / 'outbox'
        self.repo_outbox.mkdir()
        (self.repo_outbox / 'file1.txt').write_text(
            'REPO_OUTBOX_FILE1_ORIGINAL', encoding='utf-8'
        )
        (self.repo_outbox / 'existing.log').write_text(
            'REPO_PREEXISTING_LOG_DATA', encoding='utf-8'
        )

        # 2. Host turn attachments (external inbox)
        self.turn_inbox = self.root / 'turn-attachments'
        self.turn_inbox.mkdir()
        # Identical filename file1.txt and shared.json
        (self.turn_inbox / 'file1.txt').write_text(
            'ATTACHMENT_FILE1_CONTENT_UNIQUE_DEF456', encoding='utf-8'
        )
        (self.turn_inbox / 'shared.json').write_text(
            json.dumps({'origin': 'turn_inbox', 'tag': 'attachment'}),
            encoding='utf-8',
        )
        (self.turn_inbox / 'attachment_unique.png').write_bytes(
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRtest'
        )

        # 3. Host turn outbox (external outbox)
        self.turn_outbox = self.root / 'turn-outbox'
        self.turn_outbox.mkdir()

        # 4. Probe script setup
        self.probe_script = self.root / 'adversarial_probe_pi.py'
        self.probe_script.write_text(ADVERSARIAL_PROBE_PI, encoding='utf-8')

        # 5. Config setup
        self.state = self.root / 'state'
        self.config_path = self.root / 'config.json'
        self.config_data = {
            'agent_id': 'challenger_agent',
            'project': str(self.repo),
            'state_dir': str(self.state),
            'pi_command': ['/usr/bin/python3', str(self.probe_script)],
            'runtime_paths': [str(self.probe_script)],
            'inbox_dir': str(self.turn_inbox),
            'outbox_dir': str(self.turn_outbox),
        }
        self.config_path.write_text(json.dumps(self.config_data), encoding='utf-8')

        self.task_path = self.root / 'task.md'
        self.task_path.write_text('Adversarial challenge task', encoding='utf-8')

    def test_adversarial_unshadowing_and_isolation(self):
        result = execute(self.config_path, self.task_path)
        self.assertEqual(
            result['status'], 'completed',
            f"Execution failed: {Path(result['errors_path']).read_text(encoding='utf-8')}"
        )

        # Read sandbox internal probe results
        probe_results_file = self.repo / 'adversarial_probe_results.json'
        self.assertTrue(probe_results_file.exists(), "Probe results file not generated")
        probe = json.loads(probe_results_file.read_text(encoding='utf-8'))

        print("\n--- SANDBOX ADVERSARIAL PROBE EXECUTION RESULTS ---")
        print(json.dumps(probe, indent=2))

        # Verify all category checks passed
        for category, checks in probe.items():
            for check_name, check_data in checks.items():
                self.assertTrue(
                    check_data['passed'],
                    f"Check failed [{category}.{check_name}]: {check_data.get('details', '')}"
                )

        # --- Host-side verification ---
        # 1. Host outbox received the agent writes
        host_outbox_file1 = self.turn_outbox / 'file1.txt'
        self.assertTrue(host_outbox_file1.is_file(), "Agent did not write file1.txt to host outbox")
        self.assertEqual(
            host_outbox_file1.read_text(encoding='utf-8'),
            'AGENT_GENERATED_OUTBOX_FILE1'
        )

        host_report = self.turn_outbox / 'agent_report.json'
        self.assertTrue(host_report.is_file(), "Agent did not write agent_report.json to host outbox")
        report_data = json.loads(host_report.read_text(encoding='utf-8'))
        self.assertEqual(report_data.get('status'), 'agent_ok')

        host_nested_artifact = self.turn_outbox / 'artifacts' / 'summary.txt'
        self.assertTrue(host_nested_artifact.is_file())
        self.assertEqual(
            host_nested_artifact.read_text(encoding='utf-8'),
            'NESTED_ARTIFACT_CONTENT'
        )

        # 2. Workspace repo outbox was NOT polluted or modified
        repo_outbox_file1 = self.repo_outbox / 'file1.txt'
        self.assertEqual(
            repo_outbox_file1.read_text(encoding='utf-8'),
            'REPO_OUTBOX_FILE1_ORIGINAL',
            "Workspace repo outbox file1.txt was overwritten or polluted!"
        )
        self.assertEqual(
            (self.repo_outbox / 'existing.log').read_text(encoding='utf-8'),
            'REPO_PREEXISTING_LOG_DATA'
        )
        self.assertFalse(
            (self.repo_outbox / 'agent_report.json').exists(),
            "Agent artifact leaked into workspace repo outbox!"
        )
        self.assertFalse(
            (self.repo_outbox / 'artifacts').exists(),
            "Agent artifact directory leaked into workspace repo outbox!"
        )

        # 3. Workspace repo inbox was NOT polluted or modified
        self.assertEqual(
            (self.repo_inbox / 'file1.txt').read_text(encoding='utf-8'),
            'REPO_INBOX_FILE1_CONTENT_UNIQUE_ABC123',
            "Workspace repo inbox was altered!"
        )
        self.assertEqual(
            (self.repo_inbox / 'nested' / 'deep' / 'subdoc.txt').read_text(encoding='utf-8'),
            'REPO_DEEP_SUBDOC_CONTENT_XYZ'
        )
        self.assertFalse((self.repo_inbox / 'attachment_unique.png').exists())

        # 4. Host turn inbox was NOT modified (read-only enforced)
        self.assertEqual(
            (self.turn_inbox / 'file1.txt').read_text(encoding='utf-8'),
            'ATTACHMENT_FILE1_CONTENT_UNIQUE_DEF456'
        )
        self.assertFalse((self.turn_inbox / 'intruder.txt').exists())
        self.assertFalse((self.turn_inbox / 'intruder_dir').exists())
        self.assertFalse((self.turn_inbox / 'renamed.txt').exists())

        # 5. Changed files reported by runner
        self.assertIn('adversarial_probe_results.json', result['changed_files'])
        self.assertNotIn('outbox/file1.txt', result['changed_files'])
        self.assertNotIn('outbox/agent_report.json', result['changed_files'])

    def test_project_read_only_allows_outbox_writes_while_protecting_workspace_outbox(self):
        """When project_read_only=True, /outbox is still writable while /workspace is read-only."""
        ro_probe_script = self.root / 'ro_probe_pi.py'
        ro_probe_script.write_text(READONLY_WS_PROBE_PI, encoding='utf-8')

        config = dict(self.config_data)
        config['project_read_only'] = True
        config['pi_command'] = ['/usr/bin/python3', str(ro_probe_script)]
        config['runtime_paths'] = [str(ro_probe_script)]

        ro_config_path = self.root / 'ro_config.json'
        ro_config_path.write_text(json.dumps(config), encoding='utf-8')

        result = execute(ro_config_path, self.task_path)
        self.assertEqual(
            result['status'], 'completed',
            Path(result['errors_path']).read_text(encoding='utf-8')
        )

        # Output was written to host outbox
        ro_probe_file = self.turn_outbox / 'ro_probe.json'
        self.assertTrue(ro_probe_file.is_file(), "ro_probe.json was not written to outbox")
        probe = json.loads(ro_probe_file.read_text(encoding='utf-8'))

        print("\n--- READONLY WORKSPACE PROBE EXECUTION RESULTS ---")
        print(json.dumps(probe, indent=2))

        self.assertFalse(probe['workspace_writable'], "Workspace should be read-only")
        self.assertTrue(probe['outbox_writable'], "Outbox should remain writable")
        self.assertFalse(probe['inbox_writable'], "Inbox should remain read-only")

        # Verify host turn outbox contains the generated file
        out_file = self.turn_outbox / 'read_only_mode_out.txt'
        self.assertTrue(out_file.is_file())
        self.assertEqual(
            out_file.read_text(encoding='utf-8'),
            'OUTBOX_WRITABLE_EVEN_WHEN_WS_READONLY'
        )

        # Verify workspace repo was not touched at all
        self.assertFalse((self.repo / 'should_fail.txt').exists())
        self.assertFalse((self.repo_outbox / 'read_only_mode_out.txt').exists())


if __name__ == '__main__':
    unittest.main()
