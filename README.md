# SRE Take-home Assignment

這份作業是實作一支可以部署在 50 台 Linux Server 上的輕量監控 Agent。

目標不是取代完整監控系統，而是先把主機資源、Zombie Process、內外網 TCP 連線狀態記錄下來。真的出事時，維運人員可以從本機 log 快速判斷是資源問題、DNS 問題、TCP Timeout，還是服務端拒絕連線。

## 檔案

- `monitoring_agent.py`：主要監控程式，只使用 Python standard library，不需要額外安裝套件。
- `sre-monitoring-agent.service`：systemd service unit，讓 Agent 開機後自動啟動。

## 監控項目

- CPU 使用率：讀取 `/proc/stat`
- Memory 使用率：讀取 `/proc/meminfo`
- Zombie Process：掃描 `/proc/<pid>/stat`
- TCP 連線檢查：分別檢查內部與外部網路
- 錯誤分類：DNS Resolution Error、TCP Connection Timeout、TCP Connection Refused、其他 TCP Error

Agent 會同時寫 log 到 stdout 與本機檔案，預設檔案位置：

```text
/var/log/sre-monitoring-agent.log
```

Log 採用 JSON 格式，後續若要接 journald、log shipper 或集中式 log 系統會比較容易處理。

## 預設檢查目標

內部網路：

- `www.graid.com:80`
- `192.168.1.254:80`

外部網路：

- `google.com:443`
- `1.1.1.1:443`

這些目標都可以透過環境變數調整，不需要改程式碼。部署到多台機器時，可以讓程式保持一致，把環境差異放在 systemd 或設定管理工具裡。

## 手動執行

```bash
chmod +x monitoring_agent.py
sudo ./monitoring_agent.py --once
```

如果只是本機測試、不想寫入 `/var/log`，可以指定 log 檔案：

```bash
./monitoring_agent.py --once --log-file ./sre-monitoring-agent.log
```

## 安裝成 systemd 服務

```bash
sudo mkdir -p /opt/sre-monitoring-agent
sudo cp monitoring_agent.py /opt/sre-monitoring-agent/monitoring_agent.py
sudo cp sre-monitoring-agent.service /etc/systemd/system/sre-monitoring-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now sre-monitoring-agent
```

確認服務與 log：

```bash
systemctl status sre-monitoring-agent
journalctl -u sre-monitoring-agent -f
tail -f /var/log/sre-monitoring-agent.log
```

## 設定方式

systemd unit 預設帶入以下環境變數：

| 變數 | 預設值 | 用途 |
| --- | --- | --- |
| `MONITOR_INTERVAL` | `60` | 每次檢查的間隔秒數 |
| `CPU_THRESHOLD` | `85` | CPU 使用率告警門檻 |
| `MEMORY_THRESHOLD` | `90` | Memory 使用率告警門檻 |
| `TCP_TIMEOUT` | `3` | TCP 連線逾時秒數 |
| `INTERNAL_TARGETS` | `www.graid.com:80,192.168.1.254:80` | 內部檢查目標，格式為逗號分隔的 `host:port` |
| `EXTERNAL_TARGETS` | `google.com:443,1.1.1.1:443` | 外部檢查目標，格式為逗號分隔的 `host:port` |
| `MONITOR_LOG_FILE` | `/var/log/sre-monitoring-agent.log` | 本機 log 檔案位置 |

## Log 範例

正常採集：

```json
{"cpu_percent":12.31,"event":"metrics_collected","memory_percent":48.9,"zombie_count":0}
```

DNS 解析失敗：

```json
{"event":"tcp_check","failure_type":"dns_resolution_error","host":"bad.example","latency_ms":null,"message":"[Errno -2] Name or service not known","ok":false,"port":443,"target":"external-1"}
```

## 維運考量

- 門檻與檢查目標都放在參數或環境變數，後續要調整不需要重包程式。
- DNS 與 TCP 錯誤有分開記錄，排查時可以先判斷是 DNS、路由、防火牆，還是服務本身拒絕連線。
- Agent 沒有使用第三方套件，部署到多台 Linux Server 的阻力比較低。
- systemd unit 設定了自動重啟，Agent 異常結束後會再被拉起來。
- log 保留原始數值與錯誤訊息，方便後續人工查問題，也可以再接到集中式平台。

## 後續可改善項目

- 加上 logrotate，避免 `/var/log/sre-monitoring-agent.log` 無限制成長。
- 用 Ansible、RPM 或 DEB 包裝部署流程，減少 50 台機器逐台操作的風險。
- 將結果輸出成 Prometheus node exporter textfile collector 格式。
- 對 TCP 檢查加入連續失敗次數，避免短暫網路抖動造成過多告警。
