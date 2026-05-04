#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRE take-home monitoring agent.

This agent is intentionally lightweight:
- collect CPU, memory, and zombie process data from Linux /proc
- check internal and external TCP connectivity
- classify DNS and TCP failures
- write structured JSON logs locally and to stdout
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_INTERNAL_TARGETS = "www.graid.com:80,192.168.1.254:80"
DEFAULT_EXTERNAL_TARGETS = "google.com:443,1.1.1.1:443"
DEFAULT_LOG_FILE = "/var/log/sre-monitoring-agent.log"


@dataclass(frozen=True)
class TcpTarget:
    name: str
    host: str
    port: int


@dataclass(frozen=True)
class TcpCheckResult:
    target: str
    host: str
    port: int
    ok: bool
    failure_type: str | None
    message: str
    latency_ms: float | None


# ------------------------------
# Linux resource check
# ------------------------------


def read_cpu_stat() -> tuple[int, int]:
    """Return idle and total CPU jiffies from the aggregate /proc/stat row."""
    first_line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    fields = first_line.split()

    if not fields or fields[0] != "cpu":
        raise RuntimeError("unexpected /proc/stat format")

    values = [int(value) for value in fields[1:]]
    idle_time = values[3] + values[4]
    total_time = sum(values)

    return idle_time, total_time


def get_cpu_usage_percent(sample_seconds: float) -> float:
    """Sample /proc/stat twice and calculate CPU utilization for the interval."""
    idle_before, total_before = read_cpu_stat()
    time.sleep(sample_seconds)
    idle_after, total_after = read_cpu_stat()

    idle_delta = idle_after - idle_before
    total_delta = total_after - total_before

    if total_delta <= 0:
        return 0.0

    return round((1 - idle_delta / total_delta) * 100, 2)


def get_memory_usage_percent() -> float:
    """Calculate memory utilization using MemAvailable when the kernel exposes it."""
    meminfo = {}

    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, raw_value = line.split(":", 1)
        meminfo[key] = int(raw_value.strip().split()[0])

    total = meminfo["MemTotal"]
    available = meminfo.get("MemAvailable")

    if available is None:
        available = meminfo["MemFree"] + meminfo.get("Buffers", 0) + meminfo.get("Cached", 0)

    return round((1 - available / total) * 100, 2)


def get_zombie_processes() -> list[dict[str, str | int]]:
    """Scan /proc for processes whose state is Z and return useful identifiers."""
    zombies = []

    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue

        try:
            stat_text = (proc_dir / "stat").read_text(encoding="utf-8")
            name_end = stat_text.rfind(")")
            process_name = stat_text[stat_text.find("(") + 1 : name_end]
            fields = stat_text[name_end + 2 :].split()

            process_state = fields[0]
            parent_pid = int(fields[1])
        except (FileNotFoundError, IndexError, PermissionError, ValueError):
            continue

        if process_state == "Z":
            zombies.append(
                {
                    "pid": int(proc_dir.name),
                    "ppid": parent_pid,
                    "name": process_name,
                }
            )

    return zombies


# ------------------------------
# Network check
# ------------------------------


def parse_target_list(raw_targets: str, group_name: str) -> list[TcpTarget]:
    """Parse a comma-separated host:port list into named TCP targets."""
    targets = []

    for index, item in enumerate(raw_targets.split(","), start=1):
        item = item.strip()

        if not item:
            continue

        if ":" not in item:
            raise ValueError(f"target must be host:port, got {item!r}")

        host, raw_port = item.rsplit(":", 1)
        targets.append(TcpTarget(name=f"{group_name}-{index}", host=host.strip(), port=int(raw_port)))

    return targets


def check_tcp_connection(target: TcpTarget, timeout_seconds: float) -> TcpCheckResult:
    """Resolve the target and try each returned address until one TCP connect succeeds."""
    start_time = time.monotonic()

    try:
        addresses = socket.getaddrinfo(
            target.host,
            target.port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as ex:
        return TcpCheckResult(
            target=target.name,
            host=target.host,
            port=target.port,
            ok=False,
            failure_type="dns_resolution_error",
            message=str(ex),
            latency_ms=None,
        )

    # getaddrinfo may return multiple IPv4/IPv6 addresses. Trying each one avoids
    # reporting a failure when only the first candidate address is unavailable.
    last_error = None

    for family, socktype, proto, _, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout_seconds)

        try:
            sock.connect(sockaddr)
            latency_ms = round((time.monotonic() - start_time) * 1000, 2)

            return TcpCheckResult(
                target=target.name,
                host=target.host,
                port=target.port,
                ok=True,
                failure_type=None,
                message="connected",
                latency_ms=latency_ms,
            )
        except socket.timeout as ex:
            last_error = ex
        except ConnectionRefusedError as ex:
            return TcpCheckResult(
                target=target.name,
                host=target.host,
                port=target.port,
                ok=False,
                failure_type="tcp_connection_refused",
                message=str(ex),
                latency_ms=None,
            )
        except OSError as ex:
            last_error = ex
        finally:
            sock.close()

    failure_type = "tcp_connection_timeout" if isinstance(last_error, socket.timeout) else "tcp_connection_error"

    return TcpCheckResult(
        target=target.name,
        host=target.host,
        port=target.port,
        ok=False,
        failure_type=failure_type,
        message=str(last_error) if last_error else "no address succeeded",
        latency_ms=None,
    )


# ------------------------------
# Logging / main flow
# ------------------------------


def setup_logging(log_file: str) -> None:
    """Configure logs for both journald/stdout and the local assignment log file."""
    handlers = [logging.StreamHandler()]

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def write_log(level: int, event: str, **fields: object) -> None:
    """Write one compact JSON event so logs are easy to grep or ship later."""
    log_item = {"event": event, **fields}
    message = json.dumps(log_item, separators=(",", ":"), sort_keys=True)

    logging.log(level, message)


def run_one_check(args: argparse.Namespace, targets: Iterable[TcpTarget]) -> None:
    """Run one full collection cycle: resources first, then network checks."""
    cpu_percent = get_cpu_usage_percent(args.cpu_sample_seconds)
    memory_percent = get_memory_usage_percent()
    zombies = get_zombie_processes()

    write_log(
        logging.INFO,
        "metrics_collected",
        cpu_percent=cpu_percent,
        memory_percent=memory_percent,
        zombie_count=len(zombies),
    )

    if cpu_percent >= args.cpu_threshold:
        write_log(logging.WARNING, "cpu_high", cpu_percent=cpu_percent, threshold=args.cpu_threshold)

    if memory_percent >= args.memory_threshold:
        write_log(logging.WARNING, "memory_high", memory_percent=memory_percent, threshold=args.memory_threshold)

    if zombies:
        # Cap the process list so one unhealthy host cannot flood the local log.
        write_log(logging.WARNING, "zombie_processes_found", zombie_count=len(zombies), zombies=zombies[:20])

    for target in targets:
        result = check_tcp_connection(target, args.tcp_timeout)
        log_level = logging.INFO if result.ok else logging.WARNING

        write_log(log_level, "tcp_check", **asdict(result))


def build_parser() -> argparse.ArgumentParser:
    """Build CLI flags, with environment variables as deployment-friendly defaults."""
    parser = argparse.ArgumentParser(description="Lightweight Linux monitoring agent")

    parser.add_argument("--interval", type=float, default=float(os.getenv("MONITOR_INTERVAL", "60")))
    parser.add_argument("--cpu-threshold", type=float, default=float(os.getenv("CPU_THRESHOLD", "85")))
    parser.add_argument("--memory-threshold", type=float, default=float(os.getenv("MEMORY_THRESHOLD", "90")))
    parser.add_argument("--tcp-timeout", type=float, default=float(os.getenv("TCP_TIMEOUT", "3")))
    parser.add_argument("--cpu-sample-seconds", type=float, default=float(os.getenv("CPU_SAMPLE_SECONDS", "1")))
    parser.add_argument("--log-file", default=os.getenv("MONITOR_LOG_FILE", DEFAULT_LOG_FILE))
    parser.add_argument("--internal-targets", default=os.getenv("INTERNAL_TARGETS", DEFAULT_INTERNAL_TARGETS))
    parser.add_argument("--external-targets", default=os.getenv("EXTERNAL_TARGETS", DEFAULT_EXTERNAL_TARGETS))
    parser.add_argument("--once", action="store_true", help="Run one collection cycle and exit")

    return parser


def main() -> int:
    """Start the agent loop. --once is useful for smoke tests and CI checks."""
    args = build_parser().parse_args()
    setup_logging(args.log_file)

    targets = [
        *parse_target_list(args.internal_targets, "internal"),
        *parse_target_list(args.external_targets, "external"),
    ]

    write_log(logging.INFO, "agent_started", interval=args.interval, targets=[asdict(target) for target in targets])

    while True:
        try:
            run_one_check(args, targets)
        except Exception as ex:
            write_log(logging.ERROR, "collection_failed", failure_type=type(ex).__name__, message=str(ex))

        if args.once:
            return 0

        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
