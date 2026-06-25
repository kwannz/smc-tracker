#!/usr/bin/env bash
# SMC 抓庄系统 — 一键部署脚本（拉代码 / 建 venv / 装依赖 / 装 systemd 单元 / enable+start）
# 用法（在目标服务器上以有 sudo 权限的用户执行）:
#   sudo APP_DIR=/opt/smc SMC_USER=smc bash deploy/deploy.sh
# 幂等：可重复执行（再次运行=更新代码+重装依赖+重启服务）。
set -euo pipefail

# ─── 可配置参数（环境变量覆盖）──────────────────────────────────────────────
APP_DIR="${APP_DIR:-/opt/smc}"            # 部署目录
SMC_USER="${SMC_USER:-smc}"               # 运行服务的系统用户
REPO_URL="${REPO_URL:-}"                  # 首次部署可填仓库地址（已有代码则留空）
PY_BIN="${PY_BIN:-python3}"               # 系统 Python（建 venv 用，需 3.12+）

echo "==> SMC 部署：APP_DIR=${APP_DIR} USER=${SMC_USER}"

# ─── 1. 创建运行用户（若不存在，无登录权限）──────────────────────────────────
if ! id -u "${SMC_USER}" >/dev/null 2>&1; then
  echo "==> 创建系统用户 ${SMC_USER}"
  useradd --system --create-home --shell /usr/sbin/nologin "${SMC_USER}"
fi

# ─── 2. 拉取/更新代码 ────────────────────────────────────────────────────────
if [[ -n "${REPO_URL}" && ! -d "${APP_DIR}/.git" ]]; then
  echo "==> 克隆仓库到 ${APP_DIR}"
  git clone "${REPO_URL}" "${APP_DIR}"
elif [[ -d "${APP_DIR}/.git" ]]; then
  echo "==> 更新已有仓库（git pull）"
  git -C "${APP_DIR}" pull --ff-only
else
  echo "!! ${APP_DIR} 既无 .git 也未提供 REPO_URL，假设代码已就位（继续）"
fi

# ─── 3. 建 venv + 装依赖 ─────────────────────────────────────────────────────
if [[ ! -d "${APP_DIR}/.venv" ]]; then
  echo "==> 创建 venv"
  "${PY_BIN}" -m venv "${APP_DIR}/.venv"
fi
echo "==> 安装/更新依赖"
"${APP_DIR}/.venv/bin/pip" install --no-cache-dir -U pip
"${APP_DIR}/.venv/bin/pip" install --no-cache-dir -r "${APP_DIR}/requirements.txt"

# ─── 4. 准备 config / data 目录 ──────────────────────────────────────────────
mkdir -p "${APP_DIR}/data"
if [[ ! -f "${APP_DIR}/config/config.yaml" ]]; then
  echo "==> 生成 config.yaml（从 example）— 部署后务必填 telegram bot_token/chat_id"
  cp "${APP_DIR}/config/config.example.yaml" "${APP_DIR}/config/config.yaml"
fi

# ─── 5. 属主修正（服务用户可读写 data）──────────────────────────────────────
chown -R "${SMC_USER}:${SMC_USER}" "${APP_DIR}"

# ─── 6. 安装 systemd 单元 ────────────────────────────────────────────────────
echo "==> 安装 systemd 单元"
sed "s#/opt/smc#${APP_DIR}#g; s/^User=smc/User=${SMC_USER}/; s/^Group=smc/Group=${SMC_USER}/" \
  "${APP_DIR}/deploy/smc-monitor.service" > /etc/systemd/system/smc-monitor.service
sed "s#/opt/smc#${APP_DIR}#g; s/^User=smc/User=${SMC_USER}/; s/^Group=smc/Group=${SMC_USER}/" \
  "${APP_DIR}/deploy/smc-dashboard.service" > /etc/systemd/system/smc-dashboard.service

systemctl daemon-reload
systemctl enable --now smc-monitor.service
systemctl enable --now smc-dashboard.service

echo ""
echo "==> 部署完成。下一步："
echo "    1) 编辑 ${APP_DIR}/config/config.yaml 填 telegram.bot_token / chat_id"
echo "    2) 重启监控让配置生效： systemctl restart smc-monitor"
echo "    3) 探活： bash ${APP_DIR}/deploy/healthcheck.sh"
echo "    4) 配 nginx 反代： 见 ${APP_DIR}/deploy/nginx-smc.conf"
