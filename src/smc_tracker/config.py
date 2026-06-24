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
    push_ticker_board: bool = False   # 行情监控板(价/涨跌幅/费率/OI)推送——用户#不需要，默认关；核心聚焦 HL


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
class FeishuCfg:
    """飞书(Lark)自定义机器人推送。webhook_url + secret(机器人开签名校验时必填)。"""
    webhook_url: str = ""
    secret: str = ""


@dataclass(slots=True)
class DigestCfg:
    """HL 事件分类汇总推送。零散 HL 事件按分类聚合，周期推**一张**分类汇总卡片（降噪去刷屏）。

    enabled=True 时：跟庄/共振/背离/共识/挂单墙/暴涨/TA/持仓 等事件进汇总缓冲，按 interval_sec
    周期推一张分类汇总卡片；urgent_instant=True 时核心前瞻信号（超级共振/可疑地址）仍即时单独推。
    enabled=False 回退为旧行为（每条事件即时推）。
    """
    enabled: bool = True
    interval_sec: float = 300.0      # 汇总卡片推送周期（默认 5 分钟一张）
    max_per_cat: int = 8             # 每个分类卡片内最多明细行数（超出显示最新+省略计数）
    urgent_instant: bool = True      # 超级共振/可疑地址 是否仍即时单独推（核心前瞻不延迟）


@dataclass(slots=True)
class OKXCfg:
    """OKX 永续 streaming 监控配置。默认关闭(避免无脑新增 WS 连接)。"""
    enabled: bool = False
    ws_url: str = "wss://ws.okx.com:8443/ws/v5/public"
    rest_url: str = "https://www.okx.com"
    top_n: int = 20                  # 监控按 OI 排名前 N 个永续(symbols 为空时)
    surge_pct: float = 0.05          # OI 异动阈值
    symbols: list[str] = field(default_factory=list)   # 指定监控的 coin(空=top_n)


@dataclass(slots=True)
class BollingerCfg:
    """Bitget 永续多周期布林带压力/支撑分析配置。

    enabled=True 时周期推送卡片；interval_sec 控制推送频率（默认 15 分钟一张）。
    timeframes 默认覆盖 6 个主流周期（用户#：多周期 6tf；去 5m 噪音）；bars 为每周期 K 线根数（上限 1999）。
    period/k 为布林带参数（业界标准 20/2.0）；top_n 限制最多监控的币种数。
    """
    enabled: bool = True
    interval_sec: float = 900.0          # 推送周期（默认 15 分钟）
    timeframes: list[str] = field(
        default_factory=lambda: ["15m", "1H", "4H", "12H", "1D", "1W"]
    )
    bars: int = 1000                     # 每周期 K 线根数（用户#：固定 1000）；大周期受 Bitget ~90天/请求上限+
                                         # max_pages 约束取全部可得历史；429 由 _get 退避重试兜底（实现层 clamp ≤1999）
    period: int = 20                     # 布林带均线周期
    k: float = 2.0                       # 标准差倍数
    top_n: int = 12                      # 最多监控前 N 个币（按成交额排序）


@dataclass(slots=True)
class HarmonicCfg:
    """Bitget 永续多周期谐波形态（Harmonic Patterns）分析配置。

    enabled=True 时周期推送卡片；interval_sec 控制推送频率（默认 15 分钟）。
    timeframes 覆盖 6 个主流周期（用户#：多周期 6tf，与布林带一致）；bars 每周期 K 线根数；
    order 枢轴邻域大小；tol 比率容差（默认 5%）；top_n 最多监控币种数。
    """
    enabled: bool = True
    interval_sec: float = 900.0
    timeframes: list[str] = field(
        default_factory=lambda: ["15m", "1H", "4H", "12H", "1D", "1W"]
    )
    bars: int = 1000                     # 用户#：固定 1000（谐波需 ~60-150 根；大周期取可得历史，429 退避兜底）
    order: int = 3
    tol: float = 0.05
    top_n: int = 12
    account_usd: float = 10_000.0        # 仓位计算用账户名义资金（USD）
    risk_pct: float = 0.01               # 单笔风险比例（1%）
    target_rr: float = 2.0               # 目标盈亏比
    # 谐波系统**专用独立飞书**（用户#：与 HL 信号分开推送）；为空则回退主 notifier
    feishu_webhook: str = ""
    feishu_secret: str = ""


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
    okx: OKXCfg = field(default_factory=OKXCfg)
    feishu: FeishuCfg = field(default_factory=FeishuCfg)
    digest: DigestCfg = field(default_factory=DigestCfg)
    bollinger: BollingerCfg = field(default_factory=BollingerCfg)
    harmonic: HarmonicCfg = field(default_factory=HarmonicCfg)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        """从 YAML 文件加载配置；缺失字段用 dataclass 默认值兜底。"""
        raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        # BollingerCfg.timeframes 是 list，需从 raw 正确透传
        bb_raw: dict[str, Any] = dict(raw.get("bollinger") or {})
        if "timeframes" in bb_raw and not isinstance(bb_raw["timeframes"], list):
            bb_raw["timeframes"] = list(bb_raw["timeframes"])
        # HarmonicCfg.timeframes 同理
        harm_raw: dict[str, Any] = dict(raw.get("harmonic") or {})
        if "timeframes" in harm_raw and not isinstance(harm_raw["timeframes"], list):
            harm_raw["timeframes"] = list(harm_raw["timeframes"])
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
            okx=OKXCfg(**(raw.get("okx") or {})),
            feishu=FeishuCfg(**(raw.get("feishu") or {})),
            digest=DigestCfg(**(raw.get("digest") or {})),
            bollinger=BollingerCfg(**bb_raw),
            harmonic=HarmonicCfg(**harm_raw),
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
