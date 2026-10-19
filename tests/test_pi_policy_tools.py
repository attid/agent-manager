"""Tests for backend_api.json injection and policy.mjs tool exports."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import pi_runner
from pi_runner.cli import execute


class PolicyToolsTests(unittest.TestCase):
    def test_policy_syntax_and_compilation(self):
        policy_path = Path(pi_runner.__file__).parent / "policy.mjs"
        self.assertTrue(policy_path.exists())
        node = shutil.which("node")
        if node:
            res = subprocess.run([node, "-c", str(policy_path)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"policy.mjs syntax error: {res.stderr}")

    def test_backend_api_json_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "dummy.txt").write_text("hello")

            config_file = root / "config.json"
            config_file.write_text(json.dumps({
                "agent_id": "test-agent",
                "project": str(repo),
                "state_dir": str(root / "state"),
                "pi_command": ["python3", "-c", "import sys; sys.exit(0)"],
                "runtime_paths": [shutil.which("python3")],
                "tools": ["list_tasks", "openviking_search"],
            }))

            task_file = root / "task.md"
            task_file.write_text("Test task")

            result = execute(config_file, task_file)
            session_id = result["session_id"]
            session_dir = root / "state" / "sessions" / session_id
            api_file = session_dir / "pi" / "backend_api.json"
            self.assertTrue(api_file.exists())

            api_data = json.loads(api_file.read_text())
            self.assertEqual(api_data["backend_url"], "http://127.0.0.1:18767")
            self.assertEqual(api_data["openviking_url"], "http://127.0.0.1:1933")
            self.assertIn("openviking_token", api_data)
            self.assertIn("api_token", api_data)
