#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/sre-monitoring-agent}"
SERVICE_PATH="${SERVICE_PATH:-/etc/systemd/system/sre-monitoring-agent.service}"
LOGROTATE_PATH="${LOGROTATE_PATH:-/etc/logrotate.d/sre-monitoring-agent}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root, for example: sudo $0" >&2
  exit 1
fi

install -d -m 0755 "${APP_DIR}"
install -m 0755 "${REPO_ROOT}/monitoring_agent.py" "${APP_DIR}/monitoring_agent.py"
install -m 0644 "${REPO_ROOT}/sre-monitoring-agent.service" "${SERVICE_PATH}"
install -m 0644 "${SCRIPT_DIR}/logrotate/sre-monitoring-agent" "${LOGROTATE_PATH}"

systemctl daemon-reload
systemctl enable --now sre-monitoring-agent

echo "sre-monitoring-agent installed and started."
echo "Check status with: systemctl status sre-monitoring-agent"
echo "Follow logs with:  journalctl -u sre-monitoring-agent -f"
