"""Run actual JavaScript tools against an isolated HTTP backend, without an LLM."""

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from agent_manager.api import create_app
from agent_manager.knowledge import KnowledgeBase


def test_platform_tools_against_http_backend(tmp_path: Path, migrated_db: Path):
    node = shutil.which("node")
    releases = Path.home() / ".pi/agent/install/releases"
    typebox = next(
        iter(sorted(releases.glob("*/node_modules/typebox"), reverse=True)), None
    )
    if node is None or typebox is None:
        pytest.skip("Node and the Pi TypeBox runtime are required")
    policy = tmp_path / "policy.mjs"
    shutil.copyfile(Path(__file__).parents[1] / "src/pi_runner/policy.mjs", policy)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/typebox").symlink_to(typebox, target_is_directory=True)
    session = tmp_path / "session"
    session.mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "other").mkdir()
    kb = KnowledgeBase(tmp_path / "knowledge")
    kb.agents_dir.mkdir(parents=True)
    app = create_app(f"sqlite:///{migrated_db}", knowledge_base=kb)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while (
                not server.started and thread.is_alive() and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert server.started
            result = subprocess.run(
                [
                    node,
                    str(Path(__file__).with_name("platform_tools_contract.mjs")),
                    str(policy),
                    str(session),
                    f"http://127.0.0.1:{sock.getsockname()[1]}",
                    str(workspace),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            saved = (kb.agents_dir / "scout.md").read_text()
            assert "Approved revision." in saved
            assert "revision: 2" in saved
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()
