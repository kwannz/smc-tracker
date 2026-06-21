"""轮询监控单测：持仓快照跨运行存取 + seed_prev 即时 diff（无网络）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.signals import WhalePositionTracker
from smc_tracker.storage import Store


def test_whale_positions_snapshot_roundtrip():
    s = Store(Path(tempfile.mkdtemp()) / "s.db")
    s.save_whale_positions([("0xa", "BTC", 100.0, 6_000_000.0, "庄#1", 1)])
    assert s.load_whale_positions() == {("0xa", "BTC"): 6_000_000.0}
    # 覆盖式保存：旧 coin 消失
    s.save_whale_positions([("0xa", "ETH", 10.0, 30_000.0, "庄#1", 2)])
    prev = s.load_whale_positions()
    assert ("0xa", "BTC") not in prev and prev[("0xa", "ETH")] == 30_000.0
    s.close()


def test_seed_prev_diffs_on_first_scan():
    """轮询模式：seed_prev 后首轮即 diff（不走基线），上次大仓位归零 → 平仓。"""
    t = WhalePositionTracker(min_notional=1_000_000)
    t.seed_prev({("0xa", "BTC"): 6_000_000.0})
    out = t.scan({}, {"BTC": 60000.0}, {"0xa": "庄#1"}, now_ms=10)
    assert len(out) == 1 and out[0].kind == "exit" and out[0].direction == "long"


def test_seed_prev_detects_reversal():
    t = WhalePositionTracker(min_notional=1_000_000)
    t.seed_prev({("0xa", "BTC"): 6_000_000.0})            # 上轮多 $6M
    out = t.scan({("0xa", "BTC"): -50.0}, {"BTC": 60000.0}, {"0xa": "庄#1"}, now_ms=10)
    assert len(out) == 1 and out[0].kind == "reversal"


def test_merge_watchlist_appends_and_dedups():
    """_merge_watchlist：config.watchlist 显式追踪地址并入排行榜庄列表(去重，追加在后)。

    小账户/非排行榜级地址经 config 声明后，cron 轮询路径也纳入监控集(否则 run_once
    只追排行榜 top_n，watchlist 永远抓不到)。庄在前、追踪地址追加在后、按地址去重(大小写不敏感)。
    """
    from smc_tracker.config import WatchAddress
    from smc_tracker.monitor.poll_monitor import _merge_watchlist

    whales = [WatchAddress("0xAAA", "庄#1"), WatchAddress("0xBBB", "庄#2")]
    wl = [WatchAddress("0xCCC", "追踪"), WatchAddress("0xaaa", "大小写重复")]
    out = _merge_watchlist(whales, wl)
    addrs = [w.address.lower() for w in out]
    assert addrs == ["0xaaa", "0xbbb", "0xccc"]   # 庄在前，追踪追加，0xaaa 去重
    assert out[2].label == "追踪"                  # 新追踪地址保留其 label
    assert len(out) == 3


def test_merge_watchlist_empty_is_noop():
    """watchlist 为空 → 原庄列表原样返回(向后兼容现状)。"""
    from smc_tracker.config import WatchAddress
    from smc_tracker.monitor.poll_monitor import _merge_watchlist

    whales = [WatchAddress("0xAAA", "庄#1")]
    out = _merge_watchlist(whales, [])
    assert [w.address for w in out] == ["0xAAA"]


def test_poll_records_and_evaluates_predictions():
    """轮询正确性闭环：前瞻信号落 predictions（含 normalize 容错），到期按真实价核对方向对错。

    MTF 化后：每个 (coin,kind) 按 7 个 TF 各记一条；4 类信号 × 7 TF 共 28 条落库。
    但注意 BTC 同时出现在「跟庄」和「共识」，kPEPE/PEPE normalize 后是同 coin 不同 kind。
    去重 key=(normalize(coin), kind) → 4 组独立信号，各记 7 条 = 28 条总。
    等最短 TF（5m=300s）到期后仅评 4 条（4 组各 1 条 5m），验证方向对错逻辑不变。
    """
    from types import SimpleNamespace

    from smc_tracker.config import Config
    from smc_tracker.monitor.poll_monitor import PollMonitor, _make_price_of

    s = Store(Path(tempfile.mkdtemp()) / "p.db")
    cfg = Config()   # 默认 7 TF：5m/15m/30m/1h/4h/12h/1d
    n_tfs = len(cfg.review.horizons_min)   # 7
    pm = PollMonitor(cfg, s, min_flow_usd=200_000.0)
    now = 1_000_000_000_000
    price_of = _make_price_of({"BTC": 60_000.0, "kPEPE": 0.01})
    flow = {"BTC": 500_000.0, "DOGE": 100.0}          # BTC 越阈值→跟庄 long；DOGE 不够→不记
    cons = [SimpleNamespace(coin="BTC", direction="long")]
    divs = [SimpleNamespace(coin="kPEPE", direction="bearish")]   # →down
    confl = [SimpleNamespace(coin="PEPE", direction="long")]      # normalize→kPEPE 命中价

    total = pm._record_predictions(flow, cons, divs, confl, price_of, now)
    # 4 组信号(跟庄BTC/共识BTC/背离kPEPE/超级PEPE) × 7 TF
    assert total == 4 * n_tfs
    rows = s.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    assert rows == 4 * n_tfs
    assert pm.review.accuracy_report(now - 1, now + 10)["total_n"] == 0  # 未到期

    # 到期评估：等最短 TF(5m=300_000ms)到期，评 4 条（每组各 1 条 5m TF）
    hz_5m = 5 * 60_000
    later = now + hz_5m + 1
    fut = _make_price_of({"BTC": 66_000.0, "kPEPE": 0.009})
    n_eval = pm.review.evaluate_due(fut, later)
    assert n_eval == 4   # 4 组信号的 5m TF 各评 1 条
    rep = pm.review.accuracy_report(now - 1, later + 10)
    assert rep["total_n"] == 4
    # 跟庄BTC long✓、共识BTC long✓、背离kPEPE down✓（kPEPE跌）、超级PEPE long✗(kPEPE跌) → 3/4
    assert rep["total_hits"] == 3
    s.close()


def test_poll_multi_horizon_recording():
    """MTF 多时间段：7 个 TF 各落一条；5m TF 先到期仅评 1 条；报告按水平线分解正确。"""
    from types import SimpleNamespace

    from smc_tracker.config import Config
    from smc_tracker.monitor.poll_monitor import PollMonitor, _make_price_of

    s = Store(Path(tempfile.mkdtemp()) / "mh.db")
    cfg = Config()
    n_tfs = len(cfg.review.horizons_min)   # 7
    expected_hzs = {h * 60_000 for h in cfg.review.horizons_min}
    pm = PollMonitor(cfg, s)
    now = 1_000_000_000_000
    price_of = _make_price_of({"BTC": 60_000.0})
    cons = [SimpleNamespace(coin="BTC", direction="short")]

    n = pm._record_predictions({}, cons, [], [], price_of, now)
    assert n == n_tfs                                  # 1 信号 × 7 水平线
    assert s.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == n_tfs
    assert {h for (h,) in s.conn.execute("SELECT DISTINCT horizon_ms FROM predictions")} == \
        expected_hzs

    # 仅 5m TF 到期：只评估 5m 那条
    hz_5m = 5 * 60_000
    fut = _make_price_of({"BTC": 59_000.0})            # 跌→做空命中
    assert pm.review.evaluate_due(fut, now + hz_5m + 1) == 1
    rep = pm.review.accuracy_report(now - 1, now + hz_5m + 1)
    assert rep["total_n"] == 1
    assert hz_5m in rep["by_horizon"]
    assert rep["by_horizon"][hz_5m]["hits"] == 1       # 5m 做空命中
    s.close()


def _insert_corr_trades(store, now_ms: int) -> None:
    """插入合成的跨币协同成交数据。

    地址 0xaa 与 0xbb 在 BTC 和 ETH 两个不同币上，近30min 内多次同向（买方向）主动成交，
    满足 min_shared=3, min_coins=2 的庄家集团识别条件（跨≥2 币是同一实体硬证据）。
    窗口=120s，不应期=120s，所以每个(coin,side)组每120s 只记一次协同事件，
    我们把成交时间间隔设>120s 以产生多个独立协同事件。
    """
    w = 120 * 1000   # 120s 窗口 = 120000ms
    rows = []
    # BTC 上：0xaa/0xbb 在 [now-18min, now-6min] 各协同 3 次，间隔 > 120s
    base_btc = now_ms - 18 * 60 * 1000
    for i in range(3):
        t = base_btc + i * (w + 5000)   # 每次间隔>不应期，产生独立协同事件
        # 0xaa 比 0xbb 早 1s（0xaa 是 leader）
        rows.append(("BTC", 60000.0, 1.0, 60000.0, "B", "0xaa", "0xff", "0xaa", None, None, t))
        rows.append(("BTC", 60000.0, 1.0, 60000.0, "B", "0xbb", "0xee", "0xbb", None, None, t + 1000))
    # ETH 上：0xaa/0xbb 在 [now-9min, now-3min] 各协同 3 次
    base_eth = now_ms - 9 * 60 * 1000
    for i in range(3):
        t = base_eth + i * (w + 5000)
        rows.append(("ETH", 3000.0, 1.0, 3000.0, "B", "0xaa", "0xff", "0xaa", None, None, t))
        rows.append(("ETH", 3000.0, 1.0, 3000.0, "B", "0xbb", "0xee", "0xbb", None, None, t + 1000))
    store.conn.executemany(
        "INSERT INTO hl_meme_trades "
        "(coin,px,sz,notional,taker_side,buyer,seller,taker,hash,tid,time_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    store.conn.commit()


def test_clusters_detected_in_digest():
    """庄家集团识别：合成跨币协同数据 → _digest 含"庄家集团"区块（消孤儿验证）。"""
    from smc_tracker.config import Config
    from smc_tracker.monitor.poll_monitor import PollMonitor
    from smc_tracker.storage import Store

    s = Store(Path(tempfile.mkdtemp()) / "c.db")
    now_ms = 1_700_000_000_000   # 固定时间戳（确定性测试）

    # 插入合成协同成交（在 since_corr=now_ms-1_800_000 内）
    _insert_corr_trades(s, now_ms)

    pm = PollMonitor(Config(), s)
    # 直接调 AddressCorrelation 验证集群确实被检测到
    from smc_tracker.monitor.address_correlation import AddressCorrelation
    corr = AddressCorrelation(s)
    since_corr = now_ms - 1_800_000
    groups = corr.clusters_detailed(since_corr, window_sec=120, min_shared=3, min_coins=2)
    # 确保合成数据产生了至少一个跨币协同群
    assert len(groups) >= 1, f"期望至少1个群，实际: {groups}"
    assert groups[0]["coins"] >= 2, f"期望跨≥2 币，实际: {groups[0]['coins']}"
    assert groups[0]["size"] >= 2, f"期望群大小≥2，实际: {groups[0]['size']}"

    # 为群补充 _leader 信息（0xaa 比 0xbb 早，应为 leader）
    for d in groups:
        d["_leader"] = corr.cluster_leader(d["members"], since_corr, window_sec=120)

    # 调 _digest 验证庄家集团区块存在
    digest = pm._digest(
        whales=[], positions={}, changes=[], cons=[], confl=[],
        panel=[], flow={}, divs=[], prev={}, now_ms=now_ms,
        groups=groups)
    assert "庄家集团" in digest, f"digest 应含庄家集团区块:\n{digest}"
    assert "跨" in digest and "币" in digest, f"digest 应含跨币信息:\n{digest}"

    s.close()


def test_clusters_leader_in_digest():
    """庄家集团 leader 信息：0xaa 先于 0xbb → digest 含 leader 字样（领先N次）。"""
    from smc_tracker.config import Config
    from smc_tracker.monitor.poll_monitor import PollMonitor
    from smc_tracker.monitor.address_correlation import AddressCorrelation
    from smc_tracker.storage import Store

    s = Store(Path(tempfile.mkdtemp()) / "l.db")
    now_ms = 1_700_000_000_000

    _insert_corr_trades(s, now_ms)
    corr = AddressCorrelation(s)
    since_corr = now_ms - 1_800_000
    groups = corr.clusters_detailed(since_corr, window_sec=120, min_shared=3, min_coins=2)
    assert groups, "合成数据应产生至少一个群"
    for d in groups:
        d["_leader"] = corr.cluster_leader(d["members"], since_corr, window_sec=120)

    # 确保 leader 被识别（0xaa 在每次协同中均先于 0xbb 成交）
    leaders = [d["_leader"] for d in groups if d.get("_leader")]
    assert leaders, "应识别出 leader"
    assert leaders[0][0] == "0xaa", f"0xaa 应为 leader，实际: {leaders[0]}"
    assert leaders[0][1] > 0, "leader 领先次数应 > 0"

    pm = PollMonitor(Config(), s)
    digest = pm._digest(
        whales=[], positions={}, changes=[], cons=[], confl=[],
        panel=[], flow={}, divs=[], prev={}, now_ms=now_ms,
        groups=groups)
    # leader 信息应出现在 digest 中
    assert "leader" in digest, f"digest 应含 leader 字样:\n{digest}"
    assert "领先" in digest, "digest 应含领先信息"

    s.close()


def test_no_groups_no_section():
    """无协同群时，_digest 不显示庄家集团区块（不推空段）。"""
    from smc_tracker.config import Config
    from smc_tracker.monitor.poll_monitor import PollMonitor
    from smc_tracker.storage import Store

    s = Store(Path(tempfile.mkdtemp()) / "ng.db")
    pm = PollMonitor(Config(), s)
    digest = pm._digest(
        whales=[], positions={}, changes=[], cons=[], confl=[],
        panel=[], flow={}, divs=[], prev={}, now_ms=1_000_000_000,
        groups=[])
    assert "庄家集团" not in digest, "无群时 digest 不应含庄家集团区块"
    s.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
