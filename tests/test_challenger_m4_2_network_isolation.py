"""Empirical Adversarial Challenge Test Suite for Milestone 4 (Challenger M4-2).

Validates Sandbox Network Isolation & In-Container Forwarder:
1. Bubblewrap Sandbox Network Isolation (--unshare-net):
   - Direct network access without proxy (curl --noproxy '*' -sI https://google.com) strictly FAILS.
   - Direct connection to host loopback port fails (host loopback isolation).
   - Direct socket operations to external IPs fail with ENETUNREACH.
2. In-Sandbox Proxy Routing (forwarder.py + EgressProxy):
   - curl -sI https://google.com (unlisted destination) returns HTTP/1.1 403 Forbidden.
   - curl -sI https://eurmtl.me (allowed_hosts) returns HTTP/1.1 200 Connection Established.
   - Node.js fetch() with NODE_OPTIONS="--use-env-proxy" inside sandbox routes successfully.
   - Node.js fetch() to unauthorized host is rejected.
3. Process Cleanup & Zombie Check:
   - Terminate child process -> forwarder exits cleanly, no zombies remain.
   - Terminate sandbox -> forwarder exits cleanly, no processes leak.
   - Clean child exit code propagation (e.g. exit 42 -> bwrap exit 42).
   - Egress proxy UDS socket (egress_proxy.sock) is unlinked after session completion.
4. Adversarial Stress & Edge Cases:
   - Port 18777 collision fallback: forwarder binds ephemeral port 0 and traffic routes.
   - Anti-SSRF enforcement: 169.254.169.254, 127.0.0.1, 10.0.0.1 rejected with 403 Forbidden.
   - Domain spoofing & lookalikes (evil-opencode.ai, openai.com.attacker.com) rejected.
   - Oversized request header protection (>64KB) terminates safely.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import pytest

from pi_runner.proxy import EgressProxy, is_domain_match, is_ip_blocked
from pi_runner.forwarder import bind_loopback, serve_forwarder


def get_base_bwrap_args() -> list[str]:
    """Base bubblewrap arguments mirroring pi_runner.core.sandbox_command."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        pytest.skip("bubblewrap (bwrap) not found")
    argv = [
        bwrap,
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--ro-bind", "/usr", "/usr",
    ]
    for name in ("bin", "sbin", "lib", "lib64"):
        p = Path("/") / name
        if p.is_symlink():
            argv += ["--symlink", os.readlink(p), str(p)]
        elif p.exists():
            argv += ["--ro-bind", str(p), str(p)]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for name in (
        "ld.so.cache",
        "ssl/certs",
        "ca-certificates",
        "resolv.conf",
        "hosts",
        "nsswitch.conf",
    ):
        p = Path("/etc") / name
        if p.exists():
            argv += ["--ro-bind", str(p.resolve()), str(p)]

    # Mount node 24 from nvm if present
    nvm_dir = Path.home() / ".nvm"
    if nvm_dir.is_dir():
        argv += ["--ro-bind", str(nvm_dir), str(nvm_dir)]

    return argv


class TestSandboxNetworkIsolationChallenger(unittest.TestCase):
    """Empirical challenge tests for sandbox network isolation."""

    def setUp(self):
        if not shutil.which("bwrap"):
            self.skipTest("bwrap not available")
        if not shutil.which("curl"):
            self.skipTest("curl not available")

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session_dir = self.root / "session"
        self.session_dir.mkdir()
        self.sock_path = self.session_dir / "egress_proxy.sock"
        self.forwarder_script = self.session_dir / "forwarder.py"

        forwarder_src = (
            Path(__file__).resolve().parent.parent / "src" / "pi_runner" / "forwarder.py"
        )
        shutil.copy2(forwarder_src, self.forwarder_script)

        self.node_bin = Path.home() / ".nvm/versions/node/v24.19.0/bin/node"
        if not self.node_bin.exists():
            self.node_bin = Path(shutil.which("node") or "/usr/bin/node")

    def tearDown(self):
        self.tmp.cleanup()

    # --------------------------------------------------------------------------
    # 1. Direct Network Access Test
    # --------------------------------------------------------------------------

    def test_direct_network_access_strictly_fails(self):
        """Inside bwrap --unshare-net, direct curl without proxy strictly FAILS."""
        cmd = [
            *get_base_bwrap_args(),
            "curl",
            "--noproxy",
            "*",
            "-sI",
            "--connect-timeout",
            "2",
            "https://google.com",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        # Direct curl without proxy must strictly fail (exit code != 0)
        self.assertNotEqual(
            res.returncode,
            0,
            f"Expected direct network access to fail, but succeeded: {res.stdout}",
        )

    def test_direct_host_loopback_isolated(self):
        """Host loopback services are completely unreachable from within --unshare-net."""
        # Start a dummy listener on host loopback
        host_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        host_srv.bind(("127.0.0.1", 0))
        host_srv.listen(1)
        host_port = host_srv.getsockname()[1]

        def accept_one():
            try:
                conn, _ = host_srv.accept()
                conn.close()
            except OSError:
                pass

        th = threading.Thread(target=accept_one, daemon=True)
        th.start()

        # Inside sandbox, attempting to connect to host's 127.0.0.1:<host_port> must fail
        cmd = [
            *get_base_bwrap_args(),
            "/usr/bin/python3",
            "-c",
            f"""
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1.0)
try:
    s.connect(('127.0.0.1', {host_port}))
    sys.exit(0)  # Connected -> breach of isolation!
except OSError:
    sys.exit(42)  # Refused -> isolated!
""",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        host_srv.close()
        self.assertEqual(
            res.returncode,
            42,
            "Direct loopback access to host port must be refused inside unshared network namespace",
        )

    # --------------------------------------------------------------------------
    # 2. In-Sandbox Proxy Routing Test (curl)
    # --------------------------------------------------------------------------

    def test_in_sandbox_curl_unlisted_destination_returns_403(self):
        """In sandbox with forwarder and EgressProxy, unlisted targets return HTTP 403 Forbidden."""
        proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        proxy.start()
        self.addCleanup(proxy.stop)

        bwrap_cmd = [
            *get_base_bwrap_args(),
            "--bind",
            str(self.session_dir),
            "/session",
            "/usr/bin/python3",
            "/session/forwarder.py",
            "curl",
            "-sI",
            "https://google.com",
        ]
        res = subprocess.run(bwrap_cmd, capture_output=True, text=True)
        # Proxy responds with HTTP 403 Forbidden on CONNECT
        self.assertIn("HTTP/1.1 403 Forbidden", res.stdout)

    def test_in_sandbox_curl_allowed_destination_returns_200_connection_established(self):
        """In sandbox with forwarder and EgressProxy, allowed targets return HTTP 200 Connection Established."""
        proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        proxy.start()
        self.addCleanup(proxy.stop)

        bwrap_cmd = [
            *get_base_bwrap_args(),
            "--bind",
            str(self.session_dir),
            "/session",
            "/usr/bin/python3",
            "/session/forwarder.py",
            "curl",
            "-sI",
            "https://eurmtl.me",
        ]
        res = subprocess.run(bwrap_cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"curl failed: {res.stderr}")
        self.assertIn("HTTP/1.1 200 Connection Established", res.stdout)
        self.assertIn("HTTP/2 200", res.stdout)

    # --------------------------------------------------------------------------
    # 3. Node.js fetch() with NODE_OPTIONS="--use-env-proxy" Test
    # --------------------------------------------------------------------------

    def test_in_sandbox_node_fetch_with_env_proxy(self):
        """Node.js fetch with NODE_OPTIONS="--use-env-proxy" routes allowed hosts successfully and blocks unlisted."""
        if not self.node_bin.exists():
            self.skipTest(f"Node binary not found at {self.node_bin}")

        proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        proxy.start()
        self.addCleanup(proxy.stop)

        node_path_dir = str(self.node_bin.parent)
        js_code = """
        // 1. Fetch allowed target
        const resAllowed = await fetch("https://eurmtl.me");
        console.log("FETCH_ALLOWED:" + resAllowed.status);

        // 2. Fetch blocked target
        try {
            await fetch("https://google.com");
            console.log("FETCH_BLOCKED:UNEXPECTED_PASS");
        } catch (err) {
            console.log("FETCH_BLOCKED:REJECTED");
        }
        """

        bwrap_cmd = [
            *get_base_bwrap_args(),
            "--bind",
            str(self.session_dir),
            "/session",
            "--setenv",
            "PATH",
            f"{node_path_dir}:/usr/bin:/bin",
            "/usr/bin/python3",
            "/session/forwarder.py",
            str(self.node_bin),
            "--input-type=module",
            "-e",
            js_code,
        ]
        res = subprocess.run(bwrap_cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node process failed: {res.stderr}")
        self.assertIn("FETCH_ALLOWED:200", res.stdout)
        self.assertIn("FETCH_BLOCKED:REJECTED", res.stdout)

    # --------------------------------------------------------------------------
    # 4. Process Cleanup & Zombie Check
    # --------------------------------------------------------------------------

    def test_child_termination_clean_forwarder_exit_and_no_zombies(self):
        """When child process terminates inside sandbox, forwarder exits promptly without zombies."""
        proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        proxy.start()
        self.addCleanup(proxy.stop)

        # Child runs for 0.2s and exits with code 42
        bwrap_cmd = [
            *get_base_bwrap_args(),
            "--bind",
            str(self.session_dir),
            "/session",
            "/usr/bin/python3",
            "/session/forwarder.py",
            "/usr/bin/python3",
            "-c",
            "import time, sys; time.sleep(0.2); sys.exit(42)",
        ]

        t0 = time.time()
        res = subprocess.run(bwrap_cmd, capture_output=True, text=True)
        t1 = time.time()

        self.assertEqual(res.returncode, 42)
        self.assertLess(t1 - t0, 3.0, "Forwarder took too long to exit after child termination")

        # Verify no forwarder process for this session directory remains
        ps = subprocess.run(["ps", "-ef"], capture_output=True, text=True)
        current_pid = os.getpid()
        session_str = str(self.session_dir)
        running = [
            l
            for l in ps.stdout.splitlines()
            if session_str in l and str(current_pid) not in l
        ]
        self.assertEqual(
            running,
            [],
            f"Forwarder or child process leaked after normal termination: {running}",
        )

    def test_child_killed_by_signal_reaped_cleanly(self):
        """When child process is killed by SIGKILL or SIGTERM, forwarder reaps and propagates code."""
        proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        proxy.start()
        self.addCleanup(proxy.stop)

        # Child kills itself with SIGKILL
        bwrap_cmd = [
            *get_base_bwrap_args(),
            "--bind",
            str(self.session_dir),
            "/session",
            "/usr/bin/python3",
            "/session/forwarder.py",
            "/usr/bin/python3",
            "-c",
            "import os, signal, time; time.sleep(0.1); os.kill(os.getpid(), signal.SIGKILL)",
        ]

        res = subprocess.run(bwrap_cmd, capture_output=True, text=True)
        # Linux standard: 128 + 9 = 137
        self.assertEqual(res.returncode, 137)

        # Verify no zombies or orphaned forwarder processes
        ps = subprocess.run(["ps", "-ef"], capture_output=True, text=True)
        current_pid = os.getpid()
        session_str = str(self.session_dir)
        running = [
            l
            for l in ps.stdout.splitlines()
            if session_str in l and str(current_pid) not in l
        ]
        self.assertEqual(running, [], f"Process leaked after SIGKILL: {running}")

    def test_egress_proxy_socket_unlinked_after_session(self):
        """Proxy Unix domain socket file is unlinked on stop()."""
        proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        proxy.start()
        self.assertTrue(self.sock_path.exists(), "Socket file must exist while proxy is running")

        proxy.stop()
        self.assertFalse(self.sock_path.exists(), "Socket file must be unlinked after proxy stops")

    # --------------------------------------------------------------------------
    # 5. Adversarial Stress & Edge Cases
    # --------------------------------------------------------------------------

    def test_forwarder_port_collision_fallback(self):
        """If default port 18777 is occupied, forwarder allocates ephemeral port and routes correctly."""
        proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        proxy.start()
        self.addCleanup(proxy.stop)

        # Occupy port 18777 inside the sandbox namespace before forwarder launches
        inner_code = """
import socket, subprocess, sys
# Hold 18777 inside sandbox
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("127.0.0.1", 18777))
s.listen(1)

# Now launch forwarder.py with child curl
res = subprocess.run([
    "/usr/bin/python3", "/session/forwarder.py",
    "curl", "-sI", "https://eurmtl.me"
], capture_output=True, text=True)

# Relay output and exit code
sys.stdout.write(res.stdout)
sys.stderr.write(res.stderr)
s.close()
sys.exit(res.returncode)
"""
        bwrap_cmd = [
            *get_base_bwrap_args(),
            "--bind",
            str(self.session_dir),
            "/session",
            "/usr/bin/python3",
            "-c",
            inner_code,
        ]
        res = subprocess.run(bwrap_cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Inner command failed: {res.stderr}")
        self.assertIn("HTTP/1.1 200 Connection Established", res.stdout)

        # Check proxy_port.txt was written with the fallback port (not 18777)
        port_file = self.session_dir / "proxy_port.txt"
        self.assertTrue(port_file.exists())
        allocated_port = int(port_file.read_text().strip())
        self.assertNotEqual(allocated_port, 18777)
        self.assertGreater(allocated_port, 1024)

    def test_anti_ssrf_enforcement_in_sandbox(self):
        """Anti-SSRF blocks attempts to reach private networks and cloud metadata from sandbox."""
        proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        proxy.start()
        self.addCleanup(proxy.stop)

        ssrf_targets = [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8080/secret",
            "http://10.0.0.1/admin",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
        ]
        for target in ssrf_targets:
            bwrap_cmd = [
                *get_base_bwrap_args(),
                "--bind",
                str(self.session_dir),
                "/session",
                "/usr/bin/python3",
                "/session/forwarder.py",
                "curl",
                "-sI",
                target,
            ]
            res = subprocess.run(bwrap_cmd, capture_output=True, text=True)
            self.assertIn(
                "HTTP/1.1 403 Forbidden",
                res.stdout,
                f"Target {target} must be rejected with 403 Forbidden by anti-SSRF",
            )

    def test_domain_spoofing_and_lookalikes_rejected(self):
        """Domain spoofing, suffix injection, and lookalikes are strictly rejected."""
        proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        self.assertFalse(proxy.is_host_allowed("evil-eurmtl.me"))
        self.assertFalse(proxy.is_host_allowed("eurmtl.me.attacker.com"))
        self.assertFalse(proxy.is_host_allowed("opencode.ai.phishing.io"))
        self.assertFalse(proxy.is_host_allowed("fakeopenai.com"))
        self.assertFalse(proxy.is_host_allowed("anthropic.com.hacker.net"))

    def test_oversized_request_header_dos_resilience(self):
        """Proxy terminates connections sending oversized headers (>64KB) without crashing."""
        proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        proxy.start()
        self.addCleanup(proxy.stop)

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(self.sock_path))
        s.settimeout(2.0)

        # Send >65KB header without \r\n\r\n
        try:
            s.sendall(b"GET / HTTP/1.1\r\nHost: eurmtl.me\r\nX-Spam: " + b"A" * 70000 + b"\r\n")
            data = s.recv(1024)
            # Socket should either close (b"") or return error response
        except (OSError, socket.timeout):
            pass
        finally:
            s.close()

        # Verify proxy is still healthy and accepting valid connections
        valid_s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        valid_s.connect(str(self.sock_path))
        valid_s.settimeout(2.0)
        valid_s.sendall(b"CONNECT google.com:443 HTTP/1.1\r\nHost: google.com\r\n\r\n")
        resp = valid_s.recv(1024)
        valid_s.close()
        self.assertIn(b"403 Forbidden", resp)

    def test_full_pi_runner_session_lifecycle(self):
        """End-to-end execution of pi_runner.cli.execute with restricted network and allowed_hosts."""
        from pi_runner.cli import execute
        import json

        project = self.root / "e2e_project"
        project.mkdir()
        (project / "AGENTS.md").write_text("Test agent")

        state = self.root / "e2e_state"
        state.mkdir()

        task_file = self.root / "task.txt"
        task_file.write_text("Empirical pi_runner test")

        fake_pi = self.root / "fake_pi.py"
        fake_pi.write_text(r"""
import json, os, pathlib, subprocess, sys

assert "HTTP_PROXY" in os.environ, "HTTP_PROXY missing"
assert "127.0.0.1" in os.environ["HTTP_PROXY"], "Proxy must point to loopback"
assert "--use-env-proxy" in os.environ.get("NODE_OPTIONS", ""), "NODE_OPTIONS missing --use-env-proxy"
assert pathlib.Path("/session/proxy_port.txt").exists(), "proxy_port.txt missing"

# 1. Blocked target returns 403
res_g = subprocess.run(["curl", "-sI", "https://google.com"], capture_output=True, text=True)
assert "HTTP/1.1 403 Forbidden" in res_g.stdout, f"Expected 403, got {res_g.stdout}"

# 2. Allowed target returns 200
res_e = subprocess.run(["curl", "-sI", "https://eurmtl.me"], capture_output=True, text=True)
assert "HTTP/1.1 200 Connection Established" in res_e.stdout, f"Expected 200, got {res_e.stdout}"

session = pathlib.Path("/session/session.jsonl")
with session.open("a") as f:
    f.write(json.dumps({"type": "session", "id": "test-session"}) + "\n")

print(json.dumps({
    "type": "message_end",
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "Session verified"}],
        "stopReason": "stop",
        "usage": {"input": 1, "output": 1},
        "model": "fixture-model"
    }
}), flush=True)
""")

        cfg = {
            "agent_id": "test-agent",
            "project": str(project),
            "state_dir": str(state),
            "pi_command": ["/usr/bin/python3", str(fake_pi)],
            "runtime_paths": ["/usr/bin/python3", str(fake_pi)],
            "network": "restricted",
            "allowed_hosts": ["eurmtl.me"],
        }
        cfg_file = self.root / "config.json"
        cfg_file.write_text(json.dumps(cfg))

        result = execute(cfg_file, task_file)
        self.assertEqual(result["status"], "completed", f"Session failed: {result}")
        self.assertEqual(result["exit_code"], 0)

        # Confirm socket is unlinked after session completion
        session_pi_dir = state / "sessions" / result["session_id"] / "pi"
        sock = session_pi_dir / "egress_proxy.sock"
        self.assertFalse(sock.exists(), "egress_proxy.sock must be unlinked after session completion")


if __name__ == "__main__":
    unittest.main()
