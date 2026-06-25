#!/usr/bin/env bash
# SMC 抓庄系统 — 健康检查（systemd active + 仪表盘 HTTP 200 + DB 有数据）
# 用法:
#   bash deploy/healthcheck.sh
#   SMC_PORT=8787 SMC_DB=/opt/smc/data/smc.db bash deploy/healthcheck.sh
# 退出码: 0=健康, 非0=有异常（可接入 cron / 监控告警）。
set -euo pipefail

SMC_PORT="${SMC_PORT:-8787}"
SMC_HOST="${SMC_HOST:-127.0.0.1}"
SMC_DB="${SMC_DB:-./data/smc.db}"
BASE="http://${SMC_HOST}:${SMC_PORT}"

fail=0

# ─── 1. systemd 服务状态（仅在有 systemctl 的环境检查）──────────────────────
if command -v systemctl >/dev/null 2>&1; then
  for svc in smc-monitor smc-dashboard; do
    if systemctl is-active --quiet "${svc}"; then
      echo "[OK]   systemd ${svc} active"
    else
      echo "[FAIL] systemd ${svc} 未运行"
      fail=1
    fi
  done
else
  echo "[SKIP] 无 systemctl（容器/本地环境），跳过服务状态检查"
fi

# ─── 2. 仪表盘 HTTP 路由探活（/health /harmonic /hl2 均应 200）──────────────
for path in /health /harmonic /hl2; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${BASE}${path}" || echo 000)"
  if [[ "${code}" == "200" ]]; then
    echo "[OK]   GET ${path} -> 200"
  else
    echo "[FAIL] GET ${path} -> ${code}"
    fail=1
  fi
done

# ─── 3. DB 有数据（candles/trades 任一表非空即认为采集在跑）──────────────────
if [[ -f "${SMC_DB}" ]]; then
  # 统计 candles 表行数（无该表则尝试 sqlite_master 列表）
  rows="$(sqlite3 "${SMC_DB}" \
    "SELECT COALESCE((SELECT COUNT(*) FROM candles),0);" 2>/dev/null || echo 0)"
  if [[ "${rows}" =~ ^[0-9]+$ && "${rows}" -gt 0 ]]; then
    echo "[OK]   DB candles 行数=${rows}（采集有数据）"
  else
    tables="$(sqlite3 "${SMC_DB}" \
      "SELECT COUNT(*) FROM sqlite_master WHERE type='table';" 2>/dev/null || echo 0)"
    echo "[WARN] DB candles 为空（行数=${rows}，表数=${tables}）— 冷启动初期可能正常"
  fi
else
  echo "[FAIL] DB 文件不存在: ${SMC_DB}"
  fail=1
fi

echo ""
if [[ "${fail}" -eq 0 ]]; then
  echo "==> 健康检查通过"
else
  echo "==> 健康检查发现异常（退出码 1）"
fi
exit "${fail}"
