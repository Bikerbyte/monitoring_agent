# Disable and delete service
sudo systemctl stop sre-monitoring-agent
sudo systemctl disable sre-monitoring-agent
sudo rm /etc/systemd/system/sre-monitoring-agent.service
sudo systemctl daemon-reload

# Remove py agent script
sudo rm -rf /opt/sre-monitoring-agent

# Remove config
sudo rm /etc/logrotate.d/sre-monitoring-agent

# Remove log file
# sudo rm /var/log/sre-monitoring-agent.log