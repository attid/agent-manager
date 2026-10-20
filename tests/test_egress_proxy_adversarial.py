"""Adversarial challenge and stress test suite for EgressProxy (Requirement R1).

Validates:
1. Domain evasion vectors:
   - Subdomain spoofing / lookalike domains (evil-openai.com, openaixcom, myanthropic.com,
     api.openai.com.attacker.com, eurmtl.me.evil.com, deepseek.com.phishing.io)
   - Case insensitivity (OpEnAi.CoM, EURMTL.ME)
   - Trailing dots (api.openai.com., eurmtl.me.)
   - Embedded credentials in CONNECT / Host header (user:pass@google.com:443)
   - Malformed HTTP requests (garbage, missing host, invalid port, oversized headers)
   - Verification that ALL unauthorized destinations receive HTTP/1.1 403 Forbidden
     and NO upstream TCP socket is established.
2. Anti-SSRF vectors:
   - Loopback IPs: 127.0.0.1, 127.0.1.1, 127.255.255.254, [::1], [::ffff:127.0.0.1]
   - Private subnets: 10.0.0.1, 172.16.0.1, 192.168.1.1, [fc00::1]
   - Link-local and cloud metadata: 169.254.169.254, 169.254.1.1, [fe80::1]
   - CGNAT: 100.64.0.1
   - Direct IP requests and DNS-rebinding attacks resolving to these IPs.
3. Allowed destinations:
   - Verification that allowed destinations cleanly receive HTTP/1.1 200 Connection Established
     and stream bidirectional data with integrity.
4. Concurrency stress:
   - 50 concurrent requests through UDS proxy with zero deadlocks, zero descriptor leaks,
     and zero unhandled exceptions.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import pytest

from pi_runner.proxy import (
    DEFAULT_ALLOWED_DOMAINS,
    EgressProxy,
    is_domain_match,
    is_ip_blocked,
    normalize_host,
)


def _get_open_fd_count() -> int:
    """Return count of open file descriptors in current process."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return 0


class TestDomainEvasionVectors(unittest.TestCase):
    """Adversarial testing of domain parsing, lookalike spoofing, and evasion."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock_path = Path(self.tmp.name) / "egress_proxy.sock"
        self.proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        self.proxy.start()

    def tearDown(self):
        self.proxy.stop()
        self.tmp.cleanup()

    def test_subdomain_spoofing_and_lookalike_domains_blocked(self):
        """Spoofed subdomains and lookalike domains receive 403 and never connect upstream."""
        evasion_targets = [
            "evil-openai.com",
            "openaixcom",
            "myanthropic.com",
            "api.openai.com.attacker.com",
            "eurmtl.me.evil.com",
            "deepseek.com.phishing.io",
            "openai.com.evil.org",
            "evil-anthropic.com",
            "notopencode.ai",
        ]

        with mock.patch.object(
            self.proxy, "_connect_upstream", wraps=self.proxy._connect_upstream
        ) as spy_connect:
            for domain in evasion_targets:
                # 1. CONNECT request
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(self.sock_path))
                client.sendall(
                    f"CONNECT {domain}:443 HTTP/1.1\r\nHost: {domain}:443\r\n\r\n".encode()
                )
                resp = client.recv(4096)
                client.close()

                first_line = resp.decode("iso-8859-1").splitlines()[0] if resp else ""
                self.assertIn(
                    "HTTP/1.1 403 Forbidden",
                    first_line,
                    f"Target {domain} should receive 403 Forbidden, got {first_line}",
                )

                # 2. Plain HTTP request
                client_http = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client_http.connect(str(self.sock_path))
                client_http.sendall(
                    f"GET http://{domain}/index.html HTTP/1.1\r\nHost: {domain}\r\n\r\n".encode()
                )
                resp_http = client_http.recv(4096)
                client_http.close()

                first_line_http = (
                    resp_http.decode("iso-8859-1").splitlines()[0] if resp_http else ""
                )
                self.assertIn(
                    "HTTP/1.1 403 Forbidden",
                    first_line_http,
                    f"Plain HTTP to {domain} should receive 403 Forbidden",
                )

            # Ensure zero upstream connections were attempted
            self.assertEqual(
                spy_connect.call_count,
                0,
                "No upstream connection should ever be attempted for unauthorized domains",
            )

    def test_case_insensitivity_in_allowed_domains(self):
        """Case variations of allowed domains are correctly permitted."""
        test_cases = [
            ("OpEnAi.CoM", "openai.com"),
            ("EURMTL.ME", "eurmtl.me"),
            ("aPi.OpEnAi.cOm", "openai.com"),
            ("DeEpSeEk.CoM", "deepseek.com"),
            ("OpEnRoUtEr.Ai", "openrouter.ai"),
        ]

        for case_variant, base_name in test_cases:
            self.assertTrue(
                self.proxy.is_host_allowed(case_variant),
                f"Case variant {case_variant} should be permitted",
            )

        # End-to-end verification through proxy with mock upstream
        mock_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        mock_srv.bind(("127.0.0.1", 0))
        mock_srv.listen(1)
        srv_port = mock_srv.getsockname()[1]

        def srv_worker():
            conn, _ = mock_srv.accept()
            conn.sendall(b"OK_CASE_CHECK")
            conn.close()

        t = threading.Thread(target=srv_worker, daemon=True)
        t.start()

        try:
            with mock.patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", srv_port))]):
                with mock.patch.object(self.proxy, "_is_ip_explicitly_allowed", return_value=True):
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    client.connect(str(self.sock_path))
                    client.sendall(f"CONNECT OpEnAi.CoM:{srv_port} HTTP/1.1\r\n\r\n".encode())
                    resp = client.recv(1024)
                    payload = client.recv(1024)
                    client.close()

                    self.assertIn(b"200 Connection Established", resp)
                    self.assertEqual(payload, b"OK_CASE_CHECK")
        finally:
            mock_srv.close()

    def test_trailing_dots_handling(self):
        """Trailing dots are properly handled: allowed domains match, blocked domains rejected."""
        self.assertTrue(self.proxy.is_host_allowed("api.openai.com."))
        self.assertTrue(self.proxy.is_host_allowed("eurmtl.me."))
        self.assertFalse(self.proxy.is_host_allowed("evil-openai.com."))
        self.assertFalse(self.proxy.is_host_allowed("attacker.com."))

        # Verify CONNECT to unauthorized domain with trailing dot receives 403
        with mock.patch.object(
            self.proxy, "_connect_upstream", wraps=self.proxy._connect_upstream
        ) as spy_connect:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(self.sock_path))
            client.sendall(b"CONNECT evil-openai.com.:443 HTTP/1.1\r\n\r\n")
            resp = client.recv(1024)
            client.close()

            self.assertIn(b"403 Forbidden", resp)
            self.assertEqual(spy_connect.call_count, 0)

    def test_embedded_credentials_handling(self):
        """Embedded credentials in CONNECT or Host header do not bypass authorization."""
        test_payloads = [
            b"CONNECT user:pass@google.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT user:pass@evil.com:443 HTTP/1.1\r\n\r\n",
            b"GET http://user:pass@google.com:443/ HTTP/1.1\r\n\r\n",
            b"GET / HTTP/1.1\r\nHost: user:pass@google.com:443\r\n\r\n",
        ]

        with mock.patch.object(
            self.proxy, "_connect_upstream", wraps=self.proxy._connect_upstream
        ) as spy_connect:
            for payload in test_payloads:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(self.sock_path))
                client.sendall(payload)
                resp = client.recv(1024)
                client.close()

                first_line = resp.decode("iso-8859-1").splitlines()[0] if resp else ""
                self.assertIn(
                    "HTTP/1.1 403 Forbidden",
                    first_line,
                    f"Payload {payload[:30]!r} should be rejected with 403, got {first_line}",
                )

            self.assertEqual(spy_connect.call_count, 0)

    def test_malformed_http_requests_rejected_no_upstream(self):
        """Malformed, truncated, or invalid HTTP requests do not cause crashes or upstream connections."""
        malformed_cases = [
            ("garbage_binary", b"\x00\xff\xfe\xfd\r\n\r\n"),
            ("single_token", b"INVALIDREQUEST\r\n\r\n"),
            ("invalid_port", b"CONNECT openai.com:notaport HTTP/1.1\r\n\r\n"),
            ("missing_host", b"GET /path/to/resource HTTP/1.1\r\n\r\n"),
            ("oversized_header", b"GET / HTTP/1.1\r\nHost: " + b"a" * 70000 + b"\r\n\r\n"),
        ]

        with mock.patch.object(
            self.proxy, "_connect_upstream", wraps=self.proxy._connect_upstream
        ) as spy_connect:
            for name, payload in malformed_cases:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(2.0)
                client.connect(str(self.sock_path))
                client.sendall(payload)
                try:
                    resp = client.recv(1024)
                    first_line = resp.decode("iso-8859-1").splitlines()[0] if resp else "<closed>"
                except OSError:
                    first_line = "<reset_or_closed>"
                finally:
                    client.close()

                # Verify either 400 Bad Request or cleanly closed socket, but NEVER 200 or upstream call
                self.assertTrue(
                    "400 Bad Request" in first_line
                    or "<closed>" in first_line
                    or "<reset_or_closed>" in first_line,
                    f"Malformed case {name} gave unexpected status: {first_line}",
                )

            self.assertEqual(
                spy_connect.call_count,
                0,
                "No upstream connection should be attempted for malformed requests",
            )


class TestAntiSSRFVectors(unittest.TestCase):
    """Adversarial testing of SSRF protections across IPv4/IPv6 private, loopback, CGNAT, metadata."""

    SSRF_TARGETS = [
        # Loopback IPs
        ("127.0.0.1", "IPv4 loopback"),
        ("127.0.1.1", "IPv4 alternate loopback"),
        ("127.255.255.254", "IPv4 high loopback"),
        ("::1", "IPv6 loopback"),
        ("::ffff:127.0.0.1", "IPv4-mapped IPv6 loopback"),
        # Private subnets
        ("10.0.0.1", "RFC 1918 10/8"),
        ("172.16.0.1", "RFC 1918 172.16/12"),
        ("192.168.1.1", "RFC 1918 192.168/16"),
        ("fc00::1", "RFC 4193 Unique Local IPv6"),
        # Link-local and cloud metadata
        ("169.254.169.254", "AWS/GCP instance metadata"),
        ("169.254.1.1", "IPv4 link-local"),
        ("fe80::1", "IPv6 link-local"),
        # CGNAT
        ("100.64.0.1", "Carrier-Grade NAT (RFC 6598)"),
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock_path = Path(self.tmp.name) / "egress_proxy.sock"
        self.proxy = EgressProxy(self.sock_path, allowed_hosts=["eurmtl.me"])
        self.proxy.start()

    def tearDown(self):
        self.proxy.stop()
        self.tmp.cleanup()

    def test_is_ip_blocked_evaluates_all_ssrf_vectors(self):
        """is_ip_blocked identifies all loopback, private, link-local, metadata, and CGNAT IPs."""
        for ip_str, desc in self.SSRF_TARGETS:
            ip = ipaddress.ip_address(ip_str)
            self.assertTrue(
                is_ip_blocked(ip),
                f"SSRF target {ip_str} ({desc}) MUST be blocked by is_ip_blocked",
            )

    def test_direct_ip_connect_requests_blocked_with_403(self):
        """Direct CONNECT to SSRF IP addresses returns 403 Forbidden without upstream connection."""
        with mock.patch.object(
            self.proxy, "_connect_upstream", wraps=self.proxy._connect_upstream
        ) as spy_connect:
            for ip_str, desc in self.SSRF_TARGETS:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(self.sock_path))

                # Handle IPv6 bracket formatting for CONNECT host:port
                target = f"[{ip_str}]:80" if ":" in ip_str else f"{ip_str}:80"
                client.sendall(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
                resp = client.recv(1024)
                client.close()

                first_line = resp.decode("iso-8859-1").splitlines()[0] if resp else ""
                self.assertIn(
                    "HTTP/1.1 403 Forbidden",
                    first_line,
                    f"Direct CONNECT to {target} ({desc}) should receive 403 Forbidden, got {first_line}",
                )

            self.assertEqual(
                spy_connect.call_count,
                0,
                "No upstream socket should ever be created for direct SSRF IP requests",
            )

    def test_dns_rebinding_ssrf_blocked_with_403(self):
        """Allowed domain resolving to private/metadata IP (DNS rebinding) is blocked with 403."""
        for ip_str, desc in self.SSRF_TARGETS:
            with mock.patch.object(
                self.proxy, "_connect_upstream", wraps=self.proxy._connect_upstream
            ) as spy_connect:
                # Mock DNS resolving an allowed domain (api.openai.com) to the SSRF target IP
                family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
                sockaddr = (ip_str, 443, 0, 0) if family == socket.AF_INET6 else (ip_str, 443)
                mock_addrinfo = [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

                with mock.patch("socket.getaddrinfo", return_value=mock_addrinfo):
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    client.connect(str(self.sock_path))
                    client.sendall(b"CONNECT api.openai.com:443 HTTP/1.1\r\n\r\n")
                    resp = client.recv(1024)
                    client.close()

                    first_line = resp.decode("iso-8859-1").splitlines()[0] if resp else ""
                    self.assertIn(
                        "HTTP/1.1 403 Forbidden",
                        first_line,
                        f"DNS rebinding to {ip_str} ({desc}) must receive 403 Forbidden, got {first_line}",
                    )
                    self.assertIn(b"anti-SSRF", resp)

                self.assertEqual(
                    spy_connect.call_count,
                    0,
                    f"No upstream connection should be created when resolving to {ip_str}",
                )


class TestAllowedDestinations(unittest.TestCase):
    """Verification of authorized destinations and bidirectional data streaming."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock_path = Path(self.tmp.name) / "egress_proxy.sock"
        # Explicitly allow 127.0.0.1 and eurmtl.me for local mock testing
        self.proxy = EgressProxy(
            self.sock_path, allowed_hosts=["127.0.0.1", "eurmtl.me"]
        )
        self.proxy.start()

    def tearDown(self):
        self.proxy.stop()
        self.tmp.cleanup()

    def test_allowed_destination_receives_200_and_streams_bidirectionally(self):
        """Allowed CONNECT destination receives 200 Connection Established and streams data."""
        # Setup mock echo server
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server_port = server.getsockname()[1]

        def echo_server_worker():
            conn, _ = server.accept()
            try:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    conn.sendall(data)
            finally:
                conn.close()

        server_thread = threading.Thread(target=echo_server_worker, daemon=True)
        server_thread.start()

        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(self.sock_path))
            client.sendall(f"CONNECT 127.0.0.1:{server_port} HTTP/1.1\r\n\r\n".encode())

            # Verify 200 Connection Established header
            resp = client.recv(1024)
            self.assertIn(b"HTTP/1.1 200 Connection Established\r\n\r\n", resp)

            # Bidirectional streaming of multi-block binary payload
            test_payload = b"ADVERSARIAL_STREAM_PAYLOAD_" * 500  # ~13.5 KB
            client.sendall(test_payload)

            received = bytearray()
            while len(received) < len(test_payload):
                chunk = client.recv(4096)
                if not chunk:
                    break
                received.extend(chunk)

            self.assertEqual(bytes(received), test_payload)

            # Clean shutdown test
            client.shutdown(socket.SHUT_WR)
            remainder = client.recv(1024)
            self.assertEqual(remainder, b"")
            client.close()
        finally:
            server.close()


class TestConcurrencyStress(unittest.TestCase):
    """Stress testing EgressProxy under concurrent load."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock_path = Path(self.tmp.name) / "egress_proxy.sock"
        self.proxy = EgressProxy(self.sock_path, allowed_hosts=["127.0.0.1"])
        self.proxy.start()

    def tearDown(self):
        self.proxy.stop()
        self.tmp.cleanup()

    def test_fifty_concurrent_requests_no_deadlocks_no_leaks(self):
        """Execute 50 simultaneous requests; verify no deadlocks, exceptions, or descriptor leaks."""
        # Setup multi-client echo server
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(64)
        server_port = server.getsockname()[1]

        running = True

        def multi_echo_server():
            while running:
                try:
                    server.settimeout(0.5)
                    conn, _ = server.accept()
                except (socket.timeout, OSError):
                    continue

                def handle_conn(c: socket.socket):
                    try:
                        while True:
                            data = c.recv(4096)
                            if not data:
                                break
                            c.sendall(data)
                    except OSError:
                        pass
                    finally:
                        c.close()

                threading.Thread(target=handle_conn, args=(conn,), daemon=True).start()

        srv_thread = threading.Thread(target=multi_echo_server, daemon=True)
        srv_thread.start()
        time.sleep(0.1)

        initial_fd_count = _get_open_fd_count()

        num_requests = 50
        barrier = threading.Barrier(num_requests)
        results = [None] * num_requests

        def worker(idx: int):
            barrier.wait()  # Synchronize launch across all 50 threads
            is_allowed = (idx % 2 == 0)
            target = f"127.0.0.1:{server_port}" if is_allowed else "unauthorized-phishing.evil:443"

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            try:
                sock.connect(str(self.sock_path))
                sock.sendall(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
                header = sock.recv(1024)

                if is_allowed:
                    if b"200 Connection Established" not in header:
                        results[idx] = f"Expected 200, got: {header[:50]!r}"
                        return
                    # Send payload and verify echo
                    payload = f"CONCURRENT_TEST_PAYLOAD_{idx}_".encode() * 50
                    sock.sendall(payload)
                    recvd = bytearray()
                    while len(recvd) < len(payload):
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        recvd.extend(chunk)
                    if bytes(recvd) != payload:
                        results[idx] = f"Payload mismatch on worker {idx}"
                        return
                    results[idx] = "SUCCESS_ALLOWED"
                else:
                    if b"403 Forbidden" not in header:
                        results[idx] = f"Expected 403, got: {header[:50]!r}"
                        return
                    results[idx] = "SUCCESS_BLOCKED"
            except Exception as e:
                results[idx] = f"Exception: {e}"
            finally:
                sock.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_requests) as executor:
            futures = [executor.submit(worker, i) for i in range(num_requests)]
            concurrent.futures.wait(futures, timeout=30.0)

        running = False
        server.close()

        # Check results
        allowed_successes = sum(1 for r in results if r == "SUCCESS_ALLOWED")
        blocked_successes = sum(1 for r in results if r == "SUCCESS_BLOCKED")
        failures = [r for r in results if r not in ("SUCCESS_ALLOWED", "SUCCESS_BLOCKED")]

        self.assertEqual(
            allowed_successes,
            25,
            f"Expected 25 successful allowed connections, got {allowed_successes}. Failures: {failures}",
        )
        self.assertEqual(
            blocked_successes,
            25,
            f"Expected 25 successful blocked connections, got {blocked_successes}. Failures: {failures}",
        )
        self.assertEqual(len(failures), 0, f"Failures encountered: {failures}")

        # Check proxy server thread health
        self.assertTrue(
            self.proxy._server_thread.is_alive(),
            "Proxy server thread died during concurrent stress test",
        )

        # Allow brief grace period for socket descriptor cleanup
        time.sleep(1.0)
        final_fd_count = _get_open_fd_count()
        if initial_fd_count > 0:
            fd_delta = final_fd_count - initial_fd_count
            self.assertLessEqual(
                fd_delta,
                2,
                f"Possible file descriptor leak: {initial_fd_count} -> {final_fd_count} (delta: {fd_delta})",
            )
