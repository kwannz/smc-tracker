#!/usr/bin/env bash
# SMC 抓庄系统 — SQLite 数据库备份（带时间戳，安全在线备份）
# 用法:
#   bash deploy/backup.sh
#   SMC_DB=/opt/smc/data/smc.db BACKUP_DIR=/opt/smc/backups KEEP=14 bash deploy/backup.sh
# 建议 crontab（每天凌晨 3 点备份，保留 14 份）:
#   0 3 * * * cd /opt/smc && bash deploy/backup.sh >> data/backup.log 2>&1
set -euo pipefail

SMC_DB="${SMC_DB:-./data/smc.db}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP="${KEEP:-14}"                 # 保留最近 N 份，超出删旧

if [[ ! -f "${SMC_DB}" ]]; then
  echo "!! 数据库不存在: ${SMC_DB}" >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"
ts="$(date -u +%Y%m%d-%H%M%S)"
dst="${BACKUP_DIR}/smc-${ts}.db"

# 优先用 sqlite3 .backup（在线一致性快照，不锁写）；无 sqlite3 则退回 cp
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "${SMC_DB}" ".backup '${dst}'"
  echo "==> 已备份(sqlite .backup): ${dst}"
else
  cp "${SMC_DB}" "${dst}"
  echo "==> 已备份(cp): ${dst}"
fi

# 压缩节省空间（可选，有 gzip 才压）
if command -v gzip >/dev/null 2>&1; then
  gzip -f "${dst}"
  dst="${dst}.gz"
  echo "==> 已压缩: ${dst}"
fi

# 轮转：保留最近 KEEP 份，删除更旧的
mapfile -t old < <(ls -1t "${BACKUP_DIR}"/smc-*.db* 2>/dev/null | tail -n +"$((KEEP + 1))")
if [[ "${#old[@]}" -gt 0 ]]; then
  echo "==> 清理 ${#old[@]} 份旧备份（保留最近 ${KEEP} 份）"
  rm -f "${old[@]}"
fi

echo "==> 备份完成"
