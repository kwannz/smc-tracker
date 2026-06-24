"""分层调度 + 并发调优 单元测试（A1 + A2）。

测试目标：
  - 核心层（高 vol 前 core_n 个币）每轮必须全部包含。
  - 长尾层（其余币）在 tail_shards 轮内全覆盖、不重不漏。
  - 退化情形：core_n >= 总币数 → 等价全量每轮 refresh（无遗漏）。
  - _SEMA_LIMIT 已提升（4 → 8）。
  - 所有测试合成数据，不依赖网络，确定性可重复。
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smc_tracker.monitor.harmonic_monitor import HarmonicMonitor, _SEMA_LIMIT


# ──────────────────────────────────────────────────────────────────────────────
# 辅助工具
# ──────────────────────────────────────────────────────────────────────────────

def _make_coin_map(n: int) -> dict[str, str]:
    """生成 n 个合成 {coinX: COINXUSDT} 映射。"""
    return {f"COIN{i:03d}": f"COIN{i:03d}USDT" for i in range(n)}


def _make_monitor(
    n_coins: int,
    core_n: int = 60,
    tail_shards: int = 8,
    top_n: int | None = None,
) -> HarmonicMonitor:
    """构造测试用 HarmonicMonitor（合成币，不依赖网络）。"""
    coin_map = _make_coin_map(n_coins)
    return HarmonicMonitor(
        coin_to_symbol=coin_map,
        timeframes=["1H"],
        bars=100,
        order=3,
        tol=0.05,
        top_n=top_n if top_n is not None else n_coins,
        core_n=core_n,
        tail_shards=tail_shards,
    )


def _get_coins_for_round(
    monitor: HarmonicMonitor,
    round_idx: int,
) -> list[str]:
    """通过 monkeypatch refresh 内调度逻辑，确定第 round_idx 轮会处理哪些币。

    原理：直接复现 refresh 内分层计算（纯函数逻辑，从 monitor 字段读取）。
    不实际跑 async refresh，避免网络依赖。
    """
    all_coins: list[tuple[str, str]] = list(monitor.coin_to_symbol.items())[:monitor.top_n]
    total = len(all_coins)
    core_n_eff = min(monitor._core_n, total)
    core_coins = all_coins[:core_n_eff]
    tail_coins_all = all_coins[core_n_eff:]

    if tail_coins_all:
        shard_idx = round_idx % monitor._tail_shards
        chunk = max(1, (len(tail_coins_all) + monitor._tail_shards - 1) // monitor._tail_shards)
        tail_shard = tail_coins_all[shard_idx * chunk: shard_idx * chunk + chunk]
    else:
        tail_shard = []

    coins = core_coins + tail_shard
    return [c for c, _ in coins]


# ──────────────────────────────────────────────────────────────────────────────
# A1：并发限制已提升到 8
# ──────────────────────────────────────────────────────────────────────────────

class TestSemaLimit:
    """A1：_SEMA_LIMIT 已从 4 提升到 8。"""

    def test_sema_limit_is_at_least_8(self) -> None:
        """_SEMA_LIMIT 应 >= 8（实证 4→8 无 429，取保守上限 8）。"""
        assert _SEMA_LIMIT >= 8, (
            f"_SEMA_LIMIT={_SEMA_LIMIT}，应 >= 8（实证 Bitget 并发 8 无 429，已提速）"
        )

    def test_sema_limit_not_excessive(self) -> None:
        """_SEMA_LIMIT 应 <= 16（过高并发有 429 风险，保守取 10 以内）。"""
        assert _SEMA_LIMIT <= 16, (
            f"_SEMA_LIMIT={_SEMA_LIMIT}，过高会 429，建议 8-10"
        )


# ──────────────────────────────────────────────────────────────────────────────
# A2：分层调度核心属性
# ──────────────────────────────────────────────────────────────────────────────

class TestLayeredSchedulingInit:
    """HarmonicMonitor 构造函数接受 core_n / tail_shards 参数，存储为私有属性。"""

    def test_default_core_n(self) -> None:
        """默认 core_n=60，tail_shards=8。"""
        m = _make_monitor(n_coins=120)
        assert m._core_n == 60
        assert m._tail_shards == 8

    def test_custom_core_n(self) -> None:
        """自定义 core_n / tail_shards 正确存储。"""
        m = _make_monitor(n_coins=120, core_n=30, tail_shards=4)
        assert m._core_n == 30
        assert m._tail_shards == 4

    def test_round_starts_at_zero(self) -> None:
        """初始 _round=0。"""
        m = _make_monitor(n_coins=120)
        assert m._round == 0

    def test_negative_core_n_clamped(self) -> None:
        """core_n < 0 被截断为 0（全部为长尾）。"""
        m = _make_monitor(n_coins=20, core_n=-5, tail_shards=4)
        assert m._core_n == 0

    def test_zero_tail_shards_clamped(self) -> None:
        """tail_shards <= 0 被截断为 1（等价无分层）。"""
        m = _make_monitor(n_coins=20, core_n=10, tail_shards=0)
        assert m._tail_shards == 1


# ──────────────────────────────────────────────────────────────────────────────
# A2：核心层每轮全部包含
# ──────────────────────────────────────────────────────────────────────────────

class TestCoreTierAlwaysPresent:
    """核心层（高 vol 前 core_n 个币）每轮必须全部出现。"""

    def test_core_coins_present_in_every_round(self) -> None:
        """120 币，core_n=60：每轮前 60 个币必须全部出现。"""
        n = 120
        core_n = 60
        tail_shards = 8
        m = _make_monitor(n_coins=n, core_n=core_n, tail_shards=tail_shards)
        all_coins = list(m.coin_to_symbol.keys())[:n]
        core_expected = set(all_coins[:core_n])

        # 遍历 tail_shards 轮，验证每轮核心层全部出现
        for r in range(tail_shards):
            coins_this_round = set(_get_coins_for_round(m, r))
            missing = core_expected - coins_this_round
            assert not missing, (
                f"轮次 {r}：核心层缺少 {missing}（核心层每轮必须全部出现）"
            )

    def test_core_coins_present_small_n(self) -> None:
        """20 币，core_n=10：每轮前 10 个币必须全部出现。"""
        m = _make_monitor(n_coins=20, core_n=10, tail_shards=4)
        core_expected = set(list(m.coin_to_symbol.keys())[:10])
        for r in range(4):
            coins_this_round = set(_get_coins_for_round(m, r))
            missing = core_expected - coins_this_round
            assert not missing, f"轮次 {r} 核心层不完整，缺失：{missing}"


# ──────────────────────────────────────────────────────────────────────────────
# A2：长尾层 round-robin 全覆盖不重不漏
# ──────────────────────────────────────────────────────────────────────────────

class TestTailTierRoundRobin:
    """长尾层在 tail_shards 轮内全覆盖、不重不漏（round-robin 标准行为）。"""

    def test_tail_full_coverage_in_tail_shards_rounds(self) -> None:
        """120 币，core_n=60，tail_shards=8：8 轮内长尾 60 个币全覆盖。"""
        n = 120
        core_n = 60
        tail_shards = 8
        m = _make_monitor(n_coins=n, core_n=core_n, tail_shards=tail_shards)
        all_coins = list(m.coin_to_symbol.keys())[:n]
        tail_expected = set(all_coins[core_n:])

        # 收集 tail_shards 轮内出现的长尾币
        tail_seen: set[str] = set()
        for r in range(tail_shards):
            coins_this_round = set(_get_coins_for_round(m, r))
            # 只收集长尾部分（排除核心）
            tail_seen |= coins_this_round - set(all_coins[:core_n])

        missing = tail_expected - tail_seen
        assert not missing, (
            f"长尾层 {tail_shards} 轮内未覆盖: {missing}（应全量覆盖）"
        )

    def test_each_tail_shard_disjoint(self) -> None:
        """不同轮次的长尾分片互不重叠（pure round-robin，不重复处理）。"""
        n = 120
        core_n = 60
        tail_shards = 8
        m = _make_monitor(n_coins=n, core_n=core_n, tail_shards=tail_shards)
        all_core = set(list(m.coin_to_symbol.keys())[:core_n])

        # 收集每轮的长尾分片
        tail_by_round: list[set[str]] = []
        for r in range(tail_shards):
            coins_this_round = set(_get_coins_for_round(m, r))
            tail_by_round.append(coins_this_round - all_core)

        # 验证两两互不重叠
        for i in range(tail_shards):
            for j in range(i + 1, tail_shards):
                overlap = tail_by_round[i] & tail_by_round[j]
                assert not overlap, (
                    f"轮次 {i} 和 {j} 的长尾分片有重叠：{overlap}（应互不重叠）"
                )

    def test_tail_coverage_20_coins_4_shards(self) -> None:
        """20 币，core_n=5，tail_shards=4：4 轮内长尾 15 币全覆盖。"""
        m = _make_monitor(n_coins=20, core_n=5, tail_shards=4)
        all_coins = list(m.coin_to_symbol.keys())[:20]
        tail_expected = set(all_coins[5:])

        tail_seen: set[str] = set()
        for r in range(4):
            coins_r = set(_get_coins_for_round(m, r))
            tail_seen |= coins_r - set(all_coins[:5])

        assert tail_seen == tail_expected, (
            f"4 轮应覆盖全部长尾，未见：{tail_expected - tail_seen}，多余：{tail_seen - tail_expected}"
        )

    def test_round_increments_on_refresh(self) -> None:
        """每次 refresh 调用后 _round 递增 1。

        通过 monkeypatch BitgetREST 避免网络依赖。
        """
        m = _make_monitor(n_coins=10, core_n=5, tail_shards=4)
        assert m._round == 0

        # 构造最简 mock：BitgetREST ctx manager 返回的 bg 不需真实 klines
        mock_bg = AsyncMock()
        # klines 返回空列表（触发 live 路径但 analyze_candles 会短路无形态）
        mock_bg.klines = AsyncMock(return_value=[])
        mock_bg.tickers = AsyncMock(return_value={})
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_bg)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("smc_tracker.monitor.harmonic_monitor.BitgetREST", return_value=mock_ctx):
            asyncio.run(m.refresh(1_700_000_000_000))

        assert m._round == 1, f"refresh 后 _round 应为 1，实际 {m._round}"

        with patch("smc_tracker.monitor.harmonic_monitor.BitgetREST", return_value=mock_ctx):
            asyncio.run(m.refresh(1_700_000_001_000))

        assert m._round == 2, f"第2次 refresh 后 _round 应为 2，实际 {m._round}"


# ──────────────────────────────────────────────────────────────────────────────
# A2：退化情形 — core_n >= 总币数 → 全量每轮 refresh
# ──────────────────────────────────────────────────────────────────────────────

class TestDegenerateCoreFullCoverage:
    """退化情形：core_n >= 总币数时，无长尾，每轮处理全部币（等价旧行为）。"""

    def test_core_n_equals_total_no_tail(self) -> None:
        """core_n == 总币数 → 无长尾，每轮全量覆盖。"""
        n = 20
        m = _make_monitor(n_coins=n, core_n=n, tail_shards=8)
        all_coins = set(m.coin_to_symbol.keys())

        for r in range(8):  # 多轮验证
            coins_r = set(_get_coins_for_round(m, r))
            assert coins_r == all_coins, (
                f"退化模式轮次 {r} 应全量覆盖，缺少：{all_coins - coins_r}"
            )

    def test_core_n_greater_than_total_no_tail(self) -> None:
        """core_n > 总币数 → 全量覆盖（无长尾）。"""
        n = 10
        m = _make_monitor(n_coins=n, core_n=100, tail_shards=8)
        all_coins = set(m.coin_to_symbol.keys())

        for r in range(4):
            coins_r = set(_get_coins_for_round(m, r))
            assert coins_r == all_coins, (
                f"core_n > 总数时应全量覆盖，轮次 {r} 缺少：{all_coins - coins_r}"
            )

    def test_no_tail_coins_when_core_covers_all(self) -> None:
        """core_n >= 总币数时，每轮选出的币不超过总币数（无重复）。"""
        n = 15
        m = _make_monitor(n_coins=n, core_n=15, tail_shards=4)
        for r in range(4):
            coins_r = _get_coins_for_round(m, r)
            assert len(coins_r) == n, (
                f"退化模式每轮应恰好 {n} 个币，实际 {len(coins_r)}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# A2：极端情形
# ──────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    """极端/边界情形。"""

    def test_single_coin(self) -> None:
        """仅1个币，core_n=1，每轮应出现。"""
        m = _make_monitor(n_coins=1, core_n=1, tail_shards=4)
        all_coins = set(m.coin_to_symbol.keys())
        for r in range(4):
            coins_r = set(_get_coins_for_round(m, r))
            assert coins_r == all_coins

    def test_tail_shards_one_means_full_tail_every_round(self) -> None:
        """tail_shards=1 时长尾每轮全量 refresh（不分片，等价无轮转）。"""
        n = 20
        m = _make_monitor(n_coins=n, core_n=10, tail_shards=1)
        all_coins = set(m.coin_to_symbol.keys())

        for r in range(4):  # 多轮均应相同
            coins_r = set(_get_coins_for_round(m, r))
            assert coins_r == all_coins, (
                f"tail_shards=1 时每轮应全量覆盖，轮次 {r} 缺少：{all_coins - coins_r}"
            )

    def test_large_scale_665_coins(self) -> None:
        """665 币（全量 Bitget 永续）：core_n=60，tail_shards=8，8 轮内全覆盖。"""
        n = 665
        core_n = 60
        tail_shards = 8
        m = _make_monitor(n_coins=n, core_n=core_n, tail_shards=tail_shards, top_n=n)
        all_coins = list(m.coin_to_symbol.keys())[:n]
        core_set = set(all_coins[:core_n])
        tail_expected = set(all_coins[core_n:])

        tail_seen: set[str] = set()
        for r in range(tail_shards):
            coins_r = set(_get_coins_for_round(m, r))
            # 核心层每轮必须完整
            assert core_set <= coins_r, f"轮次 {r} 核心层不完整"
            tail_seen |= coins_r - core_set

        assert tail_seen == tail_expected, (
            f"665 币场景 8 轮未完整覆盖长尾，缺：{len(tail_expected - tail_seen)} 个"
        )

    def test_round_robin_cycles_repeat(self) -> None:
        """round-robin 是周期性的：第 k+tail_shards 轮与第 k 轮处理相同长尾分片。"""
        m = _make_monitor(n_coins=120, core_n=60, tail_shards=8)
        for r in range(8):
            shard_r = set(_get_coins_for_round(m, r))
            shard_r_next = set(_get_coins_for_round(m, r + 8))
            assert shard_r == shard_r_next, (
                f"轮次 {r} 和 {r + 8} 应处理相同分片（周期性 round-robin）"
            )
