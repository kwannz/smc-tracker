# SMC 聪明钱抓庄系统 — 容器镜像(systemd 之外的部署路径)
# 构建: docker build -t smc-tracker .
# 两个进程(监控+仪表盘)用 docker-compose 起,见 docker-compose.yml
FROM python:3.12-slim

# 时区(日志/时间戳)
ENV TZ=UTC PYTHONUNBUFFERED=1 PYTHONPATH=/app/src

WORKDIR /app

# 先装依赖(层缓存:requirements 不变则跳过重装)
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip && pip install --no-cache-dir -r requirements.txt

# 拷代码(.dockerignore 排除 data/.venv/.git)
COPY src/ ./src/
COPY config/ ./config/

# data 目录(SQLite,挂 volume 持久)
RUN mkdir -p /app/data

# 默认起监控进程;dashboard 由 compose 覆盖 command
CMD ["python", "-m", "smc_tracker.app", "--config", "config/config.yaml"]
