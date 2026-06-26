"""dashboard_vol 波动面板纯逻辑单测（tmp db + 合成 K 线，无 HTTP）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.dashboard_vol import volatility_state, pick_coins, render_volatility_page
from smc_tracker.storage import Store


def _store():
    return Store(Path(tempfile.mkdtemp()) / "t.db")


def _seed(s, coin, tf, fn, n=60):
    rows = [(coin, tf, i * 900_000, fn(i), fn(i) * 1.002, fn(i) * 0.998, fn(i), 1.0)
            for i in range(n)]
    s.upsert_candles(rows)


def test_volatility_state_structure():
    s = _store()
    _seed(s, "BTC", "15m", lambda i: 100.0 + i)
    st = volatility_state(s, {"BTC": "BTCUSDT"}, ["15m"], now_ms=0)
    assert st["tfs"] == ["15m"]
    assert st["coins"][0]["coin"] == "BTC"
    assert "by_tf" in st["coins"][0] and "15m" in st["coins"][0]["by_tf"]
    assert "velocity" in st["coins"][0]["by_tf"]["15m"]
    # #183 数据契约:state 含 GARCH 前瞻量(供 dashboard 渲染 σ→GA 预测,与 CLI/#179 一致)
    assert "garch_vol" in st["coins"][0]["by_tf"]["15m"]


def test_volatility_page_surfaces_garch_forecast():
    """#183:dashboard 波动页呈现 GARCH 一步预测(σ→GA),与 CLI vol 板/#179 升级一致(非只显回望速度)。"""
    html = render_volatility_page()
    assert "GA" in html and "garch_vol" in html       # cell 渲染读 m.garch_vol → GA
    assert "主前瞻量" in html                          # 表头标注 GARCH 为主前瞻量


def test_pick_coins_prefers_monitored():
    s = _store()
    s.add_monitored_coins([("ETH", "ETHUSDT", 1, "")])
    _seed(s, "BTC", "15m", lambda i: 100.0 + i)  # DB 有 BTC 但清单是 ETH
    assert pick_coins(s) == {"ETH": "ETHUSDT"}


def test_pick_coins_fallback_to_db():
    s = _store()
    _seed(s, "BTC", "15m", lambda i: 100.0 + i)
    assert pick_coins(s) == {"BTC": "BTCUSDT"}  # 清单空 + 数据非近端 → DISTINCT 降级仍含 BTC


def test_pick_coins_fallback_ranks_by_recent_range():
    """清单空 + 近端数据 → 按近 24h 振幅降序：剧烈波动币优先于安静币(波动板该突出在动的)。"""
    import time
    s = _store()
    base = int(time.time() * 1000) - 40 * 900_000   # 近期起点(40 根 15m ≈ 10h 内)
    calm = [("CALM", "15m", base + i * 900_000, 100.0, 100.1, 99.9, 100.0, 1.0)
            for i in range(40)]                      # 振幅 ~0.2%
    wild = [("WILD", "15m", base + i * 900_000, 100.0, 100.0 + i * 2, 100.0 - i * 2, 100.0, 1.0)
            for i in range(40)]                      # 振幅巨大(i=39 → h178/l22)
    s.upsert_candles(calm + wild)
    got = pick_coins(s)
    assert "WILD" in got and "CALM" in got
    assert list(got.keys())[0] == "WILD"             # 最剧烈者排首(query DESC + dict 保序)


def test_pick_coins_single_source_shared_with_cli():
    """dashboard 与 CLI 必须共用同一 pick_coins(消除两前端选币分叉，#141)：身份相同。"""
    from smc_tracker.dashboard_vol import pick_coins as dash_pick
    from smc_tracker.monitor.volatility_monitor import pick_coins as core_pick
    from smc_tracker.cli_commands import _cmd_vol  # CLI 内部惰性 import 同一函数
    import inspect
    assert dash_pick is core_pick                       # dashboard 经再导出指向同一对象
    src = inspect.getsource(_cmd_vol)
    assert "pick_coins(store)" in src                   # CLI 走共用选币而非裸 get_monitored_coins


def test_render_page_self_contained():
    html = render_volatility_page()
    assert "/api/volatility" in html
    assert "http://" not in html and "https://" not in html  # 无外链
