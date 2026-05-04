#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRE monitoring agent for Linux.

Collected metrics
-----------------
  - CPU utilisation  (two-sample delta from /proc/stat)
  - Memory utilisation (MemAvailable from /proc/meminfo)
  - Zombie process count and identifiers
  - TCP reachability + latency for configurable internal and external targets
  - Failure type classification: dns_resolution_error, tcp_connection_timeout,
    tcp_connection_refused, tcp_connection_error
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

# ---------------------------------------------------------------------------
# Version — bump this string on each release so the agent_started log entry
# makes it easy to tell which code is running across the fleet.
# ---------------------------------------------------------------------------
__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# The agent's own hostname is injected into every log record so that logs
# from all 50 servers can be collected centrally (e.g. shipped via Filebeat or
# Promtail) and still be filtered per host without needing separate log files.
# ---------------------------------------------------------------------------
HOSTNAME = socket.gethostname()

DEFAULT_INTERNAL_TARGETS = "www.graid.com:80,192.168.1.254:80"
DEFAULT_EXTERNAL_TARGETS = "google.com:443,1.1.1.1:443"
DEFAULT_LOG_FILE = "/var/log/sre-monitoring-agent.log"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

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
    failure_type: str | None  # None when ok=True
    message: str
    latency_ms: float | None  # None when the connection failed


# ---------------------------------------------------------------------------
# Linux resource checks 
# ---------------------------------------------------------------------------

def read_cpu_stat() -> tuple[int, int]:
    """
    Return (idle_jiffies, total_jiffies) from the aggregate cpu row of /proc/stat.
    idle_time = idle + iowait(waiting for IO)
        E.g.cpu  348381 846 651431 69169917 8171 0 35424 0 0 0
            cpu0 23830 167 41908 4316153 414 0 8856 0 0 0
    """
    first_line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    fields = first_line.split()

    if not fields or fields[0] != "cpu":
        raise RuntimeError("unexpected /proc/stat format")

    values = [int(v) for v in fields[1:]]
    idle_time = values[3] + values[4]   # idle + iowait
    total_time = sum(values)

    return idle_time, total_time


def get_cpu_usage_percent(sample_seconds: float) -> float:
    """
    Calculate CPU utilisation between two time snapshot window (for %).
        usage% = (1 - (idle_after - idle_before) / (total_after - total_before)) × 100
    """
    idle_before, total_before = read_cpu_stat()
    time.sleep(sample_seconds)
    idle_after, total_after = read_cpu_stat()

    idle_delta = idle_after - idle_before
    total_delta = total_after - total_before

    if total_delta <= 0:
        # Guard against counter wrap or a suspiciously short sleep.
        return 0.0

    return round((1 - idle_delta / total_delta) * 100, 2)


def get_memory_usage_percent() -> float:
    """ 
    Get memory utilisation from /proc/meminfo.
        E.g.MemTotal:       16152572 kB
            MemFree:          950848 kB
            MemAvailable:    8516336 kB
    """
    meminfo: dict[str, int] = {}

    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, raw_value = line.split(":", 1)
        meminfo[key] = int(raw_value.strip().split()[0])

    total = meminfo["MemTotal"]
    available = meminfo.get("MemAvailable")

    if available is None:
        # Fallback for kernels < 3.14: approximate available as free + buffers + cache.
        available = meminfo["MemFree"] + meminfo.get("Buffers", 0) + meminfo.get("Cached", 0)

    return round((1 - available / total) * 100, 2)


def get_zombie_processes() -> list[dict[str, str | int]]:
    """Scan /proc for zombie processes (state == 'Z').
        E.g. 1 (systemd) S 0 1 1 0
    """
    zombies: list[dict[str, str | int]] = []

    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit(): #only scan PID folder
            continue

        try:
            stat_text = (proc_dir / "stat").read_text(encoding="utf-8")
            name_end = stat_text.rfind(")")
            process_name = stat_text[stat_text.find("(") + 1 : name_end]
            fields = stat_text[name_end + 2 :].split()

            process_state = fields[0]
            parent_pid = int(fields[1])
        except (FileNotFoundError, IndexError, PermissionError, ValueError):
            # Process exited between iterdir() and read, or we lack permission.
            continue

        # Z == zombie
        if process_state == "Z":
            zombies.append(
                {
                    "pid": int(proc_dir.name),
                    "ppid": parent_pid,
                    "name": process_name,
                }
            )

    return zombies


# ---------------------------------------------------------------------------
# Network diagnostics
# ---------------------------------------------------------------------------

def parse_target_list(raw_targets: str, group_name: str) -> list[TcpTarget]:
    """
    Parse 'host:port' string into a list of TcpTarget objects.
        "www.graid.com:80,192.168.1.254:80", "internal"
        -> [TcpTarget(name="internal-1", host="www.graid.com", port=80),
            TcpTarget(name="internal-2", host="192.168.1.254", port=80)]
    """
    targets: list[TcpTarget] = []

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
    """
    Attempt a TCP connection to 'target' and return a classified result.
    """
    start_time = time.monotonic()

    # 1: DNS resolution
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

    # 2: Try TCP connect to resolved address
    last_error: Exception | None = None

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
            # Continue to next address; another candidate might respond.
        except ConnectionRefusedError as ex:
            # RST is definitive — no point trying other addresses for the same port.
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

    # All addresses exhausted — report the last error we saw.
    failure_type = (
        "tcp_connection_timeout"
        if isinstance(last_error, socket.timeout)
        else "tcp_connection_error"
    )

    return TcpCheckResult(
        target=target.name,
        host=target.host,
        port=target.port,
        ok=False,
        failure_type=failure_type,
        message=str(last_error) if last_error else "no address succeeded",
        latency_ms=None,
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_file: str) -> None:
    """
    log at both stdout (captured by journald) and a local file
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def write_log(level: int, event: str, **fields: object) -> None:
    """
    Output format is JSON
    """
    log_item = {"event": event, "host": HOSTNAME, **fields}
    message = json.dumps(log_item, separators=(",", ":"), sort_keys=True)
    logging.log(level, message)


# ---------------------------------------------------------------------------
# Main collection cycle
# ---------------------------------------------------------------------------

def run_one_check(args: argparse.Namespace, targets: Iterable[TcpTarget]) -> None:
    """
    Execute full cycle
    """
    cpu_percent = get_cpu_usage_percent(args.cpu_sample_seconds)
    memory_percent = get_memory_usage_percent()
    zombies = get_zombie_processes()

    # Always log current metrics at INFO so there is a continuous baseline record.
    write_log(
        logging.INFO,
        "metrics_collected",
        cpu_percent=cpu_percent,
        memory_percent=memory_percent,
        zombie_count=len(zombies),
    )

    # Threshold alerts — WARNING level so they stand out in journalctl and can
    # be targeted by alerting rules (e.g. Loki alert on level=WARNING).
    if cpu_percent >= args.cpu_threshold:
        write_log(logging.WARNING, "cpu_high", cpu_percent=cpu_percent, threshold=args.cpu_threshold)

    if memory_percent >= args.memory_threshold:
        write_log(logging.WARNING, "memory_high", memory_percent=memory_percent, threshold=args.memory_threshold)

    if zombies:
        # Cap at 20 entries: if a fork bomb produces thousands of zombies, logging
        # all of them would bloat the log file and obscure other events.
        write_log(logging.WARNING, "zombie_processes_found", zombie_count=len(zombies), zombies=zombies[:20])

    for target in targets:
        result = check_tcp_connection(target, args.tcp_timeout)
        log_level = logging.INFO if result.ok else logging.WARNING
        write_log(log_level, "tcp_check", **asdict(result))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """
    Define CLI flags with environment variable fallbacks.

    Supporting environment variables alongside CLI flags makes the agent easy to
    configure via systemd's EnvironmentFile= directive without touching the script:

        # /etc/sre-monitoring-agent/agent.env
        CPU_THRESHOLD=80
        MONITOR_INTERVAL=30
    """
    parser = argparse.ArgumentParser(
        description="Lightweight Linux monitoring agent",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("MONITOR_INTERVAL", "60")),
        help="Seconds between collection cycles (env: MONITOR_INTERVAL)",
    )
    parser.add_argument(
        "--cpu-threshold",
        type=float,
        default=float(os.getenv("CPU_THRESHOLD", "85")),
        help="CPU %% above which a warning is logged (env: CPU_THRESHOLD)",
    )
    parser.add_argument(
        "--memory-threshold",
        type=float,
        default=float(os.getenv("MEMORY_THRESHOLD", "90")),
        help="Memory %% above which a warning is logged (env: MEMORY_THRESHOLD)",
    )
    parser.add_argument(
        "--tcp-timeout",
        type=float,
        default=float(os.getenv("TCP_TIMEOUT", "3")),
        help="TCP connect timeout in seconds (env: TCP_TIMEOUT)",
    )
    parser.add_argument(
        "--cpu-sample-seconds",
        type=float,
        default=float(os.getenv("CPU_SAMPLE_SECONDS", "1")),
        help="Duration of each CPU measurement window (env: CPU_SAMPLE_SECONDS)",
    )
    parser.add_argument(
        "--log-file",
        default=os.getenv("MONITOR_LOG_FILE", DEFAULT_LOG_FILE),
        help="Local log file path (env: MONITOR_LOG_FILE)",
    )
    parser.add_argument(
        "--internal-targets",
        default=os.getenv("INTERNAL_TARGETS", DEFAULT_INTERNAL_TARGETS),
        help="Comma-separated host:port list for internal TCP checks (env: INTERNAL_TARGETS)",
    )
    parser.add_argument(
        "--external-targets",
        default=os.getenv("EXTERNAL_TARGETS", DEFAULT_EXTERNAL_TARGETS),
        help="Comma-separated host:port list for external TCP checks (env: EXTERNAL_TARGETS)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one collection cycle and exit (useful for smoke tests and CI)",
    )

    return parser


def main() -> int:
    """Entry point: parse args, validate targets, then run the agent loop.
        python3 agent.py --once && echo "agent OK"
    """
    args = build_parser().parse_args()
    setup_logging(args.log_file)

    targets = [
        *parse_target_list(args.internal_targets, "internal"),
        *parse_target_list(args.external_targets, "external"),
    ]

    write_log(
        logging.INFO,
        "agent_started",
        version=__version__,
        interval=args.interval,
        targets=[asdict(t) for t in targets],
    )

    while True:
        try:
            run_one_check(args, targets)
        except Exception as ex:
            # Log the failure type so it can be correlated with system events
            # (e.g. /proc becoming unreadable during a kernel panic).
            write_log(
                logging.ERROR,
                "collection_failed",
                failure_type=type(ex).__name__,
                message=str(ex),
            )

        if args.once:
            return 0

        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())