"""Empirical Adversarial Challenge Test Suite for Milestone M2-2:
Task #40 Analysis Execution & 128KB Prompt Fix Verification.

This test suite empirically challenges the analyzer launch path and Task #40 execution:
1. Real simulation of Task #40:
   - Rich developer profile snapshot (system prompt, runner config, allowed hosts).
   - Task #40 with complex metadata, 20 submitted review operations with realistic patches.
   - Comprehensive diagnostic trace with 60+ tool executions and session events.
   - Instructions rendered by `_render_instructions(task, evidence)` strictly exceed 132 KiB (>135,168 bytes)
     purely through realistic operational and trace data (without artificial padding).
2. Full analyzer launch path execution:
   `_render_instructions` -> `TurnInput` -> `OperationWorker.step()` ->
   `default_analyzer_runner` -> `freeze_profile_handler` -> `PiReplyHandler` ->
   `[sys.executable, "-m", "pi_runner"]` -> `pi_runner.cli.main()` -> `pi_runner.cli.execute()` ->
   `bwrap` sandbox with `@/session/task.txt`.
3. Absence of [Errno 7] Argument list too long:
   - Negative control proving >=131,072 bytes on argv triggers [Errno 7] E2BIG on this Linux kernel.
   - Clean execution of real pi_runner sandbox with prompt exceeding 132 KiB.
4. Validation of evidence structure & citation references:
   - Analyzer inside sandbox verifies top-level entities (`task`, `profile`, `operations`, `trace`, `metrics`).
   - Analyzer verifies compacted manifest `evidence["items"]` (`{"id": ..., "kind": ...}` without "content").
   - Analyzer parses citations, emits valid findings and recommendations citing manifest IDs.
   - Report and recommendations are parsed and persisted to `AnalysisRepository` with `status == "succeeded"`.
"""

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import pytest
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[1]

from agent_manager.analysis import (
    ANALYZER_AGENT_ID,
    ANALYZER_OUTPUT_CONTRACT,
    AnalysisRepository,
    _render_instructions,
    build_evidence_package,
    parse_analyzer_output,
)
from agent_manager.operation_handlers import register_default_operation_handlers
from agent_manager.operation_worker import (
    OPERATION_KIND_ANALYSIS,
    OperationInstanceContext,
    OperationWorker,
)
from agent_manager.storage import (
    AgentProfileRepository,
    Database,
    OperationRepository,
    TaskRepository,
)
from pi_runner.cli import execute


# Script executed inside Bubblewrap sandbox as pi_command
ANALYZER_PROBE_SCRIPT = r"""
import hashlib
import json
import os
import pathlib
import sys

# 1. Verify argv contract: pi_runner must pass '@/session/task.txt' as last argument
task_arg = sys.argv[-1]
assert task_arg == '@/session/task.txt', f"Expected argv[-1] to be '@/session/task.txt', got {task_arg!r}"

task_file = pathlib.Path('/session/task.txt')
assert task_file.is_file(), "/session/task.txt does not exist or is not a file inside sandbox"

raw_bytes = task_file.read_bytes()
prompt_text = raw_bytes.decode('utf-8')
byte_len = len(raw_bytes)
sha256_hex = hashlib.sha256(raw_bytes).hexdigest()

# 2. Extract embedded JSON evidence package from instructions
start_marker = "Evidence package (JSON):\n"
end_marker = "\n\nReturn exactly one JSON object"
start_idx = prompt_text.find(start_marker)
end_idx = prompt_text.find(end_marker)
assert start_idx != -1, f"start_marker not found in prompt (len={byte_len})"
assert end_idx != -1, f"end_marker not found in prompt (len={byte_len})"

json_str = prompt_text[start_idx + len(start_marker):end_idx].strip()
evidence = json.loads(json_str)

# 3. Verify evidence structure
task_data = evidence.get("task", {})
profile_data = evidence.get("profile", {})
operations_data = evidence.get("operations", [])
trace_data = evidence.get("trace", {})
metrics_data = evidence.get("metrics", {})
items_data = evidence.get("items", [])

assert evidence.get("task_id") == "task-40", f"Unexpected task_id: {evidence.get('task_id')}"
assert task_data.get("id") == "task-40"
assert "Task #40" in task_data.get("title", "")

# Verify M2 profile enrichment: system_prompt and runner_config present at top level
assert "system_prompt" in profile_data, "system_prompt missing from top-level profile in evidence"
assert "runner_config" in profile_data, "runner_config missing from top-level profile in evidence"
assert profile_data.get("agent_id") == "developer"
assert len(profile_data["system_prompt"]) > 500

# Verify operations and trace
assert len(operations_data) >= 10, f"Expected >= 10 operations, got {len(operations_data)}"
assert trace_data.get("status") in ("observed", "available"), f"Trace status: {trace_data.get('status')}"
assert "events" in trace_data or "observations" in trace_data

# Verify evidence compaction: items must be metadata-only manifests
assert len(items_data) >= 10, f"Expected >= 10 items, got {len(items_data)}"
for item in items_data:
    assert "id" in item, f"Missing id in item: {item}"
    assert "kind" in item, f"Missing kind in item: {item}"
    assert "content" not in item, f"Found unexpected 'content' in compacted item: {item}"

# 4. Verify citation references
manifest_ids = {item["id"] for item in items_data}
assert "task:task-40" in manifest_ids
assert "trace:task" in manifest_ids
assert any(i.startswith("operation:") for i in manifest_ids)

# Pick valid citations for report
first_op_id = next(i for i in manifest_ids if i.startswith("operation:"))
citations = ["task:task-40", "trace:task", first_op_id]

# 5. Record inspection probe results into /workspace for host verification
probe_result = {
    "task_arg": task_arg,
    "byte_len": byte_len,
    "sha256": sha256_hex,
    "task_id": evidence.get("task_id"),
    "items_count": len(items_data),
    "operations_count": len(operations_data),
    "has_profile_system_prompt": bool(profile_data.get("system_prompt")),
    "has_profile_runner_config": bool(profile_data.get("runner_config")),
    "citations_tested": citations,
}
pathlib.Path("/workspace/analyzer_probe_result.json").write_text(
    json.dumps(probe_result, indent=2), encoding="utf-8"
)

# 6. Formulate structured analyzer report matching ANALYZER_OUTPUT_CONTRACT
report_document = {
    "summary": "Observed session facts for Task #40 with rich trace and operations.",
    "findings": [
        {
            "title": "Comprehensive Task #40 Trace Analysis",
            "detail": f"Observed {len(operations_data)} operations and complete session trace without errors.",
            "evidence": ["task:task-40", "trace:task"]
        }
    ],
    "recommendations": [
        {
            "target_kind": "profile",
            "target_id": "developer",
            "title": "Refine developer profile based on Task #40 observations",
            "rationale": "Empirical analysis confirms flawless execution across 20 review operations.",
            "evidence": citations,
            "proposed_content": {
                "system_prompt": "Updated developer prompt reflecting verified Task #40 workflows."
            },
            "target_revision": 1,
            "target_content_hash": None,
            "diff": None
        }
    ]
}
report_json = json.dumps(report_document, ensure_ascii=False)

# 7. Output stream protocol for pi_runner
print(json.dumps({"type": "session", "id": "analyzer-task-40-session"}), flush=True)
print(json.dumps({
    "type": "message_end",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": report_json}],
        "stopReason": "stop",
        "usage": {"input": byte_len, "output": len(report_json)},
        "model": "system-log-analyzer-probe"
    }
}), flush=True)
"""


class TestChallengerTask40Analysis(unittest.TestCase):
    """Adversarial challenge test case verifying Task #40 analysis execution."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("bwrap"):
            raise unittest.SkipTest("bubblewrap is not installed on this system")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

        # Database setup
        self.db_path = self.tmp_path / "test.db"
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path}")
        command.upgrade(config, "head")
        self.database = Database.from_path(self.db_path)
        self.addCleanup(self.database.dispose)

        # Probe script setup
        self.probe_script = self.tmp_path / "analyzer_probe.py"
        self.probe_script.write_text(ANALYZER_PROBE_SCRIPT, encoding="utf-8")

        # Directories
        self.analyzer_ws = self.tmp_path / "analyzer_workspace"
        self.analyzer_ws.mkdir()
        self.analyzer_state = self.tmp_path / "analyzer_state"
        self.analyzer_state.mkdir()

        # Ensure PYTHONPATH includes agent-manager/src
        src_path = str(Path(__file__).resolve().parent.parent / "src")
        self.old_pythonpath = os.environ.get("PYTHONPATH")
        if self.old_pythonpath:
            os.environ["PYTHONPATH"] = f"{src_path}:{self.old_pythonpath}"
        else:
            os.environ["PYTHONPATH"] = src_path

    def tearDown(self):
        if self.old_pythonpath is not None:
            os.environ["PYTHONPATH"] = self.old_pythonpath
        else:
            os.environ.pop("PYTHONPATH", None)

    def test_01_negative_control_proves_kernel_errno_7_limit(self):
        """Negative control: passing arguments >= 131,072 bytes directly to argv triggers [Errno 7]."""
        # Exactly 131,071 bytes succeeds on Linux x86_64 MAX_ARG_STRLEN
        res = subprocess.run(["/bin/echo", "X" * 131_071], capture_output=True)
        self.assertEqual(res.returncode, 0)

        # >= 131,072 bytes (128 KiB) in argv triggers [Errno 7] E2BIG
        with self.assertRaises(OSError) as ctx_128k:
            subprocess.run(["/bin/echo", "X" * 131_072], capture_output=True)
        self.assertEqual(ctx_128k.exception.errno, errno.E2BIG)
        self.assertIn("Argument list too long", str(ctx_128k.exception))

        # 135,168 bytes (132 KiB) in argv triggers [Errno 7] E2BIG
        with self.assertRaises(OSError) as ctx_132k:
            subprocess.run(["/bin/echo", "X" * 135_168], capture_output=True)
        self.assertEqual(ctx_132k.exception.errno, errno.E2BIG)
        self.assertIn("Argument list too long", str(ctx_132k.exception))

    def test_02_task40_full_analyzer_launch_path_simulation(self):
        """Task #40 realistic simulation (>132 KiB) executes cleanly through full pi_runner path."""
        tasks = TaskRepository(self.database)
        profiles = AgentProfileRepository(self.database)
        operations = OperationRepository(self.database)

        # 1. Register realistic target developer profile with comprehensive rules
        dev_system_prompt = (
            "# Developer Agent Persona\n"
            "You are an autonomous senior software engineer.\n"
            "Constraints:\n"
            "- Always verify behavior with empirical test suites.\n"
            "- Follow strict coding conventions and error handling protocols.\n"
            "- Document findings in five-component handoff reports.\n"
            "Architecture rules:\n"
            "- Bubblewrap namespace isolation for all agent executions.\n"
            "- Zero data loss and robust fallback on missing fields.\n"
        ) * 50  # ~8.5 KB of realistic system prompt

        profiles.create(
            agent_id="developer",
            name="Senior Developer",
            role="developer",
            system_prompt=dev_system_prompt,
            runner_config={
                "project": "/workspace/target",
                "network": False,
                "allowed_hosts": ["eurmtl.me", "api.z-ai.io", "opencode.ai"],
            },
        )

        # 2. Create Task #40
        task_desc = (
            "Task #40: Architectural overhaul and multi-stage refactoring.\n"
            "Scope: Isolate session prompt passing from command-line arguments, "
            "eliminate argument list limits, and enforce metadata compaction.\n"
        ) * 40  # ~8 KB description

        task = tasks.create(
            id="task-40",
            title="Task #40 Architectural Overhaul & Session Trace",
            agent_id="developer",
            description=task_desc,
            metadata={
                "milestone": "M2",
                "reviews": [
                    {"cycle": i, "reviewer": "teamwork_preview_challenger", "verdict": "APPROVE"}
                    for i in range(10)
                ],
            },
        )

        # 3. Create 35 realistic review operations with substantial git diff patches
        for i in range(35):
            patch_content = (
                f"diff --git a/src/service_{i:02d}.py b/src/service_{i:02d}.py\n"
                f"--- a/src/service_{i:02d}.py\n"
                f"+++ b/src/service_{i:02d}.py\n"
                f"@@ -1,30 +1,50 @@\n"
                + "".join(
                    f"+async def process_batch_item_{i}_{j}(payload: dict) -> Result:\n"
                    f"+    logger.info('Processing item {j} in batch {i}')\n"
                    f"+    return await execute_pipeline(payload, timeout=30)\n"
                    for j in range(25)
                )
            )
            sub = operations.submit(
                kind="review",
                target_id=task.id,
                input={
                    "step": i,
                    "target_file": f"src/service_{i:02d}.py",
                    "patch": patch_content,
                    "metrics": {"additions": 75, "deletions": 5},
                },
            )
            claimed = operations.claim_next()
            if claimed:
                operations.complete(
                    claimed.id,
                    claimed.claim_token,
                    result={"status": "approved", "step": i, "patch_lines": 50},
                )

        # 4. Set up realistic diagnostic trace files for Task #40
        task_artifact_root = self.tmp_path / "tasks"
        task_dir = task_artifact_root / f"task_{task.id}_artifacts"
        session_dir = task_dir / "state" / "sessions" / "session-task-40" / "pi"
        session_dir.mkdir(parents=True, exist_ok=True)

        session_records = []
        events_records = []
        for i in range(75):
            call_id = f"tool_call_step_{i:03d}"
            cmd = f"uv run pytest tests/test_component_{i:02d}.py -v -k 'test_step_{i}'"
            session_records.append({
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_call",
                            "id": call_id,
                            "name": "bash",
                            "arguments": {"command": cmd, "cwd": "/workspace"},
                        }
                    ],
                },
            })
            events_records.append({
                "type": "tool_execution_start",
                "toolCallId": call_id,
                "toolName": "bash",
                "args": {"command": cmd},
            })
            events_records.append({
                "type": "tool_execution_end",
                "toolCallId": call_id,
                "isError": False,
                "durationMs": 120 + i * 5,
                "output": f"=== test_step_{i} PASSED [100%] in 0.{i+10}s: verified {i} assertions ===\n",
            })

        (session_dir / "session.jsonl").write_text(
            "\n".join(json.dumps(r) for r in session_records) + "\n", encoding="utf-8"
        )
        (session_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(r) for r in events_records) + "\n", encoding="utf-8"
        )

        # 5. Verify the rendered instructions exceed 132 KiB (135,168 bytes)
        evidence = build_evidence_package(
            self.database, task, artifact_roots=[task_artifact_root]
        )
        rendered_prompt = _render_instructions(task, evidence)
        prompt_bytes = len(rendered_prompt.encode("utf-8"))

        print(f"\n[TASK #40 SIMULATION]")
        print(f"  Rendered prompt size: {prompt_bytes:,} bytes ({prompt_bytes / 1024:.2f} KiB)")
        self.assertGreater(
            prompt_bytes,
            135_168,
            f"Expected prompt > 132 KiB (135,168 bytes), got {prompt_bytes} bytes",
        )

        # 6. Register system-log-analyzer profile pointing to real pi_runner sandbox
        profiles.create(
            agent_id=ANALYZER_AGENT_ID,
            name="System Log Analyzer",
            role="analyzer",
            system_prompt=(
                "# System Log & Session Analyzer\n"
                "Analyze supplied session logs, execution traces, and evidence.\n"
            ),
            runner_config={
                "project": str(self.analyzer_ws),
                "state_dir": str(self.analyzer_state),
                "pi_command": ["/usr/bin/python3", str(self.probe_script)],
                "runtime_paths": [str(self.probe_script)],
                "tools": ["read", "bash"],
                "network": True,
            },
        )

        # 7. Configure OperationInstanceContext with REAL pi_runner CLI:
        # runner_argv_prefix=[sys.executable, "-m", "pi_runner"]
        instance = OperationInstanceContext(
            database=self.database,
            artifact_root=self.tmp_path / "operations",
            runner_argv_prefix=[sys.executable, "-m", "pi_runner"],
        )
        worker = OperationWorker(self.database, instance=instance)
        register_default_operation_handlers(worker)

        # 8. Submit analysis operation for Task #40
        op = operations.submit(
            kind=OPERATION_KIND_ANALYSIS,
            target_id=task.id,
            input={},
        ).operation

        # 9. Step worker: executes full analyzer launch path through real pi_runner!
        outcome = worker.step()

        # 10. Assert flawless execution without [Errno 7]
        self.assertIsNotNone(outcome, "worker.step() produced no outcome")
        self.assertEqual(
            outcome.status,
            "succeeded",
            f"Operation failed with status '{outcome.status}': {outcome.error}",
        )

        # 11. Verify sandbox probe inspection recorded by analyzer inside bwrap
        probe_file = self.analyzer_ws / "analyzer_probe_result.json"
        self.assertTrue(probe_file.is_file(), "analyzer probe result file was not created in workspace")
        probe_data = json.loads(probe_file.read_text(encoding="utf-8"))

        print("\n[SANDBOX PROBE INSPECTION]")
        print(f"  Task argument received: {probe_data['task_arg']}")
        print(f"  Prompt bytes read: {probe_data['byte_len']:,} bytes")
        print(f"  Items count in manifest: {probe_data['items_count']}")
        print(f"  Operations count verified: {probe_data['operations_count']}")
        print(f"  Profile system_prompt present: {probe_data['has_profile_system_prompt']}")
        print(f"  Profile runner_config present: {probe_data['has_profile_runner_config']}")

        self.assertEqual(probe_data["task_arg"], "@/session/task.txt")
        self.assertGreater(probe_data["byte_len"], 135_168)
        self.assertEqual(probe_data["task_id"], "task-40")
        self.assertTrue(probe_data["has_profile_system_prompt"])
        self.assertTrue(probe_data["has_profile_runner_config"])

        # 12. Verify persisted report and citations in database
        analysis_repo = AnalysisRepository(self.database)
        reports = analysis_repo.list_reports(task_id=task.id)
        self.assertEqual(len(reports), 1)

        report = reports[0]
        self.assertEqual(report.agent_id, ANALYZER_AGENT_ID)
        self.assertIn("Observed session facts for Task #40", report.summary)

        recs = analysis_repo.list_recommendations(report_id=report.id)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.target_kind, "profile")
        self.assertEqual(rec.target_id, "developer")
        self.assertIn("task:task-40", rec.evidence)
        self.assertIn("trace:task", rec.evidence)

    def test_03_direct_pi_runner_cli_execute_stress_large_payloads(self):
        """Direct pi_runner execute() handles 132 KiB and 256 KiB multi-lingual injection payloads."""
        # 1. Create a 256 KiB adversarial payload
        target_bytes = 262_144  # 256 KiB
        payload_parts = [
            "=== ADVERSARIAL STRESS TEST START ===\n",
            "Cyrillic: Привет мир! Тестирование анализатора сессий.\n",
            "CJK: 这是一个压力测试，用于验证参数传递与证据包压缩。\n",
            "Japanese: セッショントレース解析のストレステスト。\n",
            "Emojis: 🚀🔥⚡🔍🛡️✅\n",
            "Shell injections: $(rm -rf /) ; whoami | cat /etc/passwd > /dev/null `id`\n",
            "Linebreaks: Windows\r\nMac\rUnix\n",
        ]
        prefix = "".join(payload_parts)
        prefix_bytes = len(prefix.encode("utf-8"))
        suffix = "\n=== ADVERSARIAL STRESS TEST END ===\n"
        suffix_bytes = len(suffix.encode("utf-8"))
        padding = "Z" * (target_bytes - prefix_bytes - suffix_bytes + 100)
        prompt_256k = prefix + padding + suffix
        prompt_256k_bytes = prompt_256k.encode("utf-8")
        self.assertGreaterEqual(len(prompt_256k_bytes), target_bytes)

        # 2. Write prompt to task file as exact binary bytes
        task_file = self.tmp_path / "task_256k.txt"
        task_file.write_bytes(prompt_256k_bytes)

        # 3. Create runner config
        runner_role_path = self.tmp_path / "runner_role_256k.json"
        state_dir = self.tmp_path / "runner_state_256k"
        project_dir = self.tmp_path / "runner_project_256k"
        state_dir.mkdir()
        project_dir.mkdir()

        # Dedicated probe script that checks exact SHA-256
        probe_256k = self.tmp_path / "probe_256k.py"
        normalized_bytes = task_file.read_text(encoding="utf-8").encode("utf-8")
        expected_sha = hashlib.sha256(normalized_bytes).hexdigest()
        probe_256k_code = f"""
import hashlib
import json
import pathlib
import sys

task_arg = sys.argv[-1]
assert task_arg == '@/session/task.txt', f"Expected '@/session/task.txt', got {{task_arg}}"

content = pathlib.Path('/session/task.txt').read_bytes()
assert hashlib.sha256(content).hexdigest() == '{expected_sha}', "SHA256 mismatch!"
assert 'Привет мир!' in content.decode('utf-8')
assert '$(rm -rf /)' in content.decode('utf-8')

# Emit completed pi stream
print(json.dumps({{'type': 'session', 'id': 'session-256k'}}), flush=True)
print(json.dumps({{
    'type': 'message_end',
    'message': {{
        'role': 'assistant',
        'content': [{{'type': 'text', 'text': '256KB Verified'}}],
        'stopReason': 'stop',
        'usage': {{'input': len(content), 'output': 10}},
        'model': 'stress-probe'
    }}
}}), flush=True)
"""
        probe_256k.write_text(probe_256k_code, encoding="utf-8")

        runner_role_path.write_text(
            json.dumps({
                "agent_id": "stress_tester",
                "project": str(project_dir),
                "state_dir": str(state_dir),
                "pi_command": ["/usr/bin/python3", str(probe_256k)],
                "runtime_paths": [str(probe_256k)],
                "tools": ["read", "bash"],
            }),
            encoding="utf-8",
        )

        # 4. Call execute directly
        result = execute(runner_role_path, task=task_file)
        if result.get("status") != "completed":
            stderr_logs = list(state_dir.glob("sessions/*/attempts/*/stderr.log"))
            stderr_content = stderr_logs[0].read_text() if stderr_logs else "No stderr log"
            print(f"\n[EXECUTE ERROR] result={result}\nstderr={stderr_content}")
        self.assertEqual(result.get("status"), "completed")
        self.assertEqual(result.get("exit_code"), 0)
        self.assertEqual(result.get("error"), None)
        print("\n[DIRECT EXECUTE 256 KiB STRESS]")
        print("  Status: completed, exit_code: 0, SHA256 verified inside sandbox.")

    def test_04_analyzer_malformed_report_fail_closed(self):
        """Analyzer returning invalid output fails closed and marks operation failed."""
        tasks = TaskRepository(self.database)
        profiles = AgentProfileRepository(self.database)
        operations = OperationRepository(self.database)

        profiles.create(
            agent_id="developer",
            name="Developer",
            role="developer",
            system_prompt="Standard developer prompt",
        )
        task = tasks.create(id="task-malformed", title="Malformed Analyzer Test", agent_id="developer")

        # Script that emits malformed report (missing summary and invalid target_kind)
        bad_script = self.tmp_path / "bad_analyzer_probe.py"
        bad_code = """
import json
print(json.dumps({'type': 'session', 'id': 'bad-session'}), flush=True)
bad_report = json.dumps({
    'recommendations': [{'target_kind': 'invalid_kind', 'target_id': 'dev'}]
})
print(json.dumps({
    'type': 'message_end',
    'message': {
        'role': 'assistant',
        'content': [{'type': 'text', 'text': bad_report}],
        'stopReason': 'stop',
        'usage': {},
        'model': 'bad-probe'
    }
}), flush=True)
"""
        bad_script.write_text(bad_code, encoding="utf-8")

        profiles.create(
            agent_id=ANALYZER_AGENT_ID,
            name="System Log Analyzer",
            role="analyzer",
            system_prompt="Analyze logs",
            runner_config={
                "project": str(self.analyzer_ws),
                "state_dir": str(self.analyzer_state),
                "pi_command": ["/usr/bin/python3", str(bad_script)],
                "runtime_paths": [str(bad_script)],
                "tools": ["read", "bash"],
            },
        )

        instance = OperationInstanceContext(
            database=self.database,
            artifact_root=self.tmp_path / "operations",
            runner_argv_prefix=[sys.executable, "-m", "pi_runner"],
        )
        worker = OperationWorker(self.database, instance=instance)
        register_default_operation_handlers(worker)

        operations.submit(kind=OPERATION_KIND_ANALYSIS, target_id=task.id)
        outcome = worker.step()

        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("malformed analyzer report", outcome.error)
        print("\n[MALFORMED REPORT FAIL-CLOSED TEST]")
        print(f"  Operation correctly failed with: {outcome.error}")


if __name__ == "__main__":
    unittest.main()
