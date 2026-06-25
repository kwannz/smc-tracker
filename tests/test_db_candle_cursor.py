"""DB K 线增量游标功能单元测试。

验证：
1. 不传 since_ms 行为与原完全一致（最近 N 根升序）
2. 传 since_ms 只返回更新的 K 线且升序排列
3. latest_candle_ms 返回最大 open_ms；无数据返回 None
4. 旧 get_candles(coin, tf) 调用签名零影响
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
    """每个测试独立临时库，隔离无状态。"""
    d = tempfile.mkdtemp()
    return Store(Path(d) / "test_cursor.db")


def _insert_candles(store: Store, coin: str, tf: str, open_ms_list: list[int]) -> None:
    """插入合成 K 线（open_ms 为唯一变量，OHLCV 填固定值）。"""
    rows = [
        (coin, tf, ms, 100.0, 110.0, 90.0, 105.0, 1000.0)
        for ms in open_ms_list
    ]
    store.upsert_candles(rows)
    store.conn.commit()


# --------------------------------------------------------------------------- #
# 1. 不传 since_ms — 与原行为完全一致                                          #
# --------------------------------------------------------------------------- #

def test_get_candles_no_since_ms_returns_latest_n_asc() -> None:
    """不传 since_ms 时返回最近 limit 根，升序排列，与原行为一致。"""
    s = _store()
    # 插入 10 根 K 线，open_ms = 1000, 2000, ..., 10000
    ms_list = [i * 1000 for i in range(1, 11)]
    _insert_candles(s, "BTC", "1H", ms_list)

    # 请求最近 5 根（默认升序）
    candles = s.get_candles("BTC", "1H", limit=5)
    assert len(candles) == 5
    # 应该是最新的 5 根：6000..10000，升序
    expected_ms = [6000, 7000, 8000, 9000, 10000]
    actual_ms = [c.open_time_ms for c in candles]
    assert actual_ms == expected_ms, f"期望 {expected_ms}，实际 {actual_ms}"


def test_get_candles_no_since_ms_empty_returns_empty_list() -> None:
    """空表不传 since_ms 返回空列表。"""
    s = _store()
    result = s.get_candles("ETH", "4H")
    assert result == []


def test_get_candles_no_since_ms_less_than_limit() -> None:
    """数据少于 limit 时，返回全部 K 线（升序）。"""
    s = _store()
    _insert_candles(s, "BTC", "1H", [1000, 2000, 3000])
    candles = s.get_candles("BTC", "1H", limit=100)
    assert len(candles) == 3
    actual_ms = [c.open_time_ms for c in candles]
    assert actual_ms == [1000, 2000, 3000]


# --------------------------------------------------------------------------- #
# 2. 传 since_ms — 只返回游标之后的 K 线，升序                                #
# --------------------------------------------------------------------------- #

def test_get_candles_since_ms_returns_only_newer_asc() -> None:
    """传 since_ms=5000 只返回 open_ms > 5000 的 K 线，升序。"""
    s = _store()
    ms_list = [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000]
    _insert_candles(s, "BTC", "1H", ms_list)

    candles = s.get_candles("BTC", "1H", since_ms=5000)
    assert len(candles) == 3  # 6000, 7000, 8000
    actual_ms = [c.open_time_ms for c in candles]
    assert actual_ms == [6000, 7000, 8000], f"实际: {actual_ms}"


def test_get_candles_since_ms_at_boundary_exclusive() -> None:
    """since_ms 使用严格大于（>），游标本身不包含。"""
    s = _store()
    _insert_candles(s, "BTC", "1H", [1000, 2000, 3000])

    # 游标恰好等于最后一根 open_ms
    candles = s.get_candles("BTC", "1H", since_ms=3000)
    assert candles == [], "游标等于最大 open_ms 时应返回空列表"


def test_get_candles_since_ms_all_older_returns_empty() -> None:
    """所有 K 线都早于 since_ms 时返回空列表。"""
    s = _store()
    _insert_candles(s, "BTC", "1H", [1000, 2000, 3000])
    candles = s.get_candles("BTC", "1H", since_ms=99999)
    assert candles == []


def test_get_candles_since_ms_ignores_limit_param() -> None:
    """since_ms 模式应返回游标之后的所有 K 线，不受 limit 截断。"""
    s = _store()
    # 插入 20 根
    _insert_candles(s, "BTC", "1H", [i * 1000 for i in range(1, 21)])

    # limit=5 但 since_ms 之后有 15 根
    candles = s.get_candles("BTC", "1H", limit=5, since_ms=5000)
    assert len(candles) == 15  # 6000..20000 全部返回
    actual_ms = [c.open_time_ms for c in candles]
    assert actual_ms == [i * 1000 for i in range(6, 21)]


def test_get_candles_since_ms_candle_fields() -> None:
    """since_ms 模式返回的 Candle 对象字段完整正确。"""
    s = _store()
    # 只插一行合成数据
    s.upsert_candles([("ETH", "15", 5000, 1.0, 1.2, 0.9, 1.1, 500.0)])
    s.conn.commit()

    candles = s.get_candles("ETH", "15", since_ms=4999)
    assert len(candles) == 1
    c = candles[0]
    assert c.coin == "ETH"
    assert c.interval == "15"
    assert c.open_time_ms == 5000
    assert c.o == 1.0
    assert c.h == 1.2
    assert c.l == 0.9
    assert c.c == 1.1
    assert c.v == 500.0


# --------------------------------------------------------------------------- #
# 3. latest_candle_ms                                                          #
# --------------------------------------------------------------------------- #

def test_latest_candle_ms_returns_max_open_ms() -> None:
    """有数据时返回最大 open_ms。"""
    s = _store()
    _insert_candles(s, "BTC", "1H", [3000, 1000, 7000, 5000])
    result = s.latest_candle_ms("BTC", "1H")
    assert result == 7000, f"期望 7000，实际 {result}"


def test_latest_candle_ms_empty_returns_none() -> None:
    """无数据时返回 None。"""
    s = _store()
    result = s.latest_candle_ms("BTC", "1H")
    assert result is None


def test_latest_candle_ms_coin_tf_isolated() -> None:
    """不同 coin/tf 的数据互不干扰。"""
    s = _store()
    _insert_candles(s, "BTC", "1H", [9000, 8000])
    _insert_candles(s, "ETH", "1H", [3000, 2000])
    _insert_candles(s, "BTC", "4H", [5000])

    assert s.latest_candle_ms("BTC", "1H") == 9000
    assert s.latest_candle_ms("ETH", "1H") == 3000
    assert s.latest_candle_ms("BTC", "4H") == 5000
    assert s.latest_candle_ms("SOL", "1H") is None


# --------------------------------------------------------------------------- #
# 4. 旧调用签名零影响                                                           #
# --------------------------------------------------------------------------- #

def test_old_signature_positional_args() -> None:
    """旧式调用 get_candles(coin, tf) 和 get_candles(coin, tf, limit) 零影响。"""
    s = _store()
    _insert_candles(s, "BTC", "1H", [1000, 2000, 3000, 4000, 5000])

    # 仅传两个位置参数（旧调用）
    c1 = s.get_candles("BTC", "1H")
    assert len(c1) == 5
    assert [c.open_time_ms for c in c1] == [1000, 2000, 3000, 4000, 5000]

    # 传 limit（旧调用）
    c2 = s.get_candles("BTC", "1H", 3)
    assert len(c2) == 3
    assert [c.open_time_ms for c in c2] == [3000, 4000, 5000]


def test_old_signature_keyword_limit() -> None:
    """旧式关键字参数 get_candles(coin, tf, limit=N) 行为不变。"""
    s = _store()
    _insert_candles(s, "BTC", "1H", [10, 20, 30, 40, 50])
    candles = s.get_candles("BTC", "1H", limit=2)
    assert len(candles) == 2
    # 最近 2 根升序
    assert [c.open_time_ms for c in candles] == [40, 50]


def test_since_ms_none_explicit_same_as_omitted() -> None:
    """显式传 since_ms=None 与不传等价。"""
    s = _store()
    _insert_candles(s, "BTC", "1H", [100, 200, 300])

    c_omit = s.get_candles("BTC", "1H", limit=10)
    c_none = s.get_candles("BTC", "1H", limit=10, since_ms=None)
    assert [c.open_time_ms for c in c_omit] == [c.open_time_ms for c in c_none]


# --------------------------------------------------------------------------- #
# 5. candles_for_draw — 只读轻量元组，供 SVG 绘制                              #
# --------------------------------------------------------------------------- #

def test_candles_for_draw_returns_tuples_asc() -> None:
    """candles_for_draw 返回 (open_ms, o, h, l, c) 元组列表，升序。"""
    s = _store()
    # 插入 5 根带差异 OHLC 的 K 线
    rows = [
        ("BTC", "1H", 1000, 100.0, 110.0, 90.0, 105.0, 1000.0),
        ("BTC", "1H", 2000, 105.0, 115.0, 95.0, 108.0, 1200.0),
        ("BTC", "1H", 3000, 108.0, 120.0, 100.0, 112.0, 800.0),
    ]
    s.upsert_candles(rows)
    s.conn.commit()

    result = s.candles_for_draw("BTC", "1H", limit=10)
    assert len(result) == 3
    # 升序
    assert result[0][0] == 1000
    assert result[1][0] == 2000
    assert result[2][0] == 3000
    # 每个元素是 5 列元组 (open_ms, o, h, l, c)
    assert len(result[0]) == 5
    # 数值正确
    assert result[0][1] == 100.0   # o
    assert result[0][2] == 110.0   # h
    assert result[0][3] == 90.0    # l
    assert result[0][4] == 105.0   # c


def test_candles_for_draw_limit_applied() -> None:
    """candles_for_draw limit 截断最近 N 根（与 get_candles 相同窗口）。"""
    s = _store()
    ms_list = [i * 1000 for i in range(1, 11)]  # 1000..10000
    _insert_candles(s, "ETH", "4H", ms_list)

    result = s.candles_for_draw("ETH", "4H", limit=3)
    assert len(result) == 3
    # 最近 3 根：8000, 9000, 10000，升序
    assert result[0][0] == 8000
    assert result[1][0] == 9000
    assert result[2][0] == 10000


def test_candles_for_draw_empty_returns_empty_list() -> None:
    """无数据时返回 []，不抛。"""
    s = _store()
    result = s.candles_for_draw("SOL", "1H")
    assert result == []


def test_candles_for_draw_coin_tf_isolated() -> None:
    """不同 coin/tf 数据互不干扰。"""
    s = _store()
    _insert_candles(s, "BTC", "1H", [1000, 2000])
    _insert_candles(s, "ETH", "4H", [5000, 6000, 7000])

    btc = s.candles_for_draw("BTC", "1H")
    assert len(btc) == 2
    assert all(r[0] in (1000, 2000) for r in btc)

    eth = s.candles_for_draw("ETH", "4H")
    assert len(eth) == 3

    # 不存在的 coin 返回 []
    sol = s.candles_for_draw("SOL", "1H")
    assert sol == []


def test_candles_for_draw_default_limit_300() -> None:
    """默认 limit=300，不截断 300 以内的数据。"""
    s = _store()
    ms_list = [i * 1000 for i in range(1, 201)]  # 200 根
    _insert_candles(s, "BTC", "1D", ms_list)

    result = s.candles_for_draw("BTC", "1D")
    assert len(result) == 200  # 200 < 300，全部返回
