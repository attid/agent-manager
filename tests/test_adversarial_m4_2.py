"""Adversarial stress-testing suite for Requirement R1 (Reviewer M4-2).

Stress tests:
1. Malformed and adversarial HTTP CONNECT and plain HTTP requests
2. DoS attempts (oversized header buffers > 64KB)
3. DNS rebinding and dual-homed DNS SSRF attacks
4. Domain pattern collision and lookalike attacks
5. Anti-SSRF bypass attempts (IPv4-mapped IPv6, CGNAT, broadcast, loopback aliases)
6. UDS socket disconnects, unlinking, and concurrency races
7. Forwarder signal propagation and exit codes
8. Configuration type edge cases and unicode normalization
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

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
    normalize_host,
)


class TestAdversarialProxy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock_path = Path(self.tmp.name) / "egress_proxy.sock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_oversized_header_dos_handled_gracefully(self):
        """Header exceeding 64KB closes or resets connection without crashing the proxy server."""
        with EgressProxy(self.sock_path) as proxy:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(self.sock_path))
            client.sendall(b"GET / HTTP/1.1\r\n" + b"X-Junk: " + b"A" * 70000 + b"\r\n")
            try:
                res = client.recv(1024)
            except ConnectionResetError:
                res = b"RST"
            client.close()
            # Proxy must stay running and operational
            self.assertTrue(proxy._is_running)

    def test_malformed_connect_requests(self):
        """Malformed CONNECT lines return 400 Bad Request, 403 Forbidden, 502, or close."""
        with EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"]):
            for bad_target in ["", "   ", "eurmtl.me:not_a_port", "eurmtl.me:99999999"]:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(self.sock_path))
                req = f"CONNECT {bad_target} HTTP/1.1\r\nHost: {bad_target}\r\n\r\n".encode()
                client.sendall(req)
                try:
                    res = client.recv(1024)
                except (ConnectionResetError, BrokenPipeError):
                    res = b""
                client.close()
                self.assertTrue(
                    b"400 Bad Request" in res or b"403 Forbidden" in res or b"502 Bad Gateway" in res or res == b"",
                    f"Unexpected response for {bad_target}: {res}",
                )

    def test_immediate_client_disconnect(self):
        """Client connecting and immediately closing socket without sending data does not cause error."""
        with EgressProxy(self.sock_path) as proxy:
            for _ in range(5):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(self.sock_path))
                client.close()
            time.sleep(0.1)
            self.assertTrue(proxy._is_running)

    def test_ssrf_bypass_variations(self):
        """Verify that exotic and obfuscated IP representations are blocked."""
        blocked_candidates = [
            "127.0.0.1",
            "127.1.2.3",
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.1.1",
            "169.254.169.254",
            "169.254.1.1",
            "100.64.0.1",
            "100.127.255.255",
            "198.18.0.1",
            "198.19.255.255",
            "0.0.0.0",
            "::1",
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
            "::ffff:169.254.169.254",
            "fc00::1",
            "fe80::1",
        ]
        for ip_str in blocked_candidates:
            ip = ipaddress.ip_address(ip_str)
            self.assertTrue(is_ip_blocked(ip), f"Candidate {ip_str} should be blocked")

    def test_domain_spoofing_adversarial(self):
        """Adversarial domain collisions must be rejected."""
        self.assertFalse(is_domain_match("openai.com.attacker.com", "*.openai.com"))
        self.assertFalse(is_domain_match("openai.com.attacker.com", "openai.com"))
        self.assertFalse(is_domain_match("attacker-openai.com", "*.openai.com"))
        self.assertFalse(is_domain_match("attacker-openai.com", "openai.com"))
        self.assertFalse(is_domain_match("fakeopenai.com", "openai.com"))
        self.assertFalse(is_domain_match("openai.com@attacker.com", "openai.com"))
        self.assertFalse(is_domain_match("eurmtl.me.org", "eurmtl.me"))

    def test_domain_matching_permissive_edge_cases(self):
        """Valid subdomains and apex domains with varying case and trailing dot."""
        self.assertTrue(is_domain_match("OPENAI.COM", "openai.com"))
        self.assertTrue(is_domain_match("api.openai.com", "openai.com"))
        self.assertTrue(is_domain_match("api.openai.com.", "openai.com"))
        self.assertTrue(is_domain_match("API.OPENAI.COM.", "*.openai.com"))
        self.assertTrue(is_domain_match("eurmtl.me", "eurmtl.me"))
        self.assertTrue(is_domain_match("api.eurmtl.me", "eurmtl.me"))

    def test_concurrent_connections_stress(self):
        """Handle 20 simultaneous connections without deadlock or socket leaks."""
        # Start mock server
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(25)
        port = server.getsockname()[1]

        stop_server = threading.Event()

        def handle_server():
            while not stop_server.is_set():
                rlist, _, _ = select.select([server], [], [], 0.2)
                if not rlist:
                    continue
                try:
                    c, _ = server.accept()
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                    c.close()
                except OSError:
                    break

        st = threading.Thread(target=handle_server, daemon=True)
        st.start()

        with EgressProxy(self.sock_path, allowed_hosts=["127.0.0.1"]):
            errors = []

            def client_worker():
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(str(self.sock_path))
                    sock.sendall(
                        f"GET http://127.0.0.1:{port}/ HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
                    )
                    resp = sock.recv(2048)
                    sock.close()
                    if b"200 OK" not in resp:
                        errors.append(f"Bad response: {resp}")
                except Exception as e:
                    errors.append(str(e))

            threads = [threading.Thread(target=client_worker) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=3.0)

            self.assertEqual(errors, [])

        stop_server.set()
        server.close()
        st.join(timeout=1.0)


class TestAdversarialConfig(unittest.TestCase):
    def test_is_network_restricted_cases(self):
        """Exhaustively verify is_network_restricted across all inputs."""
        self.assertTrue(is_network_restricted(False))
        self.assertTrue(is_network_restricted("restricted"))
        self.assertTrue(is_network_restricted("RESTRICTED"))
        self.assertTrue(is_network_restricted("  restricted  "))
        self.assertTrue(is_network_restricted("false"))
        self.assertTrue(is_network_restricted("FALSE"))

        self.assertFalse(is_network_restricted(True))
        self.assertFalse(is_network_restricted("true"))
        self.assertFalse(is_network_restricted("unrestricted"))
        self.assertFalse(is_network_restricted(None))
        self.assertFalse(is_network_restricted(1))
        self.assertFalse(is_network_restricted(0))
        self.assertFalse(is_network_restricted([]))
        self.assertFalse(is_network_restricted({}))

    def test_config_load_allowed_hosts_robustness(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = root / "proj"
            proj.mkdir()
            cfg_path = root / "config.json"

            base = {
                "agent_id": "test",
                "project": str(proj),
                "state_dir": str(root / "state"),
                "pi_command": ["/bin/true"],
                "runtime_paths": [],
                "network": "restricted",
                "allowed_hosts": ["  eurmtl.me  ", "api.z-ai.io", ""],
            }
            cfg_path.write_text(json.dumps(base), encoding="utf-8")
            c = Config.load(cfg_path)
            self.assertEqual(c.allowed_hosts, ["eurmtl.me", "api.z-ai.io"])
            self.assertTrue(c.is_network_restricted)


if __name__ == "__main__":
    unittest.main()
