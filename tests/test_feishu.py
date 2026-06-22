"""飞书(Lark)推送单测：签名算法 + payload 结构 + build_notifier 接入（不联网）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_feishu_sign_matches_algorithm():
    """feishu_sign = base64(HMAC-SHA256(key=f'{ts}\\n{secret}', msg=b''))，确定性。"""
    from smc_tracker.notify.feishu import feishu_sign
    ts, secret = 1_700_000_000, "ZJyfYF2jzpPxg8WGZm2vt"
    s = feishu_sign(ts, secret)
    expect = base64.b64encode(
        hmac.new(f"{ts}\n{secret}".encode("utf-8"), b"", hashlib.sha256).digest()
    ).decode("utf-8")
    assert s == expect                 # 与飞书官方算法一致(已对真实 webhook 验证成功)
    assert len(s) == 44                # sha256(32B) → base64 44 字符
    assert feishu_sign(ts, secret) == s  # 确定性


def test_feishu_payload_with_secret_has_sign():
    """有 secret → payload 带 timestamp+sign；text 在 content.text。"""
    from smc_tracker.notify.feishu import FeishuNotifier
    p = FeishuNotifier("https://open.feishu.cn/x", secret="sec")._payload("hello")
    assert p["msg_type"] == "text"
    assert p["content"]["text"] == "hello"
    assert "timestamp" in p and "sign" in p and p["sign"]


def test_feishu_payload_without_secret_no_sign():
    """无 secret → 不带 sign(机器人未开签名校验场景)。"""
    from smc_tracker.notify.feishu import FeishuNotifier
    p = FeishuNotifier("https://open.feishu.cn/x", secret="")._payload("hi")
    assert "sign" not in p and "timestamp" not in p
    assert p["content"]["text"] == "hi"


def test_feishu_enabled_gating():
    from smc_tracker.notify.feishu import FeishuNotifier
    assert FeishuNotifier("https://open.feishu.cn/x").enabled is True
    assert FeishuNotifier("").enabled is False


def test_build_notifier_includes_feishu():
    """config.feishu.webhook_url 配置 → build_notifier 含 FeishuNotifier 渠道。"""
    from smc_tracker.config import Config, FeishuCfg
    from smc_tracker.notify import build_notifier
    cfg = Config()
    cfg.feishu = FeishuCfg(webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
                           secret="s")
    notifier = build_notifier(cfg)
    assert any(type(s).__name__ == "FeishuNotifier" for s in notifier.senders)
    assert notifier.channels >= 1
