"""配置加载：从 YAML 读取并提供带默认值的强类型访问。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class UniverseCfg:
    """币种宇宙选择配置。

    mode:
      "all"    → 全部 USDT 永续合约（以 base_map 为准）
      "top_n"  → 按 24h 成交额(quoteVolume)降序取前 top_n 个
      "list"   → 仅 include 列表中指定的 coin
    top_n:        mode="top_n" 时选取数量（默认 12）
    include:      mode="list" 时指定 coin 列表；mode 为其他时忽略
    exclude:      无论何种 mode，最终结果都剔除这些 coin
    asset_filter: "all"(不过滤) / "crypto"(仅加密) / "tradfi"(仅传统金融)
    """
    mode: str = "top_n"
    top_n: int = 12
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    asset_filter: str = "all"


def _safe_vol(val: Any) -> float:
    """安全解析 quoteVolume 字符串为 float；无效/缺失时返回 0.0。"""
    try:
        v = float(val)
        return v if math.isfinite(v) and v >= 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def resolve_universe(
    base_map: dict[str, str],
    tickers: dict[str, dict],
    cfg: UniverseCfg,
) -> dict[str, str]:
    """根据配置选出活跃币种宇宙，返回 {coin: symbol} 映射（按 24h 成交额降序）。

    参数：
        base_map: **{symbol → baseCoin}**（来自 BitgetREST.perp_base_coins()，如 {'BTCUSDT':'BTC'}）
        tickers:  {symbol → {quoteVolume: str, ...}}（来自 tickers() 行情快照）
        cfg:      UniverseCfg 配置对象

    返回：
        {coin(baseCoin) → symbol}，按 quoteVolume 降序（list 模式保持 include 顺序）。
        **asset_filter/exclude 作用于 baseCoin**（与 asset_class 一致；此前 bug：误用 symbol 致 tradfi=0）。

    纯函数：无副作用，确定性可测。流程：候选(仅USDT永续)按成交额降序 → asset_filter+exclude 过滤
    → mode(all/top_n/list) → 按 coin 去重(保留最高成交额)。
    """
    from .asset_class import asset_class as _asset_class

    # 候选 (coin=baseCoin, symbol, 成交额)，仅 USDT 永续，按成交额降序
    # base_map 来自 perp_base_coins()，已仅含 USDT-FUTURES，故不再额外按符号后缀过滤
    cands: list[tuple[str, str, float]] = []
    for sym, base in base_map.items():
        if not base:
            continue
        cands.append((base, sym, _safe_vol(tickers.get(sym, {}).get("quoteVolume"))))
    cands.sort(key=lambda t: t[2], reverse=True)

    # asset_filter + exclude 先过滤（作用于 baseCoin），使 top_n 取的是过滤后的前 N
    if cfg.asset_filter != "all":
        cands = [t for t in cands if _asset_class(t[0]) == cfg.asset_filter]
    exclude_set: frozenset[str] = frozenset(cfg.exclude)
    cands = [t for t in cands if t[0] not in exclude_set]

    # mode 选择
    if cfg.mode == "list":
        by_coin = {b: s for b, s, _ in cands}   # 成交额序去重
        sel: list[tuple[str, str]] = [(c, by_coin[c]) for c in cfg.include if c in by_coin]
    elif cfg.mode == "top_n":
        sel = [(b, s) for b, s, _ in cands[: max(0, cfg.top_n)]]
    else:  # "all" 或未知 → 全部
        sel = [(b, s) for b, s, _ in cands]

    # 按 coin 去重（保留首个=最高成交额）
    out: dict[str, str] = {}
    for coin, symbol in sel:
        if coin not in out:
            out[coin] = symbol
    return out


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
class CorrelationCfg:
    """地址协同显著性阈值配置。

    设计依据(B2 spec)：
      min_lift=2.0  — 共现强度 ≥2× 随机期望才算显著（业界常用强关联阈；lift>2 排除高频偶然）；
      max_p=0.01    — 二项右尾 p ≤1%（99% 置信非随机，闭式确定性）；
      min_shared=3  — 协同事件至少 3 次（防单次巧合）；
      min_coins=2   — 跨≥2 币（单币追涨人群隔离；跨币=同一实体硬证据，CLAUDE.md 点名）。
    宽松降级：min_lift=0/max_p=1 等价旧行为（无显著性过滤）。
    """
    min_lift: float = 2.0       # 共现强度下限(lift >= 此值才显著)
    max_p: float = 0.01         # 二项右尾 p 上限(p <= 此值才显著)
    min_shared: int = 3         # 协同事件最小次数
    min_coins: int = 2          # 最少跨币数(硬证据门槛)


@dataclass(slots=True)
class SmartScoreCfg:
    """smart_money_score 权重/封顶/折扣——全部依据文档化，非魔数。

    权重设计依据(CLAUDE.md 聪明钱原则):
      w_alltime=28  : 全期 PnL 是最可靠的 edge 证明，权重最大；
      w_month=18    : 近月盈利验证持续性，去除历史遥远运气；
      w_consistency_all=16: 三窗皆正(周/月/全期)=持续 edge，区分一次性运气；
      w_consistency_part=7 : 仅月+全期正=过渡状态，给予部分分数；
      w_roi=14      : 月化 ROI=单位本金真实 alpha，区分「大资金碰运气」与「高手」；
      w_realized=8  : 已实现盈利≠浮盈，是真实出金能力；
      w_account=8   : 账户规模=资金认可度，辅助验证地址真实性；
      w_winrate=8   : 聪明钱常低胜率高盈亏比，胜率权重低，仅辅助；
    封顶设计: 防单一维度无限拉分；churn 折扣: 高成交额低方向盈亏=做市/刷量非 alpha。
    min_trades_winrate: 胜率 Wilson 守卫触发阈(< 此值=小样本，下界大幅压缩)。
    """
    w_alltime: float = 28.0
    cap_alltime: float = 50_000_000.0     # 全期 PnL 5000 万封顶
    w_month: float = 18.0
    cap_month: float = 10_000_000.0       # 近月 PnL 1000 万封顶
    w_consistency_all: float = 16.0       # 三窗皆正=持续 edge
    w_consistency_part: float = 7.0       # 仅月+全期正=过渡
    w_roi: float = 14.0
    cap_roi_monthly: float = 0.5          # 月化 50% 封顶(防杠杆短期爆发虚高)
    w_realized: float = 8.0
    w_account: float = 8.0
    cap_account: float = 10_000_000.0     # 账户规模 1000 万封顶
    w_winrate: float = 8.0
    cap_winrate: float = 0.7              # 胜率 70% 封顶(聪明钱无需高胜率)
    churn_vol_floor: float = 1_000_000.0  # 刷量判别成交额门槛(100 万 USD)
    churn_eff_max: float = 0.001          # 方向盈亏效率上限(0.1%)，低于此=做市/刷量
    churn_penalty: float = 0.85           # 刷量整体折扣(×0.85)
    min_trades_winrate: int = 20          # 胜率最小样本(Wilson 守卫触发阈)


@dataclass(slots=True)
class HarmonicCfg:
    """Bitget 永续多周期谐波形态（Harmonic Patterns）分析配置。

    enabled=True 时周期推送卡片；interval_sec 控制推送频率（默认 15 分钟）。
    timeframes 覆盖 6 个主流周期（用户#：多周期 6tf，与布林带一致）；bars 每周期 K 线根数；
    order 枢轴邻域大小；tol 比率容差（默认 5%）；top_n 最多监控币种数。

    universe_mode:
      "top_n"    → 按 24h 成交额降序取前 top_n 个（默认，向后兼容）
      "all_perp" → 全部 Bitget USDT 永续合约按成交额降序（真实全市场覆盖）
    """
    enabled: bool = True
    interval_sec: float = 900.0
    # 用户#：谐波精确 7 周期 15m/30m/1H/4H/12H/1D/1W。
    # 历史注：曾用 6H 替代 8H（Bitget 不支持 8H），现用户明确要 30m 替代 6H。
    # Bitget GRANULARITY_MS 支持全部 7 周期（15m/30m/1H/4H/12H/1D/1W 均已实证）。
    timeframes: list[str] = field(
        default_factory=lambda: ["15m", "30m", "1H", "4H", "12H", "1D", "1W"]
    )
    bars: int = 2500                     # 用户#：每周期保留 2500 bar（历史+实时，不强制；大周期取可得）
    order: int = 3
    tol: float = 0.05
    top_n: int = 12
    # universe_mode: "top_n"(默认，向后兼容) | "all_perp"(全部 USDT 永续，高 vol 优先)
    # all_perp 模式首次 refresh 会回填全量 K 线（一次性冷启动 ~10min），之后用 DB 缓存
    universe_mode: str = "top_n"
    account_usd: float = 10_000.0        # 仓位计算用账户名义资金（USD）
    risk_pct: float = 0.01               # 单笔风险比例（1%）
    target_rr: float = 2.0               # 目标盈亏比
    # 谐波系统**专用独立飞书**（用户#：与 HL 信号分开推送）；为空则回退主 notifier
    feishu_webhook: str = ""
    feishu_secret: str = ""
    # K 线 WS 增量驱动实时性（B1）：
    # False（默认）= 纯 periodic refresh 模式（向后兼容，不影响现网）
    # True = 收盘线即触发增量谐波分析（K 线级实时，periodic refresh 保留作全量兜底）
    realtime_ws: bool = False
    # 分层调度配置（A2）：
    # core_n:      核心层币数（高 vol 前 N 个，每轮必 refresh，实时性最高）。默认 60。
    #              core_n >= top_n（或 >= 总币数）时退化为全量每轮 refresh（向后兼容）。
    # tail_shards: 长尾分片数（其余币按 round-robin 分 tail_shards 片，每轮只处理 1 片）。
    #              tail_shards=1 时长尾每轮全量 refresh（等价无分层）。默认 8。
    core_n: int = 60
    tail_shards: int = 8


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
    universe: UniverseCfg = field(default_factory=UniverseCfg)
    smart_score: SmartScoreCfg = field(default_factory=SmartScoreCfg)
    correlation: CorrelationCfg = field(default_factory=CorrelationCfg)

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
        # UniverseCfg.include/exclude 是 list，需正确透传
        univ_raw: dict[str, Any] = dict(raw.get("universe") or {})
        if "include" in univ_raw and not isinstance(univ_raw["include"], list):
            univ_raw["include"] = list(univ_raw["include"])
        if "exclude" in univ_raw and not isinstance(univ_raw["exclude"], list):
            univ_raw["exclude"] = list(univ_raw["exclude"])
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
            universe=UniverseCfg(**univ_raw),
            smart_score=SmartScoreCfg(**(raw.get("smart_score") or {})),
            correlation=CorrelationCfg(**(raw.get("correlation") or {})),
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
