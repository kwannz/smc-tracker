"""配置热加载单测：diff_config 纯函数 + _apply_config 运行时应用（确定性，无网络）。"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import (
    Config, DetectionCfg, LLMCfg, OutputCfg, TelegramCfg, diff_config,
)


# ---------------------------------------------------------------------------
# diff_config 纯函数测试
# ---------------------------------------------------------------------------

def test_diff_config_no_change():
    """两个相同配置 → 空列表。"""
    cfg = Config()
    assert diff_config(cfg, Config()) == []


def test_diff_config_detection_threshold():
    """修改 large_fill_notional_usd → 包含对应变更条目。"""
    old = Config()
    new = Config()
    new.detection.large_fill_notional_usd = 99_999.0
    changes = diff_config(old, new)
    assert len(changes) == 1
    assert "large_fill_notional_usd" in changes[0]
    assert "99999" in changes[0]


def test_diff_config_require_sweep_toggle():
    """开启 require_sweep 门槛 → 检测到布尔变更。"""
    old = Config()
    new = Config()
    new.detection.require_sweep = True
    changes = diff_config(old, new)
    assert any("require_sweep" in c for c in changes)


def test_diff_config_output_console():
    """关闭 output.console → 检测到变更。"""
    old = Config()
    new = Config()
    new.output.console = False
    changes = diff_config(old, new)
    assert any("output.console" in c for c in changes)


def test_diff_config_webhook_url():
    """修改 webhook_url → 检测到变更。"""
    old = Config()
    new = Config()
    new.output.webhook_url = "https://example.com/hook"
    changes = diff_config(old, new)
    assert any("output.webhook_url" in c for c in changes)


def test_diff_config_telegram():
    """同时修改 bot_token + chat_id → 返回两条变更。"""
    old = Config()
    new = Config()
    new.telegram.bot_token = "newtoken"
    new.telegram.chat_id = "12345"
    changes = diff_config(old, new)
    keys = [c.split(":")[0] for c in changes]
    assert "telegram.bot_token" in keys
    assert "telegram.chat_id" in keys


def test_diff_config_llm_enabled():
    """开启 llm.enabled → 检测到变更。"""
    old = Config()
    new = Config()
    new.llm.enabled = True
    changes = diff_config(old, new)
    assert any("llm.enabled" in c for c in changes)


def test_diff_config_multiple_fields():
    """同时修改多个字段 → 每个都有对应条目。"""
    old = Config()
    new = Config()
    new.detection.large_fill_notional_usd = 80_000.0
    new.detection.require_sweep = True
    new.llm.enabled = True
    new.llm.model = "gpt-5.4"
    changes = diff_config(old, new)
    changed_keys = [c.split(":")[0] for c in changes]
    assert "detection.large_fill_notional_usd" in changed_keys
    assert "detection.require_sweep" in changed_keys
    assert "llm.enabled" in changed_keys
    assert "llm.model" in changed_keys


def test_diff_config_position_change_pct():
    """修改 position_change_pct → 检测到变更。"""
    old = Config()
    new = Config()
    new.detection.position_change_pct = 0.25
    changes = diff_config(old, new)
    assert any("position_change_pct" in c for c in changes)


def test_diff_config_llm_interval():
    """修改 llm.interval_sec → 检测到变更。"""
    old = Config()
    new = Config()
    new.llm.interval_sec = 1800.0
    changes = diff_config(old, new)
    assert any("llm.interval_sec" in c for c in changes)


# ---------------------------------------------------------------------------
# _apply_config 应用测试（最小 mock app）
# ---------------------------------------------------------------------------

def _make_mock_app(cfg: Config) -> MagicMock:
    """构造最小 mock TradingSystem（只有 _apply_config 需要的属性）。"""
    app = MagicMock()
    app.cfg = cfg
    # address_monitor / meme_monitor
    app.address_monitor = MagicMock()
    app.address_monitor.large_fill_notional_usd = cfg.detection.large_fill_notional_usd
    app.meme_monitor = MagicMock()
    app.meme_monitor.large_notional_usd = cfg.detection.large_fill_notional_usd
    app.meme_monitor.suspicious_notional = cfg.detection.large_fill_notional_usd * 2
    # signal_engine
    app.signal_engine = MagicMock()
    app.signal_engine.require_sweep = cfg.detection.require_sweep
    # notifier / analyst — mock 工厂
    app.notifier = MagicMock()
    app.analyst = MagicMock()
    return app


def _apply_config(app: MagicMock, new_cfg: Config) -> list[str]:
    """从 TradingSystem._apply_config 提取出的纯逻辑，便于单独测试。"""
    from smc_tracker.config import diff_config
    from smc_tracker.notify import build_notifier
    from smc_tracker.llm import build_analyst

    changes = diff_config(app.cfg, new_cfg)
    if not changes:
        return []

    det = new_cfg.detection
    app.cfg.detection.large_fill_notional_usd = det.large_fill_notional_usd
    app.address_monitor.large_fill_notional_usd = det.large_fill_notional_usd
    app.meme_monitor.large_notional_usd = det.large_fill_notional_usd
    app.meme_monitor.suspicious_notional = det.large_fill_notional_usd * 2
    app.signal_engine.require_sweep = det.require_sweep
    app.cfg.detection.require_sweep = det.require_sweep
    app.cfg.detection.position_change_pct = det.position_change_pct
    app.cfg.output.console = new_cfg.output.console

    old_webhook = app.cfg.output.webhook_url
    old_tg_token = app.cfg.telegram.bot_token
    old_tg_chat = app.cfg.telegram.chat_id
    if (new_cfg.output.webhook_url != old_webhook
            or new_cfg.telegram.bot_token != old_tg_token
            or new_cfg.telegram.chat_id != old_tg_chat):
        app.cfg.output.webhook_url = new_cfg.output.webhook_url
        app.cfg.telegram.bot_token = new_cfg.telegram.bot_token
        app.cfg.telegram.chat_id = new_cfg.telegram.chat_id
        app.notifier = build_notifier(new_cfg)

    old_llm_enabled = app.cfg.llm.enabled
    old_llm_model = app.cfg.llm.model
    old_llm_interval = app.cfg.llm.interval_sec
    if (new_cfg.llm.enabled != old_llm_enabled
            or new_cfg.llm.model != old_llm_model
            or new_cfg.llm.interval_sec != old_llm_interval):
        app.cfg.llm.enabled = new_cfg.llm.enabled
        app.cfg.llm.model = new_cfg.llm.model
        app.cfg.llm.interval_sec = new_cfg.llm.interval_sec
        app.analyst = build_analyst(new_cfg)

    app.cfg = new_cfg
    return changes


def test_apply_config_updates_threshold():
    """_apply_config 把新阈值写入 address_monitor + meme_monitor。"""
    old_cfg = Config()
    app = _make_mock_app(old_cfg)

    new_cfg = Config()
    new_cfg.detection.large_fill_notional_usd = 75_000.0

    changes = _apply_config(app, new_cfg)
    assert changes
    assert app.address_monitor.large_fill_notional_usd == 75_000.0
    assert app.meme_monitor.large_notional_usd == 75_000.0
    assert app.meme_monitor.suspicious_notional == 150_000.0


def test_apply_config_updates_require_sweep():
    """_apply_config 把 require_sweep 写入 signal_engine。"""
    old_cfg = Config()
    app = _make_mock_app(old_cfg)

    new_cfg = Config()
    new_cfg.detection.require_sweep = True

    _apply_config(app, new_cfg)
    assert app.signal_engine.require_sweep is True


def test_apply_config_no_change_returns_empty():
    """相同配置 → 无变更，不修改运行时对象。"""
    cfg = Config()
    app = _make_mock_app(cfg)
    original_threshold = cfg.detection.large_fill_notional_usd

    changes = _apply_config(app, Config())
    assert changes == []
    assert app.address_monitor.large_fill_notional_usd == original_threshold


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("✅ 全部通过")
