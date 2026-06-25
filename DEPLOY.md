# SMC 聪明钱抓庄系统 — 部署文档

> Linux 服务器部署。纯公开数据系统（Hyperliquid / Bitget 公开接口、公开 RPC），**无需任何 API key**。
> 两个常驻进程：
> - **monitor**：`python -m smc_tracker.app`（流式 WebSocket 实时抓庄监控 + 周期推送）
> - **dashboard**：`python -m smc_tracker dashboard --port 8787`（aiohttp 单页仪表盘，含 `/harmonic` `/hl2` `/health`）
>
> 唯一需要填的密钥是 **Telegram 推送**（可选；不填则不推送，监控照常运行）。

---

## 0. 前置要求

- Linux（Debian/Ubuntu 或同类），`python3` ≥ 3.12
- 出网访问 Hyperliquid / Bitget / 公开 RPC（HTTPS + WSS）
- 二选一运行方式：**systemd**（推荐裸机）或 **Docker Compose**

---

## 1. 拉取代码 + 配置

```bash
git clone <REPO_URL> /opt/smc
cd /opt/smc

# 从示例生成本地配置（config.yaml 已被 .gitignore，不会进仓库）
cp config/config.example.yaml config/config.yaml
```

编辑 `config/config.yaml`，**填 Telegram 推送**（其余项有合理默认，可后调）：

```yaml
telegram:
  bot_token: "<向 @BotFather 申请的 bot token>"
  chat_id: "<频道 @username 或数字 id；把 bot 加为频道管理员>"
```

- `bot_token`：@BotFather 新建机器人后获得。
- `chat_id`：目标频道/群的 `@username` 或数字 id（形如 `-100…`）。把 bot 加为该频道管理员后即可推送。
- 不填 Telegram 也能跑，只是不推送；`output.webhook_url` 可填 Discord/Slack/通用 webhook 作为替代。

---

## 2A. 部署路径一：systemd（裸机推荐）

一键脚本（建用户 / 建 venv / 装依赖 / 装 systemd 单元 / enable+start）：

```bash
sudo APP_DIR=/opt/smc SMC_USER=smc bash deploy/deploy.sh
# 完成后按提示编辑 config.yaml 填 telegram，再：
sudo systemctl restart smc-monitor
```

手动步骤（脚本内部等价做法）：

```bash
# 1) 建运行用户（无登录权限）
sudo useradd --system --create-home --shell /usr/sbin/nologin smc

# 2) venv + 依赖
sudo python3 -m venv /opt/smc/.venv
sudo /opt/smc/.venv/bin/pip install -U pip
sudo /opt/smc/.venv/bin/pip install -r /opt/smc/requirements.txt

# 3) 属主 + data 目录
sudo mkdir -p /opt/smc/data
sudo chown -R smc:smc /opt/smc

# 4) 装 systemd 单元
sudo cp deploy/smc-monitor.service   /etc/systemd/system/
sudo cp deploy/smc-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smc-monitor smc-dashboard
```

查看状态 / 日志：

```bash
systemctl status smc-monitor smc-dashboard
journalctl -u smc-monitor -f
journalctl -u smc-dashboard -f
```

> 两个 unit 默认 `WorkingDirectory=/opt/smc`、`User=smc`、`Restart=always`、`TZ=UTC`。
> 若部署到别的目录/用户，用 `deploy.sh` 的 `APP_DIR` / `SMC_USER` 变量（脚本会自动 sed 替换 unit 内路径与用户）。

---

## 2B. 部署路径二：Docker Compose

```bash
cd /opt/smc
cp config/config.example.yaml config/config.yaml   # 填 telegram
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` 起两个服务：
- `smc-monitor`：跑 `python -m smc_tracker.app`
- `smc-dashboard`：跑 `python -m smc_tracker dashboard --port 8787`，**只绑 `127.0.0.1:8787`**（外部访问走 nginx）

两服务共享 `./data` volume（SQLite 持久化），`restart: always`，`TZ=UTC`。
`config/config.yaml` 以只读方式挂入容器。

---

## 3. nginx 反向代理（对外暴露仪表盘）

仪表盘进程只监听本机 `127.0.0.1:8787`，对外访问一律经 nginx：

```bash
sudo cp deploy/nginx-smc.conf /etc/nginx/sites-available/smc
sudo ln -s /etc/nginx/sites-available/smc /etc/nginx/sites-enabled/smc
# 编辑 server_name 改成你的域名/IP
sudo nginx -t && sudo systemctl reload nginx
```

建议用 certbot 上 TLS（`nginx-smc.conf` 内含 443 配置注释模板）。可加 `limit_req` 限流或 basic auth 收口。

---

## 4. 健康检查

```bash
# systemd active + /health /harmonic /hl2 返回 200 + DB 有数据
bash deploy/healthcheck.sh
# 自定义端口/库：
SMC_PORT=8787 SMC_DB=/opt/smc/data/smc.db bash deploy/healthcheck.sh
```

返回码 0=健康，非 0=有异常（可接 cron 告警）。也可直接：

```bash
curl -s http://127.0.0.1:8787/health        # JSON：数据新鲜度 + 总体状态
/opt/smc/.venv/bin/python -m smc_tracker health --db /opt/smc/data/smc.db
```

> 冷启动初期 DB 可能暂无数据（candles 表为空），healthcheck 会标 `[WARN]` 而非 `[FAIL]`，属正常。

---

## 5. 备份

```bash
# 备份 data/smc.db 到 backups/，带 UTC 时间戳，保留最近 14 份
bash deploy/backup.sh
SMC_DB=/opt/smc/data/smc.db BACKUP_DIR=/opt/smc/backups KEEP=14 bash deploy/backup.sh
```

建议 crontab（每日 03:00 备份）：

```cron
0 3 * * * cd /opt/smc && bash deploy/backup.sh >> data/backup.log 2>&1
```

---

## 6. 升级 / 回滚

```bash
cd /opt/smc
git pull --ff-only
/opt/smc/.venv/bin/pip install -r requirements.txt   # 依赖有变时
sudo systemctl restart smc-monitor smc-dashboard
# Docker 路径：
docker compose up -d --build
```

回滚：`git checkout <旧提交>` 后重启服务；DB 用 `deploy/backup.sh` 的快照恢复。

---

## 附：cron 闭环（无常驻进程的轻量替代）

不想常驻 monitor 时，可用单条 crontab 驱动采集+评估+推送闭环（详见 `scripts/crontab.example`）：

```cron
*/15 * * * * cd /opt/smc && PYTHONPATH=src ./.venv/bin/python -m smc_tracker cycle --push >> data/cycle.log 2>&1
```
