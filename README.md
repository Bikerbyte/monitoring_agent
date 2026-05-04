# SRE Monitoring Agent

Lightweight Linux monitoring agent base on python script.

The agent runs locally on each server, periodically records resource metrics, checks internal and external TCP connectivity, classifies network failures, and writes structured JSON logs to the local machine.

If you wish for larger scale monitoring agent integreted with Terraform/ Ansible/ Grafana, please refer to the repo below. https://github.com/Bikerbyte/iac-monitoring-system

## Repository Structure

```text
monitoring_agent.py
sre-monitoring-agent.service
deploy/
  install.sh
  uninstall.sh
  logrotate/
    sre-monitoring-agent
  ansible/
    deploy.yml
    inventory.example.ini
```

## Demo

Log:
<img width="1032" height="219" alt="image" src="https://github.com/user-attachments/assets/55c17df4-3040-4f27-ab39-d1cf93da2a68" />

Startup Service:
<img width="1019" height="227" alt="image" src="https://github.com/user-attachments/assets/8d329698-9dbf-4226-ba36-fe25f3b61456" />


## Manual Run

Run one check cycle:

```bash
chmod +x monitoring_agent.py
sudo ./monitoring_agent.py --once
```

For local testing without writing to `/var/log`:

```bash
./monitoring_agent.py --once --log-file ./sre-monitoring-agent.log
```

## Install 

Use the bundled installer on a Linux server:

```bash
sudo ./deploy/install.sh
```

The installer copies the script to `/opt/sre-monitoring-agent` and enables the service on boot.

Check service and logs:

```bash
systemctl status sre-monitoring-agent
journalctl -u sre-monitoring-agent -f
tail -f /var/log/sre-monitoring-agent.log
```

## Uninstall 

```bash
sudo ./deploy/uninstall.sh
```


## Requirement Mapping

| Assignment requirement | Implementation |
| --- | --- |
| Python or Bash script | `monitoring_agent.py` |
| Agent log stored locally | `/var/log/sre-monitoring-agent.log` |
| CPU utilization | Reads `/proc/stat` |
| Memory usage | Reads `/proc/meminfo` |
| Zombie processes | Scans `/proc/<pid>/stat` |
| Internal TCP checks | `www.graid.com:80`, `192.168.1.254:80` |
| External TCP checks | `google.com:443`, `1.1.1.1:443` |
| Failure classification | DNS resolution, TCP timeout, connection refused, generic TCP error |
| Start on boot | `sre-monitoring-agent.service` |
| Maintainability bonus | Environment-driven config, JSON logs, install script, Ansible deployment example, logrotate |

## Deploy With Ansible

For a lab with many Linux servers, update the example inventory:

```bash
cp deploy/ansible/inventory.example.ini deploy/ansible/inventory.ini
```

Then deploy:

```bash
ansible-playbook -i deploy/ansible/inventory.ini deploy/ansible/deploy.yml
```

The playbook installs Python 3, copies the agent, installs the systemd unit, installs logrotate config, and starts the service.

## Configuration & ENV

The systemd unit sets these environment variables. They can be changed in `sre-monitoring-agent.service` or overridden with a systemd drop-in.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MONITOR_INTERVAL` | `60` | Seconds between check cycles |
| `CPU_THRESHOLD` | `85` | CPU warning threshold |
| `MEMORY_THRESHOLD` | `90` | Memory warning threshold |
| `TCP_TIMEOUT` | `3` | TCP connection timeout in seconds |
| `INTERNAL_TARGETS` | `www.graid.com:80,192.168.1.254:80` | Comma-separated internal `host:port` targets |
| `EXTERNAL_TARGETS` | `google.com:443,1.1.1.1:443` | Comma-separated external `host:port` targets |
| `MONITOR_LOG_FILE` | `/var/log/sre-monitoring-agent.log` | Local log file path |

Example override:

```bash
sudo systemctl edit sre-monitoring-agent
```

```ini
[Service]
Environment=MONITOR_INTERVAL=30
Environment=CPU_THRESHOLD=80
Environment=EXTERNAL_TARGETS=google.com:443,1.1.1.1:443
```

Apply the override:

```bash
sudo systemctl daemon-reload
sudo systemctl restart sre-monitoring-agent
```


