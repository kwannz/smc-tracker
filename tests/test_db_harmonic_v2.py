"""DB schema v2 单元测试：XABCD点列 + 历史保留 + bb_levels。

严格 TDD — 先写失败测试(RED), 实现后变绿(GREEN)。
合成数据, 无网络, 用临时库。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.storage import Store


# --------------------------------------------------------------------------- #
# 辅助工厂                                                                     #
# --------------------------------------------------------------------------- #

def _store() -> Store:
    """每个测试独立临时库, 隔离无状态。"""
    d = tempfile.mkdtemp()
    return Store(Path(d) / "test_v2.db")


def _row19(ts: int = 1000, coin: str = "BTC", tf: str = "1H") -> tuple:
    """构造旧格式 19 列谐波行（向后兼容基准）。"""
    return (
        ts, coin, tf,
        "completed", "Gartley", "long", 50000.0,
        49000.0, 51000.0, 48000.0, 53000.0, 55000.0,
        2.0, 0.85, "✓", "✓bid100k", "XB=0.618",
        49500.0, 50500.0,
    )


def _row29(ts: int = 1000, coin: str = "BTC", tf: str = "1H",
           x_idx: int | None = 10, x_px: float | None = 45000.0,
           a_idx: int | None = 20, a_px: float | None = 47000.0,
           b_idx: int | None = 30, b_px: float | None = 46000.0,
           c_idx: int | None = 40, c_px: float | None = 48000.0,
           d_idx: int | None = 50, d_px: float | None = 49500.0) -> tuple:
    """构造新格式 29 列谐波行（含 XABCD 点）。"""
    base = _row19(ts=ts, coin=coin, tf=tf)
    return base + (x_idx, x_px, a_idx, a_px, b_idx, b_px, c_idx, c_px, d_idx, d_px)


# --------------------------------------------------------------------------- #
# 1. 新列往返                                                                  #
# --------------------------------------------------------------------------- #

def test_xabcd_columns_roundtrip():
    """29 列行写入后, 能从 DB 读出所有 XABCD 点列。"""
    s = _store()
    row = _row29(ts=2000, coin="ETH", tf="4H")
    s.insert_harmonic_setups([row])

    rows = s.recent_harmonic_setups()
    assert len(rows) == 1, f"期望 1 行, 实际 {len(rows)}"
    r = rows[0]
    # 列总数: 19 + 10 = 29
    assert len(r) == 29, f"期望 29 列, 实际 {len(r)}"
    # 前 19 列不变
    assert r[0] == 2000    # ts
    assert r[1] == "ETH"   # coin
    assert r[2] == "4H"    # tf
    # XABCD 新列（索引 19-28）
    x_idx, x_px, a_idx, a_px, b_idx, b_px, c_idx, c_px, d_idx, d_px = r[19:]
    assert x_idx == 10
    assert abs(x_px - 45000.0) < 1e-9
    assert a_idx == 20
    assert abs(a_px - 47000.0) < 1e-9
    assert b_idx == 30
    assert abs(b_px - 46000.0) < 1e-9
    assert c_idx == 40
    assert abs(c_px - 48000.0) < 1e-9
    assert d_idx == 50
    assert abs(d_px - 49500.0) < 1e-9
    s.close()


def test_xabcd_columns_null_ok():
    """XABCD 列允许为 None（forming 形态尚未完成时合法）。"""
    s = _store()
    row = _row29(x_idx=None, x_px=None, a_idx=None, a_px=None,
                 b_idx=None, b_px=None, c_idx=None, c_px=None,
                 d_idx=None, d_px=None)
    s.insert_harmonic_setups([row])
    rows = s.recent_harmonic_setups()
    assert len(rows) == 1
    r = rows[0]
    # 所有 XABCD 列均为 None
    for val in r[19:]:
        assert val is None, f"期望 None, 实际 {val!r}"
    s.close()


# --------------------------------------------------------------------------- #
# 2. 向后兼容：旧 19 列 insert 不崩                                            #
# --------------------------------------------------------------------------- #

def test_backward_compat_19col_insert():
    """旧 19 列行 insert_harmonic_setups 不抛异常, 新 XABCD 列填 NULL。"""
    s = _store()
    old_row = _row19(ts=500, coin="SOL", tf="15m")
    # 不应抛异常
    s.insert_harmonic_setups([old_row])

    rows = s.recent_harmonic_setups()
    assert len(rows) == 1
    r = rows[0]
    assert len(r) == 29, f"SELECT 应返回 29 列, 实际 {len(r)}"
    # 前 19 列值正确
    assert r[0] == 500
    assert r[1] == "SOL"
    # XABCD 列为 NULL
    for val in r[19:]:
        assert val is None, f"旧行新列应为 None, 实际 {val!r}"
    s.close()


def test_backward_compat_mixed_rows():
    """同批 insert 中混入 19 列和 29 列行, 均不崩且正确存储。

    同批行同 ts（模拟真实同快照批次），recent 返回全部。
    分别验证 XABCD 有值行和无值行均正确存储。
    """
    s = _store()
    # 同 ts 同批, 19 列(BTC) 和 29 列(ETH) 混入
    old_row = _row19(ts=1000, coin="BTC", tf="1H")   # ts=1000, 旧格式
    new_row = _row29(ts=1000, coin="ETH", tf="4H")   # ts=1000, 新格式
    s.insert_harmonic_setups([old_row, new_row])

    # 总行数应为 2（均已入库）
    total = s.conn.execute("SELECT COUNT(*) FROM harmonic_setups").fetchone()[0]
    assert total == 2, f"期望 2 行, 实际 {total}"

    # recent 取 ts=max(ts)=1000, 应返回 2 行
    rows = s.recent_harmonic_setups()
    assert len(rows) == 2, f"recent 期望 2 行(同 ts 批), 实际 {len(rows)}"

    # ETH 新行: XABCD 有值
    eth_rows = [r for r in rows if r[1] == "ETH"]
    assert len(eth_rows) == 1
    assert eth_rows[0][19] == 10    # x_idx

    # BTC 旧行: XABCD 均为 None
    btc_rows = [r for r in rows if r[1] == "BTC"]
    assert len(btc_rows) == 1
    for val in btc_rows[0][19:]:
        assert val is None
    s.close()


# --------------------------------------------------------------------------- #
# 3. 历史保留（不再 DELETE-then-insert, 带 ts 追加）                           #
# --------------------------------------------------------------------------- #

def test_history_append_not_delete():
    """多批 insert 后表行数累积（不清空）, 每批 ts 不同。"""
    s = _store()
    batch1 = [_row29(ts=1000, coin="BTC", tf="1H"),
              _row29(ts=1000, coin="ETH", tf="1H")]
    batch2 = [_row29(ts=2000, coin="BTC", tf="1H"),
              _row29(ts=2000, coin="ETH", tf="1H"),
              _row29(ts=2000, coin="SOL", tf="1H")]
    s.insert_harmonic_setups(batch1)
    s.insert_harmonic_setups(batch2)

    # 表内应有 2+3=5 行（不是只有最新批的 3 行）
    total = s.conn.execute("SELECT COUNT(*) FROM harmonic_setups").fetchone()[0]
    assert total == 5, f"期望 5 行(历史累积), 实际 {total}"
    s.close()


def test_recent_harmonic_setups_returns_latest_snapshot():
    """recent_harmonic_setups() 返回 ts=max(ts) 的批次, 不含旧批。"""
    s = _store()
    batch1 = [_row29(ts=1000, coin="BTC", tf="1H")]
    batch2 = [_row29(ts=2000, coin="ETH", tf="4H"),
              _row29(ts=2000, coin="SOL", tf="4H")]
    s.insert_harmonic_setups(batch1)
    s.insert_harmonic_setups(batch2)

    recent = s.recent_harmonic_setups()
    # 应只返回 ts=2000 的批次（2 行）
    assert len(recent) == 2, f"期望 2 行(最新快照), 实际 {len(recent)}"
    for r in recent:
        assert r[0] == 2000, f"期望 ts=2000, 实际 {r[0]}"
    s.close()


def test_recent_harmonic_setups_empty():
    """空表时 recent_harmonic_setups() 返回空列表, 不抛。"""
    s = _store()
    result = s.recent_harmonic_setups()
    assert result == []
    s.close()


def test_harmonic_history_returns_all_for_coin():
    """harmonic_history(coin) 返回该币所有历史形态行, ts 降序。"""
    s = _store()
    # BTC 有 3 批（ts=1000/2000/3000）, ETH 只有 1 批
    s.insert_harmonic_setups([_row29(ts=1000, coin="BTC", tf="1H")])
    s.insert_harmonic_setups([_row29(ts=2000, coin="BTC", tf="4H"),
                               _row29(ts=2000, coin="ETH", tf="4H")])
    s.insert_harmonic_setups([_row29(ts=3000, coin="BTC", tf="1H")])

    btc_hist = s.harmonic_history("BTC")
    assert len(btc_hist) == 3, f"BTC 期望 3 行, 实际 {len(btc_hist)}"
    # ts 降序
    tss = [r[0] for r in btc_hist]
    assert tss == sorted(tss, reverse=True), f"期望 ts 降序, 实际 {tss}"

    eth_hist = s.harmonic_history("ETH")
    assert len(eth_hist) == 1
    assert eth_hist[0][1] == "ETH"
    s.close()


def test_harmonic_history_limit():
    """harmonic_history(coin, limit=2) 最多返回 limit 行。"""
    s = _store()
    for ts in range(1000, 6000, 1000):
        s.insert_harmonic_setups([_row29(ts=ts, coin="BTC", tf="1H")])
    hist = s.harmonic_history("BTC", limit=2)
    assert len(hist) == 2
    # 最新的两条
    assert hist[0][0] == 5000
    assert hist[1][0] == 4000
    s.close()


def test_harmonic_history_empty_coin():
    """该币没有历史时返回空列表, 不抛。"""
    s = _store()
    s.insert_harmonic_setups([_row29(ts=1000, coin="ETH", tf="1H")])
    result = s.harmonic_history("UNKNOWN_COIN_XYZ")
    assert result == []
    s.close()


def test_harmonic_history_default_limit_50():
    """harmonic_history 默认 limit=50, 不截断 50 行以内历史。"""
    s = _store()
    for i in range(30):
        s.insert_harmonic_setups([_row29(ts=i * 100 + 100, coin="BTC", tf="1H")])
    hist = s.harmonic_history("BTC")
    assert len(hist) == 30, f"30 行历史应全部返回, 实际 {len(hist)}"
    s.close()


# --------------------------------------------------------------------------- #
# 4. bb_levels 表往返                                                          #
# --------------------------------------------------------------------------- #

def _bb_row(coin: str = "BTC", tf: str = "1H", ts: int = 1000,
            upper: float = 52000.0, mid: float = 50000.0,
            lower: float = 48000.0, pct_b: float = 0.75,
            squeeze: int = 0) -> tuple:
    """构造 bb_levels 行。"""
    return (coin, tf, ts, upper, mid, lower, pct_b, squeeze)


def test_bb_levels_roundtrip():
    """bb_levels 行写入后能完整读回所有字段。"""
    s = _store()
    row = _bb_row(coin="BTC", tf="4H", ts=2000, upper=55000.0, mid=50000.0,
                  lower=45000.0, pct_b=0.8, squeeze=1)
    s.insert_bb_levels([row])

    result = s.conn.execute(
        "SELECT coin,tf,ts,upper,mid,lower,pct_b,squeeze FROM bb_levels"
    ).fetchall()
    assert len(result) == 1
    r = result[0]
    assert r[0] == "BTC"
    assert r[1] == "4H"
    assert r[2] == 2000
    assert abs(r[3] - 55000.0) < 1e-9
    assert abs(r[4] - 50000.0) < 1e-9
    assert abs(r[5] - 45000.0) < 1e-9
    assert abs(r[6] - 0.8) < 1e-9
    assert r[7] == 1
    s.close()


def test_bb_levels_primary_key():
    """bb_levels PRIMARY KEY(coin,tf,ts): 同 coin+tf+ts 重复写入覆盖, 不报错, 不重复行。"""
    s = _store()
    row1 = _bb_row(coin="ETH", tf="1H", ts=1000, upper=4000.0, mid=3800.0, lower=3600.0)
    row2 = _bb_row(coin="ETH", tf="1H", ts=1000, upper=4100.0, mid=3900.0, lower=3700.0)
    s.insert_bb_levels([row1])
    s.insert_bb_levels([row2])  # 同 PK, 应覆盖

    cnt = s.conn.execute("SELECT COUNT(*) FROM bb_levels").fetchone()[0]
    assert cnt == 1, f"期望 1 行(PK覆盖), 实际 {cnt}"
    # 验证是最新值
    r = s.conn.execute("SELECT upper FROM bb_levels").fetchone()
    assert abs(r[0] - 4100.0) < 1e-9
    s.close()


def test_bb_levels_multiple_tfs():
    """同一 coin 多 tf, insert_bb_levels 批量写入各 tf 行。"""
    s = _store()
    rows = [
        _bb_row(coin="BTC", tf="15m", ts=1000),
        _bb_row(coin="BTC", tf="1H",  ts=1000),
        _bb_row(coin="BTC", tf="4H",  ts=1000),
        _bb_row(coin="BTC", tf="6H",  ts=1000),
        _bb_row(coin="BTC", tf="12H", ts=1000),
        _bb_row(coin="BTC", tf="1D",  ts=1000),
        _bb_row(coin="BTC", tf="1W",  ts=1000),
    ]
    s.insert_bb_levels(rows)
    cnt = s.conn.execute("SELECT COUNT(*) FROM bb_levels WHERE coin='BTC'").fetchone()[0]
    assert cnt == 7, f"7 个周期应各 1 行, 实际 {cnt}"
    s.close()


def test_recent_bb_levels_returns_latest_per_tf():
    """recent_bb_levels(coin) 每 tf 返回最新一条(ts最大)。"""
    s = _store()
    # 同 BTC/1H 写两批 ts, 旧 ts=1000 / 新 ts=2000
    rows_old = [
        _bb_row(coin="BTC", tf="1H", ts=1000, upper=51000.0),
        _bb_row(coin="BTC", tf="4H", ts=1000, upper=52000.0),
    ]
    rows_new = [
        _bb_row(coin="BTC", tf="1H", ts=2000, upper=53000.0),
        _bb_row(coin="BTC", tf="4H", ts=2000, upper=54000.0),
    ]
    s.insert_bb_levels(rows_old)
    s.insert_bb_levels(rows_new)

    latest = s.recent_bb_levels("BTC")
    # 应返回 2 行(各 tf 最新)
    assert len(latest) == 2, f"期望 2 行(各tf最新), 实际 {len(latest)}"
    tf_map = {r[1]: r for r in latest}  # tf -> row(coin,tf,ts,upper,...)
    assert abs(tf_map["1H"][3] - 53000.0) < 1e-9, "1H 应是最新 upper=53000"
    assert abs(tf_map["4H"][3] - 54000.0) < 1e-9, "4H 应是最新 upper=54000"
    # ts 应均为 2000
    for r in latest:
        assert r[2] == 2000, f"期望 ts=2000, 实际 {r[2]}"
    s.close()


def test_recent_bb_levels_empty():
    """该 coin 无 bb_levels 时返回空列表, 不抛。"""
    s = _store()
    result = s.recent_bb_levels("UNKNOWN_COIN_ZZZ")
    assert result == [], f"期望 [], 实际 {result!r}"
    s.close()


def test_recent_bb_levels_isolates_coins():
    """recent_bb_levels 只返回指定 coin 的行, 不混入其他 coin。"""
    s = _store()
    s.insert_bb_levels([
        _bb_row(coin="BTC", tf="1H", ts=1000),
        _bb_row(coin="ETH", tf="1H", ts=1000),
    ])
    btc = s.recent_bb_levels("BTC")
    assert all(r[0] == "BTC" for r in btc)
    eth = s.recent_bb_levels("ETH")
    assert all(r[0] == "ETH" for r in eth)
    s.close()


def test_insert_bb_levels_empty_rows():
    """insert_bb_levels([]) 空输入安全返回, 不抛。"""
    s = _store()
    s.insert_bb_levels([])  # 不应抛异常
    cnt = s.conn.execute("SELECT COUNT(*) FROM bb_levels").fetchone()[0]
    assert cnt == 0
    s.close()


# --------------------------------------------------------------------------- #
# 5. harmonic_setups/bb_levels 加入 _DB_RETAIN                                #
# --------------------------------------------------------------------------- #

def test_harmonic_setups_in_db_retain():
    """harmonic_setups 必须出现在 app._DB_RETAIN 中。"""
    from smc_tracker.app import TradingSystem
    retain_tables = {entry[0] for entry in TradingSystem._DB_RETAIN}
    assert "harmonic_setups" in retain_tables, (
        "harmonic_setups 未加入 _DB_RETAIN, 历史数据会无限增长!"
    )


def test_bb_levels_in_db_retain():
    """bb_levels 必须出现在 app._DB_RETAIN 中。"""
    from smc_tracker.app import TradingSystem
    retain_tables = {entry[0] for entry in TradingSystem._DB_RETAIN}
    assert "bb_levels" in retain_tables, (
        "bb_levels 未加入 _DB_RETAIN, 历史数据会无限增长!"
    )


def test_harmonic_setups_retain_at_least_7_days():
    """harmonic_setups 保留期 ≥ 7 天（供历史回看）。"""
    from smc_tracker.app import TradingSystem
    retain_map = {entry[0]: entry[2] for entry in TradingSystem._DB_RETAIN}
    assert retain_map.get("harmonic_setups", 0) >= 7 * 86_400_000, (
        "harmonic_setups 保留期应 ≥ 7 天"
    )


def test_bb_levels_retain_at_least_7_days():
    """bb_levels 保留期 ≥ 7 天（多周期 S/R 历史）。"""
    from smc_tracker.app import TradingSystem
    retain_map = {entry[0]: entry[2] for entry in TradingSystem._DB_RETAIN}
    assert retain_map.get("bb_levels", 0) >= 7 * 86_400_000, (
        "bb_levels 保留期应 ≥ 7 天"
    )


# --------------------------------------------------------------------------- #
# 6. 边界: 大批量 / 混合 ts / 多币多 tf                                        #
# --------------------------------------------------------------------------- #

def test_large_batch_insert_harmonic():
    """100 行 29 列大批量写入不崩, 行数正确。"""
    s = _store()
    rows = [_row29(ts=i * 100 + 1000, coin=f"COIN{i % 10}", tf="1H")
            for i in range(100)]
    s.insert_harmonic_setups(rows)
    cnt = s.conn.execute("SELECT COUNT(*) FROM harmonic_setups").fetchone()[0]
    assert cnt == 100
    s.close()


def test_recent_harmonic_setups_confidence_order():
    """recent_harmonic_setups 按 confidence DESC 排序。"""
    s = _store()
    # 同 ts 批次, 置信度各不同
    def _with_conf(coin: str, conf: float) -> tuple:
        r = list(_row29(ts=5000, coin=coin, tf="1H"))
        r[13] = conf   # confidence 在第 14 列（索引 13）
        return tuple(r)

    s.insert_harmonic_setups([
        _with_conf("LOW",  0.3),
        _with_conf("HIGH", 0.9),
        _with_conf("MID",  0.6),
    ])
    rows = s.recent_harmonic_setups()
    confs = [r[13] for r in rows]
    assert confs == sorted(confs, reverse=True), f"期望降序, 实际 {confs}"
    s.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except Exception as exc:
                print(f"  ✗ {name}: {exc}")
    print("done")
