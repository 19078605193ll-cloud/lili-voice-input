from __future__ import annotations

import argparse
import concurrent.futures
import json
import socket
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path


CONTROL_TARGETS = (
    ("cloudflare_control", "www.cloudflare.com", "/cdn-cgi/trace"),
    ("global_control", "www.microsoft.com", "/"),
    ("domestic_control", "www.baidu.com", "/"),
)


def classify_error(exc: BaseException) -> tuple[str, int | None]:
    message = str(exc).lower()
    error_number = exc.errno if isinstance(exc, OSError) else None
    if error_number in {-3, -2} or any(
        marker in message for marker in ("name resolution", "getaddrinfo", "temporary failure in name")
    ):
        return "dns_resolution", error_number
    if "certificate" in message or "tls" in message or "ssl" in message:
        return "tls_handshake", error_number
    if "refused" in message:
        return "tcp_refused", error_number
    if "reset" in message:
        return "connection_reset", error_number
    if "unreachable" in message or "no route" in message:
        return "network_unreachable", error_number
    if "timed out" in message or "timeout" in message:
        return "timeout", error_number
    return type(exc).__name__, error_number


def probe(target: str, host: str, path: str, timeout_seconds: float) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "target": target,
        "host": host,
        "dns_ok": False,
        "tcp_ok": False,
        "tls_ok": False,
        "http_ok": False,
        "dns_ms": None,
        "tcp_ms": None,
        "tls_ms": None,
        "http_ms": None,
        "http_status": None,
        "error_stage": None,
        "error_kind": None,
        "exception_type": None,
        "os_errno": None,
    }
    stage = "dns"
    sock: socket.socket | None = None
    tls_sock: ssl.SSLSocket | None = None
    try:
        started = time.perf_counter()
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        row["dns_ms"] = round((time.perf_counter() - started) * 1000, 2)
        row["dns_ok"] = True

        family, socket_type, protocol, _, address = addresses[0]
        stage = "tcp"
        started = time.perf_counter()
        sock = socket.socket(family, socket_type, protocol)
        sock.settimeout(timeout_seconds)
        sock.connect(address)
        row["tcp_ms"] = round((time.perf_counter() - started) * 1000, 2)
        row["tcp_ok"] = True

        stage = "tls"
        started = time.perf_counter()
        tls_sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock = None
        row["tls_ms"] = round((time.perf_counter() - started) * 1000, 2)
        row["tls_ok"] = True

        stage = "http"
        started = time.perf_counter()
        request = f"HEAD {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nUser-Agent: lili-network-probe\r\n\r\n"
        tls_sock.sendall(request.encode("ascii"))
        response_line = tls_sock.recv(256).split(b"\r\n", 1)[0]
        parts = response_line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            row["http_status"] = int(parts[1])
            row["http_ok"] = True
        else:
            raise RuntimeError("invalid_http_status_line")
        row["http_ms"] = round((time.perf_counter() - started) * 1000, 2)
    except Exception as exc:  # noqa: BLE001 - the probe must record every network failure
        kind, error_number = classify_error(exc)
        row["error_stage"] = stage
        row["error_kind"] = kind
        row["exception_type"] = type(exc).__name__
        row["os_errno"] = error_number
    finally:
        if tls_sock is not None:
            tls_sock.close()
        if sock is not None:
            sock.close()
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--primary-name", default="openrouter")
    parser.add_argument("--primary-host", default="openrouter.ai")
    parser.add_argument("--primary-path", default="/api/v1/models")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    targets = ((args.primary_name, args.primary_host, args.primary_path), *CONTROL_TARGETS)

    deadline = time.monotonic() + args.duration
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8", buffering=1) as output:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.workers, len(targets)))) as executor:
            while time.monotonic() < deadline:
                iteration_started = time.monotonic()
                futures = [executor.submit(probe, *target, args.timeout) for target in targets]
                for future in futures:
                    output.write(json.dumps(future.result(), ensure_ascii=True, separators=(",", ":")) + "\n")
                remaining = args.interval - (time.monotonic() - iteration_started)
                if remaining > 0:
                    time.sleep(remaining)


if __name__ == "__main__":
    main()
