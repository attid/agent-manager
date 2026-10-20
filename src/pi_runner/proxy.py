"""Host-side Unix Domain Socket (UDS) egress forward proxy.

Enforces strict domain allowlisting (built-in trusted LLM providers + explicit
custom allowed_hosts) and anti-SSRF IP verification for sandboxed runner
processes running with restricted network namespace isolation (--unshare-net).
"""
from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
import select
import socket
import threading
import time
from typing import Iterable
import urllib.parse

logger = logging.getLogger(__name__)

# Default trusted LLM provider domains and wildcards per Milestone 4 specifications
DEFAULT_ALLOWED_DOMAINS: tuple[str, ...] = (
    # OpenCode
    "opencode.ai",
    "*.opencode.ai",
    # ZAI
    "z-ai.io",
    "*.z-ai.io",
    "api.z-ai.io",
    # OpenAI
    "openai.com",
    "*.openai.com",
    "api.openai.com",
    # Anthropic
    "anthropic.com",
    "*.anthropic.com",
    "api.anthropic.com",
    # Google AI / Gemini
    "generativelanguage.googleapis.com",
    "googleapis.com",
    "*.googleapis.com",
    # DeepSeek
    "deepseek.com",
    "*.deepseek.com",
    "api.deepseek.com",
    # OpenRouter
    "openrouter.ai",
    "*.openrouter.ai",
)

# Additional blocked networks beyond standard ipaddress properties
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("100.64.0.0/10"),     # Carrier-Grade NAT (RFC 6598)
    ipaddress.ip_network("198.18.0.0/15"),     # Benchmarking (RFC 2544)
    ipaddress.ip_network("192.0.0.0/24"),      # IETF Protocol Assignments (RFC 6890)
    ipaddress.ip_network("192.0.2.0/24"),      # TEST-NET-1 (RFC 5737)
    ipaddress.ip_network("198.51.100.0/24"),   # TEST-NET-2 (RFC 5737)
    ipaddress.ip_network("203.0.113.0/24"),    # TEST-NET-3 (RFC 5737)
)


class ProxyError(Exception):
    """Base exception for egress proxy operations."""


class DestinationBlockedError(ProxyError):
    """Raised when destination is rejected by policy."""


class SSRFBlockedError(DestinationBlockedError):
    """Raised when destination IP address triggers anti-SSRF protections."""


def is_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check whether an IP address is private, loopback, link-local, multicast, or reserved."""
    # Unpack IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True

    for net in BLOCKED_NETWORKS:
        if ip.version == net.version and ip in net:
            return True

    return False


def normalize_host(host: str) -> str:
    """Normalize hostname or URL: strip schemes, ports, brackets, and trailing dots."""
    h = host.strip()
    if h.startswith("http://") or h.startswith("https://"):
        u = urllib.parse.urlsplit(h)
        h = u.hostname or h
    if h.startswith("[") and "]" in h:
        h = h[1:h.index("]")]
    elif ":" in h and h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.lower().rstrip(".")


def is_domain_match(target_host: str, pattern: str) -> bool:
    """Check if target_host matches a domain pattern or wildcard."""
    target = normalize_host(target_host)
    pat = pattern.lower().strip().rstrip(".")
    if not target or not pat:
        return False
    if target == pat:
        return True
    if pat.startswith("*."):
        suffix = pat[1:]  # e.g. '.openai.com'
        if target.endswith(suffix):
            return True
        if target == pat[2:]:  # apex match for *.example.com -> example.com
            return True
    elif target.endswith("." + pat):
        return True
    return False


class EgressProxy:
    """Host-side UDS HTTP/HTTPS forward proxy with strict allowlisting and anti-SSRF."""

    def __init__(
        self,
        uds_path: str | Path,
        allowed_hosts: Iterable[str] | None = None,
        connection_timeout: float = 10.0,
    ) -> None:
        self.uds_path = Path(uds_path).resolve()
        self.connection_timeout = float(connection_timeout)
        self.allowed_hosts = list(allowed_hosts or [])
        self._explicit_allowed_hosts = {
            h.lower().strip() for h in self.allowed_hosts if h.strip()
        }
        self._allow_patterns = set(DEFAULT_ALLOWED_DOMAINS) | self._explicit_allowed_hosts

        self._server_sock: socket.socket | None = None
        self._server_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._client_sockets: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._is_running = False

    def is_host_allowed(self, target_host: str) -> bool:
        """Evaluate target hostname against built-in and custom allowlist patterns."""
        target = normalize_host(target_host)
        if not target:
            return False
        return any(is_domain_match(target, pat) for pat in self._allow_patterns)

    def _is_ip_explicitly_allowed(self, host: str, ip_str: str) -> bool:
        """Permit specific IP/loopback targets only if explicitly specified by caller."""
        return (
            ip_str in self._explicit_allowed_hosts
            or (host in ("localhost", "127.0.0.1", "::1") and host in self._explicit_allowed_hosts)
        )

    def start(self) -> None:
        """Start proxy listener thread on Unix domain socket."""
        if self._is_running:
            return

        self.uds_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.uds_path.exists() or self.uds_path.is_socket():
                self.uds_path.unlink(missing_ok=True)
        except OSError:
            pass

        self._stop_event.clear()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.uds_path))
            os.chmod(self.uds_path, 0o600)
            sock.listen(128)
            self._server_sock = sock
            self._is_running = True
            self._server_thread = threading.Thread(
                target=self._serve_loop,
                name=f"EgressProxy-{self.uds_path.name}",
                daemon=True,
            )
            self._server_thread.start()
        except Exception:
            sock.close()
            try:
                self.uds_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def stop(self) -> None:
        """Stop proxy listener, terminate active client sockets, and unlink socket file."""
        if not self._is_running:
            return

        self._is_running = False
        self._stop_event.set()

        sock = self._server_sock
        self._server_sock = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

        with self._lock:
            clients = list(self._client_sockets)
            self._client_sockets.clear()

        for s in clients:
            try:
                s.close()
            except OSError:
                pass

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=3.0)
            self._server_thread = None

        try:
            if self.uds_path.exists() or self.uds_path.is_socket():
                self.uds_path.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self) -> "EgressProxy":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def _serve_loop(self) -> None:
        """Background thread accepting client connections on the UDS."""
        while not self._stop_event.is_set():
            sock = self._server_sock
            if not sock:
                break
            try:
                rlist, _, _ = select.select([sock], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not rlist or self._stop_event.is_set():
                continue
            try:
                client_sock, _ = sock.accept()
            except (OSError, ValueError):
                break

            with self._lock:
                self._client_sockets.add(client_sock)

            t = threading.Thread(
                target=self._handle_client_safe,
                args=(client_sock,),
                daemon=True,
            )
            t.start()

    def _handle_client_safe(self, client_sock: socket.socket) -> None:
        try:
            self._handle_client(client_sock)
        except Exception as exc:
            logger.debug("Error handling proxy client: %s", exc)
        finally:
            with self._lock:
                self._client_sockets.discard(client_sock)
            try:
                client_sock.close()
            except OSError:
                pass

    def _read_headers(self, sock: socket.socket) -> tuple[bytes, bytes]:
        """Read until HTTP header terminator, returning (header_bytes, remaining_bytes)."""
        buf = bytearray()
        while not self._stop_event.is_set():
            if b"\r\n\r\n" in buf:
                idx = buf.find(b"\r\n\r\n") + 4
                return bytes(buf[:idx]), bytes(buf[idx:])
            if b"\n\n" in buf:
                idx = buf.find(b"\n\n") + 2
                return bytes(buf[:idx]), bytes(buf[idx:])
            try:
                chunk = sock.recv(4096)
            except (OSError, socket.timeout):
                break
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > 65536:
                raise ValueError("Request headers exceed maximum size (64KB)")
        return bytes(buf), b""

    def _send_response(
        self,
        sock: socket.socket,
        code: int,
        status: str,
        body: str = "",
    ) -> None:
        body_bytes = body.encode("utf-8")
        resp = (
            f"HTTP/1.1 {code} {status}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + body_bytes
        try:
            sock.sendall(resp)
        except OSError:
            pass

    def _resolve_and_verify(
        self, host: str, port: int
    ) -> list[tuple[int, int, int, str, tuple]]:
        """Resolve DNS and verify resolved IPs against anti-SSRF policies."""
        try:
            addr_infos = socket.getaddrinfo(
                host, port, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except socket.gaierror as e:
            raise ProxyError(f"DNS resolution failed for {host}: {e}") from e

        if not addr_infos:
            raise ProxyError(f"No address records found for {host}")

        verified = []
        for family, socktype, proto, canonname, sockaddr in addr_infos:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue

            if self._is_ip_explicitly_allowed(host, ip_str):
                verified.append((family, socktype, proto, canonname, sockaddr))
            elif is_ip_blocked(ip):
                logger.warning("SSRF blocked attempt to reach %s via IP %s", host, ip_str)
                raise SSRFBlockedError(
                    f"Host {host} resolved to blocked IP {ip_str} (anti-SSRF)"
                )
            else:
                verified.append((family, socktype, proto, canonname, sockaddr))

        if not verified:
            raise SSRFBlockedError(
                f"All resolved addresses for {host} are blocked by anti-SSRF"
            )

        return verified

    def _connect_upstream(
        self, endpoints: list[tuple[int, int, int, str, tuple]]
    ) -> socket.socket | None:
        """Attempt connection to one of the verified endpoint addresses."""
        for family, socktype, proto, _, sockaddr in endpoints:
            s = None
            try:
                s = socket.socket(family, socktype, proto)
                s.settimeout(self.connection_timeout)
                s.connect(sockaddr)
                return s
            except OSError:
                if s:
                    s.close()
        return None

    def _handle_client(self, client_sock: socket.socket) -> None:
        client_sock.settimeout(self.connection_timeout)
        headers_bytes, remaining = self._read_headers(client_sock)
        if not headers_bytes:
            return

        header_text = headers_bytes.decode("iso-8859-1")
        lines = header_text.splitlines()
        if not lines:
            return

        parts = lines[0].split()
        if len(parts) < 2:
            self._send_response(client_sock, 400, "Bad Request", "400 Bad Request\n")
            return

        method = parts[0].upper()
        target = parts[1]
        version = parts[2] if len(parts) > 2 else "HTTP/1.1"

        if method == "CONNECT":
            self._handle_connect(client_sock, target, version, remaining)
        else:
            self._handle_plain_http(
                client_sock, method, target, version, lines, remaining
            )

    def _handle_connect(
        self,
        client_sock: socket.socket,
        target: str,
        version: str,
        remaining: bytes,
    ) -> None:
        if target.startswith("[") and "]" in target:
            host = target[1:target.index("]")]
            rest = target[target.index("]") + 1 :]
            port = int(rest.lstrip(":")) if rest.startswith(":") else 443
        elif ":" in target:
            host, port_str = target.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                self._send_response(
                    client_sock, 400, "Bad Request", "400 Bad Request: Invalid port\n"
                )
                return
        else:
            host = target
            port = 443

        if not self.is_host_allowed(host):
            self._send_response(
                client_sock, 403, "Forbidden", "403 Forbidden: Destination blocked\n"
            )
            return

        try:
            endpoints = self._resolve_and_verify(host, port)
        except SSRFBlockedError:
            self._send_response(
                client_sock, 403, "Forbidden", "403 Forbidden: Destination blocked (anti-SSRF)\n"
            )
            return
        except Exception:
            self._send_response(
                client_sock, 502, "Bad Gateway", "502 Bad Gateway: Resolution failed\n"
            )
            return

        remote = self._connect_upstream(endpoints)
        if not remote:
            self._send_response(
                client_sock, 502, "Bad Gateway", "502 Bad Gateway: Connection failed\n"
            )
            return

        try:
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if remaining:
                remote.sendall(remaining)
        except OSError:
            remote.close()
            return

        # Disable timeouts during raw bidirectional streaming
        client_sock.settimeout(None)
        remote.settimeout(None)
        self._tunnel(client_sock, remote)

    def _handle_plain_http(
        self,
        client_sock: socket.socket,
        method: str,
        target: str,
        version: str,
        lines: list[str],
        remaining: bytes,
    ) -> None:
        host = None
        port = 80
        path = target

        if target.startswith("http://") or target.startswith("https://"):
            u = urllib.parse.urlsplit(target)
            host = u.hostname
            port = u.port or (443 if u.scheme == "https" else 80)
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
        else:
            for line in lines[1:]:
                if line.lower().startswith("host:"):
                    h_val = line.split(":", 1)[1].strip()
                    if h_val.startswith("[") and "]" in h_val:
                        host = h_val[1:h_val.index("]")]
                        rest = h_val[h_val.index("]") + 1 :]
                        if rest.startswith(":"):
                            port = int(rest.lstrip(":"))
                    elif ":" in h_val:
                        host, p_str = h_val.rsplit(":", 1)
                        try:
                            port = int(p_str)
                        except ValueError:
                            port = 80
                    else:
                        host = h_val
                    break

        if not host:
            self._send_response(
                client_sock, 400, "Bad Request", "400 Bad Request: Missing Host header\n"
            )
            return

        if not self.is_host_allowed(host):
            self._send_response(
                client_sock, 403, "Forbidden", "403 Forbidden: Destination blocked\n"
            )
            return

        try:
            endpoints = self._resolve_and_verify(host, port)
        except SSRFBlockedError:
            self._send_response(
                client_sock, 403, "Forbidden", "403 Forbidden: Destination blocked (anti-SSRF)\n"
            )
            return
        except Exception:
            self._send_response(
                client_sock, 502, "Bad Gateway", "502 Bad Gateway: Resolution failed\n"
            )
            return

        remote = self._connect_upstream(endpoints)
        if not remote:
            self._send_response(
                client_sock, 502, "Bad Gateway", "502 Bad Gateway: Connection failed\n"
            )
            return

        # Rewrite request line to origin-form path
        lines[0] = f"{method} {path} {version}"
        forward_headers = ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")

        try:
            remote.sendall(forward_headers)
            if remaining:
                remote.sendall(remaining)
        except OSError:
            remote.close()
            return

        client_sock.settimeout(None)
        remote.settimeout(None)
        self._tunnel(client_sock, remote)

    def _tunnel(self, s1: socket.socket, s2: socket.socket) -> None:
        """Bidirectional raw byte streaming between two sockets with half-close support."""
        readers = [s1, s2]
        try:
            while readers and not self._stop_event.is_set():
                rlist, _, xlist = select.select(readers, [], readers, 0.5)
                if xlist:
                    break
                for r in rlist:
                    w = s2 if r is s1 else s1
                    try:
                        data = r.recv(65536)
                    except OSError:
                        data = b""
                    if not data:
                        if r in readers:
                            readers.remove(r)
                        try:
                            w.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                    else:
                        try:
                            w.sendall(data)
                        except OSError:
                            readers.clear()
                            break
        finally:
            for s in (s1, s2):
                try:
                    s.close()
                except OSError:
                    pass


def main() -> None:
    """CLI entrypoint for manual verification or standalone debugging."""
    import argparse

    parser = argparse.ArgumentParser(description="Host-Side UDS Egress Proxy")
    parser.add_argument("socket_path", help="Path to Unix Domain Socket")
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        help="Allowed host/domain (can be specified multiple times)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Connection timeout in seconds",
    )
    args = parser.parse_args()

    proxy = EgressProxy(
        args.socket_path, allowed_hosts=args.allow, connection_timeout=args.timeout
    )
    proxy.start()
    print(f"EgressProxy listening on {args.socket_path} (allowed: {args.allow})")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        proxy.stop()
        print("EgressProxy stopped.")


if __name__ == "__main__":
    main()
