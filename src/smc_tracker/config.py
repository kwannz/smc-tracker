"""配置加载：从 YAML 读取并提供带默认值的强类型访问。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class WatchAddress:
    address: str
    label: str = ""
    notional_alert_usd: float = 100_000.0


@dataclass(slots=True)
class HyperliquidCfg:
    rest_url: str = "https://api.hyperliquid.xyz"
    ws_url: str = "wss://api.hyperliquid.xyz/ws"
    ping_interval_sec: float = 50.0
    reconnect_max_backoff_sec: float = 30.0


@dataclass(slots=True)
class DetectionCfg:
    large_fill_notional_usd: float = 50_000.0
    position_change_pct: float = 0.10
    poll_interval_sec: float = 2.0
    require_sweep: bool = False        # 信号硬门槛：必须有流动性扫荡确认（回测验证的正期望过滤）


@dataclass(slots=True)
class SmcCfg:
    candle_interval: str = "5m"
    swing_lookback: int = 3
    fvg_min_gap_pct: float = 0.0005
    history_bars: int = 500


@dataclass(slots=True)
class TelegramCfg:
    bot_token: str = ""      # @BotFather 创建机器人得到（Bot API 推送用）
    chat_id: str = ""        # 频道 @username 或数字 id（把 bot 加为频道管理员）
    api_id: int = 0          # MTProto app id（Telethon 路线备用，Bot API 不需要）
    api_hash: str = ""       # MTProto app hash（敏感，勿提交）


@dataclass(slots=True)
class LLMCfg:
    """Codex(OAuth GPT-5.4) 研判层。默认关闭——需本机 `codex login`。"""
    enabled: bool = False
    command: list[str] = field(
        default_factory=lambda: ["codex", "exec", "--skip-git-repo-check"])
    model: str = ""                  # 形如 "gpt-5.4"；空=用 codex 默认/配置档
    timeout_sec: float = 90.0
    interval_sec: float = 3600.0     # 研判频率(默认随摘要日报同频)
    max_input_chars: int = 6000      # 喂入态势摘要的截断上限(控延迟/token)


@dataclass(slots=True)
class OutputCfg:
    console: bool = True
    jsonl_path: str = "data/signals.jsonl"
    webhook_url: str = ""


@dataclass(slots=True)
class ReviewCfg:
    """多时间段(MTF)信号有效性评估配置。

    horizons_min：信号评估水平线（分钟），默认 7 个 TF：5m/15m/30m/1h/4h/12h/1d。
    每条前瞻信号在每个 TF 各记一条预测，事后分 TF 分解命中率，诊断哪个时间尺度有真 alpha。
    """
    horizons_min: list[int] = field(
        default_factory=lambda: [5, 15, 30, 60, 240, 720, 1440]
    )


@dataclass(slots=True)
class Config:
    hyperliquid: HyperliquidCfg = field(default_factory=HyperliquidCfg)
    markets: list[str] = field(default_factory=lambda: ["BTC", "ETH"])
    watchlist: list[WatchAddress] = field(default_factory=list)
    detection: DetectionCfg = field(default_factory=DetectionCfg)
    smc: SmcCfg = field(default_factory=SmcCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    telegram: TelegramCfg = field(default_factory=TelegramCfg)
    llm: LLMCfg = field(default_factory=LLMCfg)
    review: ReviewCfg = field(default_factory=ReviewCfg)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        """从 YAML 文件加载配置；缺失字段用 dataclass 默认值兜底。"""
        raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(
            hyperliquid=HyperliquidCfg(**(raw.get("hyperliquid") or {})),
            markets=list(raw.get("markets") or ["BTC", "ETH"]),
            watchlist=[WatchAddress(**w) for w in (raw.get("watchlist") or [])],
            detection=DetectionCfg(**(raw.get("detection") or {})),
            smc=SmcCfg(**(raw.get("smc") or {})),
            output=OutputCfg(**(raw.get("output") or {})),
            telegram=TelegramCfg(**(raw.get("telegram") or {})),
            llm=LLMCfg(**(raw.get("llm") or {})),
            review=ReviewCfg(**(raw.get("review") or {})),
        )


def diff_config(old: "Config", new: "Config") -> list[str]:
    """比较两个 Config 实例的可热更字段，返回变更描述字符串列表。

    可热更字段：
      detection.large_fill_notional_usd / position_change_pct / require_sweep
      output.console / webhook_url
      telegram.bot_token / chat_id
      llm.enabled / model / interval_sec
      watchlist（仅检测新增地址 → 运行时订阅；移除不退订以保留累计状态）

    无变更返回 []。纯函数，无副作用，确定性可测。
    """
    changes: list[str] = []

    # detection
    _cmp(changes, "detection.large_fill_notional_usd",
         old.detection.large_fill_notional_usd, new.detection.large_fill_notional_usd)
    _cmp(changes, "detection.position_change_pct",
         old.detection.position_change_pct, new.detection.position_change_pct)
    _cmp(changes, "detection.require_sweep",
         old.detection.require_sweep, new.detection.require_sweep)

    # output
    _cmp(changes, "output.console", old.output.console, new.output.console)
    _cmp(changes, "output.webhook_url", old.output.webhook_url, new.output.webhook_url)

    # telegram
    _cmp(changes, "telegram.bot_token", old.telegram.bot_token, new.telegram.bot_token)
    _cmp(changes, "telegram.chat_id", old.telegram.chat_id, new.telegram.chat_id)

    # llm
    _cmp(changes, "llm.enabled", old.llm.enabled, new.llm.enabled)
    _cmp(changes, "llm.model", old.llm.model, new.llm.model)
    _cmp(changes, "llm.interval_sec", old.llm.interval_sec, new.llm.interval_sec)

    # watchlist：按地址集合比较，仅报告新增（移除不退订，保留累计仓位/流向状态）
    old_addrs = {w.address.lower() for w in old.watchlist}
    added = [w.address for w in new.watchlist if w.address.lower() not in old_addrs]
    if added:
        preview = ", ".join(a[:10] + "…" for a in added[:3])
        changes.append(f"watchlist 新增 {len(added)} 个: {preview}")

    return changes


def _cmp(changes: list[str], key: str, old_val: Any, new_val: Any) -> None:
    """辅助：若新旧值不同则追加变更描述。"""
    if old_val != new_val:
        changes.append(f"{key}: {old_val!r}→{new_val!r}")
