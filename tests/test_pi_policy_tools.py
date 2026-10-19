"""Tests for backend_api.json injection and policy.mjs tool exports."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import pi_runner
from pi_runner.cli import build_pi_command, execute
from pi_runner.core import Config


class PolicyToolsTests(unittest.TestCase):
    def test_policy_syntax_and_compilation(self):
        policy_path = Path(pi_runner.__file__).parent / "policy.mjs"
        self.assertTrue(policy_path.exists())
        node = shutil.which("node")
        if node:
            res = subprocess.run([node, "-c", str(policy_path)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"policy.mjs syntax error: {res.stderr}")

    def _write_config(self, root, extra):
        repo = root / "repo"
        repo.mkdir(exist_ok=True)
        (repo / "dummy.txt").write_text("hello")
        config = {
            "agent_id": "test-agent",
            "project": str(repo),
            "state_dir": str(root / "state"),
            "pi_command": ["python3", "-c", "import sys; sys.exit(0)"],
            "runtime_paths": [shutil.which("python3")],
        }
        config.update(extra)
        config_file = root / "config.json"
        config_file.write_text(json.dumps(config))
        task_file = root / "task.md"
        task_file.write_text("Test task")
        return config_file, task_file

    def test_backend_api_json_uses_only_explicit_instance_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file, task_file = self._write_config(
                root,
                {
                    "tools": ["list_tasks", "openviking_search"],
                    "backend": {
                        "backend_url": "http://instance.example:9999",
                        "api_token": "scoped-execution-token",
                        "openviking_url": "http://viking.example:1933",
                        "openviking_token": "viking-instance-token",
                    },
                },
            )

            result = execute(config_file, task_file)
            session_id = result["session_id"]
            session_dir = root / "state" / "sessions" / session_id
            api_file = session_dir / "pi" / "backend_api.json"
            self.assertTrue(api_file.exists())

            api_data = json.loads(api_file.read_text())
            self.assertEqual(api_data["backend_url"], "http://instance.example:9999")
            self.assertEqual(api_data["api_token"], "scoped-execution-token")
            self.assertEqual(api_data["openviking_url"], "http://viking.example:1933")
            self.assertEqual(api_data["openviking_token"], "viking-instance-token")

    def test_turn_identity_is_exposed_to_policy_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file, task_file = self._write_config(
                root, {"tools": ["attach_document"]}
            )

            result = execute(config_file, task_file, turn_id="turn-77")
            session_id = result["session_id"]
            session_dir = root / "state" / "sessions" / session_id
            turn_file = session_dir / "pi" / "turn.json"
            self.assertTrue(turn_file.exists())
            self.assertEqual(
                json.loads(turn_file.read_text()), {"turn_id": "turn-77"}
            )

    def test_turn_identity_is_replaced_on_resume_and_cleared_without_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file, task_file = self._write_config(
                root, {"tools": ["attach_document"]}
            )

            result = execute(config_file, task_file, turn_id="turn-1")
            session_id = result["session_id"]
            turn_file = root / "state" / "sessions" / session_id / "pi" / "turn.json"
            self.assertEqual({"turn_id": "turn-1"}, json.loads(turn_file.read_text()))

            resumed = execute(config_file, resume=session_id, turn_id="turn-2")
            self.assertEqual(resumed["session_id"], session_id)
            self.assertEqual({"turn_id": "turn-2"}, json.loads(turn_file.read_text()))

            # A manual resume without a turn identity must not expose the
            # previous turn to policy tools or replay its deferred requests.
            execute(config_file, resume=session_id)
            self.assertFalse(turn_file.exists())

    def test_no_backend_document_and_no_production_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file, task_file = self._write_config(
                root, {"tools": ["list_tasks"]}
            )

            result = execute(config_file, task_file)
            session_id = result["session_id"]
            session_dir = root / "state" / "sessions" / session_id
            api_file = session_dir / "pi" / "backend_api.json"
            # No implicit localhost address or administrative token is written.
            self.assertFalse(api_file.exists())

    def test_runner_does_not_silently_add_platform_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_file, _ = self._write_config(root, {"tools": ["read", "bash"]})
            config = Config.load(config_file)
            command = build_pi_command(config)
            tools_arg = command[command.index("--tools") + 1]
            self.assertEqual(tools_arg, "read,bash")
            for platform_tool in (
                "create_task",
                "create_agent",
                "trigger_commit_push",
                "openviking_search",
            ):
                self.assertNotIn(platform_tool, tools_arg.split(","))


if __name__ == "__main__":
    unittest.main()
