"""Comprehensive unit, integration, and E2E tests for Sandbox Egress Proxy and network allowlisting.

Validates Requirement R1:
- Default trusted LLM providers & wildcard matching
- Custom runner_config.allowed_hosts
- Anti-SSRF private IP / cloud metadata blocking
- HTTP CONNECT tunneling & HTTP 403 Forbidden rejection
- Config.load parsing of network ("restricted") & allowed_hosts
- Bubblewrap --unshare-net invocation
- Real in-sandbox curl execution with egress proxy
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import pytest

from pi_runner.core import (
    Config,
    RunnerError,
    is_network_restricted,
    sandbox_command,
)
from pi_runner.proxy import (
    DEFAULT_ALLOWED_DOMAINS,
    EgressProxy,
    is_domain_match,
    is_ip_blocked,
)


# ==============================================================================
# 1. Unit Tests: Domain Allowlist Matching
# ==============================================================================

class TestDomainAllowlist(unittest.TestCase):
    def test_default_llm_domains_included(self):
        """Default allowlist contains all required trusted LLM provider domains."""
        expected = [
            "opencode.ai", "*.opencode.ai",
            "z-ai.io", "*.z-ai.io", "api.z-ai.io",
            "openai.com", "*.openai.com", "api.openai.com",
            "anthropic.com", "*.anthropic.com", "api.anthropic.com",
            "generativelanguage.googleapis.com", "googleapis.com", "*.googleapis.com",
            "deepseek.com", "*.deepseek.com", "api.deepseek.com",
            "openrouter.ai", "*.openrouter.ai",
        ]
        for domain in expected:
            self.assertIn(domain, DEFAULT_ALLOWED_DOMAINS, f"Missing {domain} in default allowlist")

    def test_wildcard_domain_matching(self):
        """Wildcard patterns correctly match multi-level and single-level subdomains."""
        self.assertTrue(is_domain_match("api.openai.com", "*.openai.com"))
        self.assertTrue(is_domain_match("chat.deepseek.com", "*.deepseek.com"))
        self.assertTrue(is_domain_match("v1.api.z-ai.io", "*.z-ai.io"))
        # Wildcard pattern also matches the root domain itself
        self.assertTrue(is_domain_match("openai.com", "*.openai.com"))

    def test_exact_domain_matching(self):
        """Exact domain matches match the domain and direct subdomains."""
        self.assertTrue(is_domain_match("opencode.ai", "opencode.ai"))
        self.assertTrue(is_domain_match("api.opencode.ai", "opencode.ai"))

    def test_custom_allowed_hosts_matching(self):
        """Custom allowed_hosts entries are properly evaluated by EgressProxy."""
        proxy = EgressProxy("/tmp/test_eval.sock", allowed_hosts=["eurmtl.me", "*.partner.org"])
        self.assertTrue(proxy.is_host_allowed("eurmtl.me"))
        self.assertTrue(proxy.is_host_allowed("api.eurmtl.me"))
        self.assertTrue(proxy.is_host_allowed("service.partner.org"))
        # Built-in LLMs also still allowed
        self.assertTrue(proxy.is_host_allowed("api.anthropic.com"))

    def test_unauthorized_destinations_rejected(self):
        """Arbitrary internet targets outside the allowlist are rejected."""
        proxy = EgressProxy("/tmp/test_eval.sock", allowed_hosts=["eurmtl.me"])
        self.assertFalse(proxy.is_host_allowed("google.com"))
        self.assertFalse(proxy.is_host_allowed("yandex.ru"))
        self.assertFalse(proxy.is_host_allowed("github.com"))
        self.assertFalse(proxy.is_host_allowed("raw.githubusercontent.com"))
        self.assertFalse(proxy.is_host_allowed("attacker-c2.net"))

    def test_lookalike_and_subdomain_collision_rejected(self):
        """Lookalike domains and suffix spoofing attempts are rejected."""
        self.assertFalse(is_domain_match("notopenai.com", "openai.com"))
        self.assertFalse(is_domain_match("evil-openai.com", "*.openai.com"))
        self.assertFalse(is_domain_match("opencode.ai.attacker.com", "opencode.ai"))
        self.assertFalse(is_domain_match("eurmtl.me.phishing.cc", "eurmtl.me"))

    def test_case_insensitivity_and_trailing_dot(self):
        """Domain evaluation is case-insensitive and handles trailing dots."""
        self.assertTrue(is_domain_match("API.OPENAI.COM", "*.openai.com"))
        self.assertTrue(is_domain_match("api.openai.com.", "*.openai.com"))
        self.assertTrue(is_domain_match("EurMTL.ME", "eurmtl.me"))


# ==============================================================================
# 2. Unit Tests: Anti-SSRF Protection
# ==============================================================================

class TestAntiSSRF(unittest.TestCase):
    def test_private_ipv4_blocked(self):
        """RFC 1918 private subnets are blocked."""
        private_ips = [
            "10.0.0.1", "10.254.254.254",
            "172.16.0.1", "172.31.255.255",
            "192.168.0.1", "192.168.1.100",
        ]
        for ip in private_ips:
            self.assertTrue(is_ip_blocked(ipaddress.ip_address(ip)), f"IP {ip} should be blocked")

    def test_loopback_ipv4_and_ipv6_blocked(self):
        """Loopback interfaces (127.0.0.0/8, ::1, ::ffff:127.0.0.1) are blocked."""
        loopbacks = [
            "127.0.0.1", "127.0.0.2", "127.12.34.56",
            "::1", "::ffff:127.0.0.1",
        ]
        for ip in loopbacks:
            self.assertTrue(is_ip_blocked(ipaddress.ip_address(ip)), f"Loopback {ip} should be blocked")

    def test_cloud_metadata_blocked(self):
        """Cloud instance metadata (169.254.169.254) and link-local ranges are blocked."""
        metadata_ips = [
            "169.254.169.254", "169.254.1.1", "fe80::1",
            "::ffff:169.254.169.254",
        ]
        for ip in metadata_ips:
            self.assertTrue(is_ip_blocked(ipaddress.ip_address(ip)), f"Metadata IP {ip} should be blocked")

    def test_carrier_grade_nat_and_benchmarking_blocked(self):
        """Carrier-Grade NAT (RFC 6598) and benchmark subnets are blocked."""
        blocked = ["100.64.0.1", "100.127.255.254", "198.18.0.1"]
        for ip in blocked:
            self.assertTrue(is_ip_blocked(ipaddress.ip_address(ip)), f"IP {ip} should be blocked")

    def test_public_ips_allowed_when_host_permitted(self):
        """Public IP addresses are not blocked by is_ip_blocked."""
        public_ips = ["8.8.8.8", "1.1.1.1", "104.18.25.10", "2606:4700::6811:d209"]
        for ip in public_ips:
            self.assertFalse(is_ip_blocked(ipaddress.ip_address(ip)), f"Public IP {ip} should NOT be blocked")


# ==============================================================================
# 3. Integration Tests: EgressProxy Server & Network Handshake
# ==============================================================================

class TestEgressProxyServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock_path = Path(self.tmp.name) / "egress_proxy.sock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_lifecycle_and_socket_cleanup(self):
        """Starting proxy creates UDS; stopping proxy cleanly unlinks it."""
        proxy = EgressProxy(self.sock_path)
        self.assertFalse(self.sock_path.exists())

        proxy.start()
        self.assertTrue(self.sock_path.exists())
        self.assertTrue(self.sock_path.is_socket())

        # Safe double start
        proxy.start()
        self.assertTrue(self.sock_path.exists())

        proxy.stop()
        self.assertFalse(self.sock_path.exists())

        # Safe double stop
        proxy.stop()
        self.assertFalse(self.sock_path.exists())

    def test_http_connect_to_unauthorized_returns_403(self):
        """CONNECT to unlisted domain immediately returns HTTP 403 Forbidden."""
        with EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"]):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(self.sock_path))
            client.sendall(b"CONNECT google.com:443 HTTP/1.1\r\nHost: google.com:443\r\n\r\n")
            response = client.recv(4096)
            client.close()

            self.assertIn(b"403 Forbidden", response)
            self.assertIn(b"Destination blocked", response)

    def test_http_connect_anti_ssrf_rejection_returns_403(self):
        """CONNECT to cloud metadata or private IP returns HTTP 403 Forbidden."""
        with EgressProxy(self.sock_path):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(self.sock_path))
            client.sendall(b"CONNECT 169.254.169.254:80 HTTP/1.1\r\nHost: 169.254.169.254:80\r\n\r\n")
            response = client.recv(4096)
            client.close()

            self.assertIn(b"403 Forbidden", response)

    def test_plain_http_unauthorized_destination_returns_403(self):
        """Plain HTTP GET to unauthorized target returns HTTP 403 Forbidden."""
        with EgressProxy(self.sock_path):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(self.sock_path))
            client.sendall(b"GET http://yandex.ru/index.html HTTP/1.1\r\nHost: yandex.ru\r\n\r\n")
            response = client.recv(4096)
            client.close()

            self.assertIn(b"403 Forbidden", response)

    def test_http_connect_tunnel_to_authorized_mock(self):
        """Authorized CONNECT creates transparent bidirectional tunnel to upstream."""
        # Host-side mock TCP server (simulates upstream TLS server)
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        upstream_port = upstream.getsockname()[1]

        def mock_upstream_echo():
            conn, _ = upstream.accept()
            data = conn.recv(1024)
            conn.sendall(b"ECHO:" + data)
            conn.close()

        t = threading.Thread(target=mock_upstream_echo, daemon=True)
        t.start()

        # Explicitly allow 127.0.0.1 for this local test fixture
        with EgressProxy(self.sock_path, allowed_hosts=["127.0.0.1"]):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(self.sock_path))
            client.sendall(f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\n\r\n".encode())

            status = client.recv(1024)
            self.assertIn(b"200 Connection Established", status)

            # Transmit arbitrary payload
            client.sendall(b"Hello Encrypted Tunnel Payload")
            echoed = client.recv(1024)
            self.assertEqual(echoed, b"ECHO:Hello Encrypted Tunnel Payload")
            client.close()

        upstream.close()
        t.join(timeout=1.0)

    def test_plain_http_authorized_destination_forwarding(self):
        """Authorized plain HTTP forwards request and delivers response."""
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        upstream_port = upstream.getsockname()[1]

        def mock_http():
            conn, _ = upstream.accept()
            _ = conn.recv(1024)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nHELLO"
            )
            conn.close()

        t = threading.Thread(target=mock_http, daemon=True)
        t.start()

        with EgressProxy(self.sock_path, allowed_hosts=["127.0.0.1"]):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(self.sock_path))
            client.sendall(
                f"GET http://127.0.0.1:{upstream_port}/test HTTP/1.1\r\nHost: 127.0.0.1:{upstream_port}\r\n\r\n".encode()
            )
            resp = client.recv(4096)
            self.assertIn(b"200 OK", resp)
            self.assertIn(b"HELLO", resp)
            client.close()

        upstream.close()
        t.join(timeout=1.0)


# ==============================================================================
# 4. Integration Tests: Config.load and sandbox_command
# ==============================================================================

class TestConfigIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "proj"
        self.project.mkdir()
        self.state = self.root / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_config(self, **overrides) -> Path:
        data = {
            "agent_id": "test-agent",
            "project": str(self.project),
            "state_dir": str(self.state),
            "pi_command": ["/usr/bin/true"],
            "runtime_paths": [],
            "model": "m",
            "provider": "p",
        }
        data.update(overrides)
        path = self.root / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_config_load_network_boolean_and_restricted(self):
        """Config.load parses boolean and 'restricted' network settings."""
        # 1. network: False
        p1 = self._write_config(network=False)
        c1 = Config.load(p1)
        self.assertFalse(c1.network)
        self.assertTrue(c1.is_network_restricted)
        self.assertTrue(is_network_restricted(c1.network))

        # 2. network: "restricted"
        p2 = self._write_config(network="restricted")
        c2 = Config.load(p2)
        self.assertEqual(c2.network, "restricted")
        self.assertTrue(c2.is_network_restricted)
        self.assertTrue(is_network_restricted(c2.network))

        # 3. network: "RESTRICTED"
        p3 = self._write_config(network="RESTRICTED")
        c3 = Config.load(p3)
        self.assertTrue(c3.is_network_restricted)

        # 4. network: True
        p4 = self._write_config(network=True)
        c4 = Config.load(p4)
        self.assertTrue(c4.network)
        self.assertFalse(c4.is_network_restricted)
        self.assertFalse(is_network_restricted(c4.network))

        # 5. network omitted (defaults to True)
        p5 = self._write_config()
        c5 = Config.load(p5)
        self.assertTrue(c5.network)
        self.assertFalse(c5.is_network_restricted)

    def test_config_load_network_invalid_type_rejected(self):
        """Config.load raises RunnerError on invalid network specifications."""
        p_invalid = self._write_config(network=123)
        with self.assertRaises(RunnerError):
            Config.load(p_invalid)

        p_str = self._write_config(network="invalid-policy")
        with self.assertRaises(RunnerError):
            Config.load(p_str)

    def test_config_load_allowed_hosts_parsing(self):
        """Config.load parses and strips allowed_hosts string list."""
        p = self._write_config(allowed_hosts=["eurmtl.me", "  api.z-ai.io  ", "opencode.ai"])
        c = Config.load(p)
        self.assertEqual(c.allowed_hosts, ["eurmtl.me", "api.z-ai.io", "opencode.ai"])

        # Omitted allowed_hosts defaults to empty list
        p_empty = self._write_config()
        c_empty = Config.load(p_empty)
        self.assertEqual(c_empty.allowed_hosts, [])

    def test_config_load_allowed_hosts_invalid_rejected(self):
        """Non-list or non-string allowed_hosts raises RunnerError."""
        p_str = self._write_config(allowed_hosts="eurmtl.me")
        with self.assertRaises(RunnerError):
            Config.load(p_str)

        p_nums = self._write_config(allowed_hosts=["eurmtl.me", 443])
        with self.assertRaises(RunnerError):
            Config.load(p_nums)

    def test_sandbox_command_unshare_net_flag_matrix(self):
        """sandbox_command adds --unshare-net and proxy envs for False and 'restricted'."""
        home = self.state / "home"
        session = self.state / "session"

        # 1. False -> --unshare-net present and proxy envs present
        c_false = Config.load(self._write_config(network=False))
        argv_false = sandbox_command(c_false, home, session)
        self.assertIn("--unshare-net", argv_false)
        self.assertIn("HTTP_PROXY", argv_false)
        self.assertIn("NODE_OPTIONS", argv_false)

        # 2. "restricted" -> --unshare-net present and proxy envs present
        c_restr = Config.load(self._write_config(network="restricted"))
        argv_restr = sandbox_command(c_restr, home, session)
        self.assertIn("--unshare-net", argv_restr)
        self.assertIn("HTTP_PROXY", argv_restr)
        self.assertIn("NODE_OPTIONS", argv_restr)

        # 3. True -> --unshare-net absent
        c_true = Config.load(self._write_config(network=True))
        argv_true = sandbox_command(c_true, home, session)
        self.assertNotIn("--unshare-net", argv_true)
        self.assertNotIn("HTTP_PROXY", argv_true)


# ==============================================================================
# 5. Upstream Integration Tests: Worker & Profile Freezing
# ==============================================================================

def _run_migrations(db_path: Path) -> None:
    from alembic import command
    from alembic.config import Config
    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


class TestUpstreamIntegration(unittest.TestCase):
    def test_task_worker_propagates_allowed_hosts_to_role_json(self):
        """TaskExecutionWorker writes allowed_hosts and restricted network to role.json."""
        from kuzya.storage import Database, AgentProfileRepository, TaskRepository
        from kuzya.task_worker import TaskExecutionWorker
        from kuzya.runtime_defaults import RuntimeDefaultsResolver

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "test.db"
            _run_migrations(db_path)
            db = Database.from_path(db_path)

            profiles = AgentProfileRepository(db)
            profiles.create(
                agent_id="test-sec-worker",
                name="Security Worker",
                role="developer",
                system_prompt="Developer instructions.",
                runner_config={
                    "network": "restricted",
                    "allowed_hosts": ["eurmtl.me", "api.z-ai.io"],
                    "workdir": ".",
                },
            )

            tasks = TaskRepository(db)
            task = tasks.create(
                title="Egress test task",
                status="in_progress",
                workspace=str(root / "workspace"),
                agent_id="test-sec-worker",
                metadata={"task_type": "research"},
            )
            (root / "workspace").mkdir()

            worker = TaskExecutionWorker(
                db,
                root / "artifacts",
                runtime_defaults=RuntimeDefaultsResolver(),
                runner_argv_prefix=[sys.executable, str(Path(__file__).parent / "fake_pi_runner.py")],
            )
            result = worker.step()
            self.assertEqual(result.status, "completed", result.error)

            role_file = next((root / "artifacts").rglob("role.json"))
            role_data = json.loads(role_file.read_text(encoding="utf-8"))

            self.assertEqual(role_data.get("allowed_hosts"), ["eurmtl.me", "api.z-ai.io"])
            self.assertEqual(role_data.get("network"), "restricted")

            stored_task = tasks.get(task.id)
            exec_ctx = stored_task.meta["execution_context"]
            self.assertEqual(exec_ctx.get("allowed_hosts"), ["eurmtl.me", "api.z-ai.io"])
            self.assertEqual(exec_ctx.get("network"), "restricted")

            db.dispose()

    def test_profile_runtime_freezes_allowed_hosts(self):
        """freeze_profile_handler preserves allowed_hosts in runner_role.json & snapshot.json."""
        from kuzya.storage import Database, AgentProfileRepository
        from kuzya.profile_runtime import freeze_profile_handler

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "test.db"
            _run_migrations(db_path)
            db = Database.from_path(db_path)

            profiles = AgentProfileRepository(db)
            profiles.create(
                agent_id="frozen-agent",
                name="Frozen Agent",
                role="worker",
                system_prompt="Prompt",
                runner_config={
                    "network": "restricted",
                    "allowed_hosts": ["eurmtl.me"],
                },
            )

            frozen = freeze_profile_handler(
                database=db,
                agent_id="frozen-agent",
                artifact_root=root / "exports",
                runner_argv_prefix=[sys.executable, str(Path(__file__).parent / "fake_pi_runner.py")],
            )

            role = json.loads(frozen.role_config_path.read_text(encoding="utf-8"))
            self.assertEqual(role.get("allowed_hosts"), ["eurmtl.me"])
            self.assertEqual(role.get("network"), "restricted")

            snapshot = json.loads(frozen.snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["runner_config"].get("allowed_hosts"), ["eurmtl.me"])

            db.dispose()

    def test_knowledge_format_markdown_includes_allowed_hosts(self):
        """format_agent_profile_markdown includes allowed_hosts in runner_config policy."""
        from kuzya.storage import Database, AgentProfileRepository
        from kuzya.knowledge import format_agent_profile_markdown

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            _run_migrations(db_path)
            db = Database.from_path(db_path)
            profiles = AgentProfileRepository(db)
            profile = profiles.create(
                agent_id="doc-agent",
                name="Doc Agent",
                role="worker",
                system_prompt="Prompt",
                runner_config={
                    "network": "restricted",
                    "allowed_hosts": ["eurmtl.me"],
                },
            )
            md = format_agent_profile_markdown(profile)
            self.assertIn('"allowed_hosts": ["eurmtl.me"]', md)
            db.dispose()


# ==============================================================================
# 6. End-to-End Sandbox Tests with Bubblewrap
# ==============================================================================

class TestEndToEndSandboxEgress(unittest.TestCase):
    """Real Bubblewrap container sandbox testing network isolation and proxy."""

    def setUp(self):
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap (bwrap) not found on host")
        if not shutil.which("curl"):
            self.skipTest("curl not found on host")

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session_dir = self.root / "session"
        self.session_dir.mkdir()
        self.sock_path = self.session_dir / "egress_proxy.sock"

    def tearDown(self):
        self.tmp.cleanup()

    def _base_bwrap_args(self) -> list[str]:
        argv = [
            "bwrap", "--unshare-pid", "--unshare-net", "--die-with-parent",
            "--ro-bind", "/usr", "/usr",
        ]
        for name in ("bin", "sbin", "lib", "lib64"):
            p = Path("/") / name
            if p.is_symlink():
                argv += ["--symlink", os.readlink(p), str(p)]
            elif p.exists():
                argv += ["--ro-bind", str(p), str(p)]
        argv += ["--proc", "/proc", "--dev", "/dev"]
        for name in ("ld.so.cache", "ssl/certs", "ca-certificates", "resolv.conf", "hosts", "nsswitch.conf"):
            p = Path("/etc") / name
            if p.exists():
                argv += ["--ro-bind", str(p.resolve()), str(p)]
        return argv

    def test_sandbox_direct_networking_isolated_by_unshare_net(self):
        """Direct network egress from --unshare-net sandbox fails with connection error."""
        bwrap_cmd = [
            *self._base_bwrap_args(),
            "curl", "-s", "--connect-timeout", "2", "http://google.com"
        ]
        res = subprocess.run(bwrap_cmd, capture_output=True, text=True)
        # Direct curl without proxy must fail (exit code != 0, typically 6 or 7)
        self.assertNotEqual(res.returncode, 0)

    def test_sandbox_egress_curl_unauthorized_returns_403(self):
        """In-sandbox curl through egress forwarder to unauthorized destination returns HTTP 403."""
        # 1. Start host-side UDS proxy permitting only eurmtl.me
        proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        proxy.start()
        self.addCleanup(proxy.stop)

        # 2. Copy the actual forwarder.py into the session directory
        forwarder_src = Path(__file__).resolve().parent.parent / "src" / "pi_runner" / "forwarder.py"
        forwarder_script = self.session_dir / "forwarder.py"
        shutil.copy2(forwarder_src, forwarder_script)

        # 3. Execute curl to unauthorized destination inside sandbox via forwarder
        bwrap_cmd = [
            *self._base_bwrap_args(),
            "--bind", str(self.session_dir), "/session",
            "/usr/bin/python3", "/session/forwarder.py",
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "http://google.com"
        ]
        res = subprocess.run(bwrap_cmd, capture_output=True, text=True)
        # curl should receive HTTP 403 Forbidden
        self.assertEqual(res.stdout.strip(), "403")

    def test_sandbox_egress_curl_authorized_destination_connects(self):
        """In-sandbox curl through forwarder to authorized mock target succeeds with 200 OK."""
        # 1. Start mock HTTP upstream on host
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(5)
        port = upstream.getsockname()[1]

        def handle_upstream():
            while True:
                try:
                    conn, _ = upstream.accept()
                    _ = conn.recv(4096)
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/plain\r\n"
                        b"Content-Length: 15\r\n"
                        b"Connection: close\r\n\r\n"
                        b"Authorized Pass"
                    )
                    conn.close()
                except Exception:
                    break

        upstream_thread = threading.Thread(target=handle_upstream, daemon=True)
        upstream_thread.start()

        # 2. Host proxy with 127.0.0.1 allowed for this test
        proxy = EgressProxy(self.sock_path, allowed_hosts=["127.0.0.1"])
        proxy.start()
        self.addCleanup(proxy.stop)

        # 3. Copy the actual forwarder.py into the session directory
        forwarder_src = Path(__file__).resolve().parent.parent / "src" / "pi_runner" / "forwarder.py"
        forwarder_script = self.session_dir / "forwarder.py"
        shutil.copy2(forwarder_src, forwarder_script)

        # 4. In-sandbox curl targeting authorized upstream
        bwrap_cmd = [
            *self._base_bwrap_args(),
            "--bind", str(self.session_dir), "/session",
            "/usr/bin/python3", "/session/forwarder.py",
            "curl", "-s", f"http://127.0.0.1:{port}/api/health"
        ]
        res = subprocess.run(bwrap_cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "Authorized Pass")

        upstream.close()
        upstream_thread.join(timeout=1.0)

    def test_long_session_path_keeps_egress_proxy_within_unix_socket_limit(self):
        """A session path over 140 characters still launches a working EgressProxy.

        The host UDS must live on a short path; only the fixed
        /session/egress_proxy.sock mountpoint may be below the long session
        directory. Before the short-path allocation this raised
        ``AF_UNIX path too long`` from socket.bind().
        """
        import json
        from pi_runner.cli import execute

        # The session itself is intentionally much longer than the 108-byte
        # sockaddr_un.sun_path limit; the proxy socket must not live there.
        long_state = (
            self.root
            / ("long-state-" + "a" * 60)
            / ("nested-" + "b" * 60)
            / "state"
        )
        long_state.mkdir(parents=True)
        self.assertGreater(len(str(long_state)), 140)

        project = self.root / "long-path-project"
        project.mkdir()

        # Local upstream reached by the host-side proxy after the sandbox
        # connects through the forwarder and the bind-mounted UDS.
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(5)
        upstream_port = upstream.getsockname()[1]
        expected_body = b"long-path-egress-ok"

        def handle_upstream():
            while True:
                try:
                    conn, _ = upstream.accept()
                    _ = conn.recv(4096)
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/plain\r\n"
                        + f"Content-Length: {len(expected_body)}\r\n".encode()
                        + b"Connection: close\r\n\r\n"
                        + expected_body
                    )
                    conn.close()
                except Exception:
                    break

        upstream_thread = threading.Thread(target=handle_upstream, daemon=True)
        upstream_thread.start()
        self.addCleanup(upstream.close)

        fake_pi = self.root / "long_path_fake_pi.py"
        fake_pi.write_text(
            "import json, os, pathlib, subprocess\n"
            "sock = pathlib.Path('/session/egress_proxy.sock')\n"
            "assert sock.is_socket(), f'egress UDS missing or not a socket: {sock}'\n"
            f"res = subprocess.run(['curl', '-sS', '--noproxy', '', "
            f"'--connect-timeout', '3', 'http://127.0.0.1:{upstream_port}/health'], "
            "capture_output=True, text=True)\n"
            f"assert res.stdout == {expected_body.decode()!r}, (res.stdout, res.stderr)\n"
            "print(json.dumps({'type': 'session', 'id': 'long-path-session'}), flush=True)\n"
            "print(json.dumps({'type': 'message_end', 'message': {\n"
            "    'role': 'assistant', 'content': [{'type': 'text', 'text': 'verified'}],\n"
            "    'stopReason': 'stop', 'usage': {'input': 1, 'output': 1}}}\n"
            "), flush=True)\n",
            encoding="utf-8",
        )
        task = self.root / "long_path_task.txt"
        task.write_text("verify long-path egress", encoding="utf-8")
        cfg_file = self.root / "long_path_config.json"
        cfg_file.write_text(
            json.dumps({
                "agent_id": "long-path-agent",
                "project": str(project),
                "state_dir": str(long_state),
                "pi_command": ["/usr/bin/python3", str(fake_pi)],
                "runtime_paths": ["/usr/bin/python3", str(fake_pi)],
                "network": "restricted",
                "allowed_hosts": ["127.0.0.1"],
            }),
            encoding="utf-8",
        )

        result = execute(cfg_file, task)
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["exit_code"], 0)

        pi_dir = long_state / "sessions" / result["session_id"] / "pi"
        self.assertFalse((pi_dir / "egress_proxy.sock").exists())
        upstream_thread.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
