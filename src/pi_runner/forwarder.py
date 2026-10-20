#!/usr/bin/env python3
"""In-container loopback TCP to host Unix domain socket egress proxy forwarder.

This script executes INSIDE the Bubblewrap container. It bridges standard
HTTP_PROXY / HTTPS_PROXY TCP traffic on 127.0.0.1 to the host-mounted UDS
proxy at /session/egress_proxy.sock.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time

DEFAULT_UDS_PATH = Path("/session/egress_proxy.sock")
DEFAULT_PORT = 18777
PORT_FILE = Path("/session/proxy_port.txt")
BUFFER_SIZE = 65536


def bridge_sockets(sock_a: socket.socket, sock_b: socket.socket) -> None:
    """Bidirectionally shuttle raw bytes between two sockets until EOF or error."""
    readers = [sock_a, sock_b]
    try:
        while readers:
            rlist, _, xlist = select.select(readers, [], readers, 1.0)
            if xlist:
                break
            for r in rlist:
                w = sock_b if r is sock_a else sock_a
                try:
                    data = r.recv(BUFFER_SIZE)
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
    except OSError:
        pass
    finally:
        for s in (sock_a, sock_b):
            try:
                s.close()
            except OSError:
                pass


def serve_forwarder(
    tcp_server: socket.socket, uds_path: Path, stop_event: threading.Event
) -> None:
    """Accept incoming loopback TCP connections and bridge each to the host UDS."""
    tcp_server.settimeout(0.5)
    while not stop_event.is_set():
        try:
            client_sock, _ = tcp_server.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        try:
            uds_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            uds_sock.connect(str(uds_path))
        except OSError:
            try:
                err_resp = (
                    b"HTTP/1.1 502 Bad Gateway\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Content-Length: 32\r\n"
                    b"Connection: close\r\n\r\n"
                    b"502 Bad Gateway: UDS unavailable\n"
                )
                client_sock.sendall(err_resp)
            except OSError:
                pass
            finally:
                client_sock.close()
            continue

        thread = threading.Thread(
            target=bridge_sockets,
            args=(client_sock, uds_sock),
            daemon=True,
        )
        thread.start()


def wait_for_uds(uds_path: Path, timeout: float = 2.0) -> bool:
    """Wait briefly for the host-side UDS to become available."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if uds_path.exists():
            return True
        time.sleep(0.05)
    return uds_path.exists()


def bind_loopback(preferred_port: int) -> tuple[socket.socket, int]:
    """Bind to 127.0.0.1 on preferred port, falling back to ephemeral port 0."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", preferred_port))
    except OSError:
        server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(128)
    return server, port


def main() -> None:
    parser = argparse.ArgumentParser(
        description="In-container egress proxy loopback forwarder"
    )
    parser.add_argument(
        "--uds",
        type=Path,
        default=DEFAULT_UDS_PATH,
        help="Path to host UDS socket",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Loopback port to bind",
    )
    parser.add_argument(
        "--port-file",
        type=Path,
        default=PORT_FILE,
        help="File to publish allocated port",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run as standalone server without child command",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Inner command to execute",
    )
    args = parser.parse_args()

    # Determine command to run
    child_cmd = list(args.command)
    if child_cmd and child_cmd[0] == "--":
        child_cmd = child_cmd[1:]

    # Bind loopback TCP listener synchronously
    tcp_server, port = bind_loopback(args.port)

    # Write port file
    try:
        args.port_file.parent.mkdir(parents=True, exist_ok=True)
        args.port_file.write_text(str(port), encoding="utf-8")
    except OSError:
        pass

    # Ensure UDS is present
    wait_for_uds(args.uds, timeout=2.0)

    # Start forwarder server thread
    stop_event = threading.Event()
    server_thread = threading.Thread(
        target=serve_forwarder,
        args=(tcp_server, args.uds, stop_event),
        daemon=True,
    )
    server_thread.start()

    # If daemon mode requested without a command, wait for termination
    if args.daemon or not child_cmd:
        def handle_daemon_exit(signum, frame):
            stop_event.set()
            tcp_server.close()
            sys.exit(0)

        signal.signal(signal.SIGTERM, handle_daemon_exit)
        signal.signal(signal.SIGINT, handle_daemon_exit)
        while not stop_event.is_set():
            time.sleep(0.5)
        return

    # In wrapper mode: inject proxy environment variables
    env = dict(os.environ)
    proxy_url = f"http://127.0.0.1:{port}"
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["http_proxy"] = proxy_url
    env["https_proxy"] = proxy_url
    env["ALL_PROXY"] = proxy_url

    node_opts = env.get("NODE_OPTIONS", "")
    if "--use-env-proxy" not in node_opts:
        env["NODE_OPTIONS"] = (node_opts + " --use-env-proxy").strip()

    # Launch target command
    child = subprocess.Popen(child_cmd, env=env)

    def forward_signal(signum, frame):
        if child.poll() is None:
            try:
                child.send_signal(signum)
            except OSError:
                pass

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)

    exit_code = child.wait()

    # Clean shutdown
    stop_event.set()
    tcp_server.close()

    if exit_code < 0:
        sys.exit(128 + abs(exit_code))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
