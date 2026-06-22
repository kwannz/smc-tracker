"""飞书(Lark)交互卡片推送单测：签名 + 卡片结构 + 配色 + build_notifier 接入（不联网）。"""
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
    assert len(s) == 44
    assert feishu_sign(ts, secret) == s


def test_feishu_card_payload_structure_and_sign():
    """交互卡片: msg_type=interactive + 彩色头部 + lark_md 完整正文; 有 secret 带 sign。"""
    from smc_tracker.notify.feishu import FeishuNotifier, card_color, card_title
    n = FeishuNotifier("https://open.feishu.cn/x", secret="sec")
    text = "🚨 跟庄信号 BTC 净做空 $3万\n  庄#3 ZEC 空头 $30,000"
    p = n._payload(text, card_title(text), card_color(text))
    assert p["msg_type"] == "interactive"
    card = p["card"]
    assert card["header"]["template"] == "red"               # 🚨 告警 → 红
    assert "跟庄信号" in card["header"]["title"]["content"]   # 首行作标题
    div = card["elements"][0]
    assert div["tag"] == "div" and div["text"]["tag"] == "lark_md"
    assert text in div["text"]["content"]                    # 完整正文不丢
    assert "timestamp" in p and "sign" in p and p["sign"]


def test_feishu_card_color_mapping():
    """配色按内容类型: 告警红/信号橙/挂单墙紫/摘要浅蓝。"""
    from smc_tracker.notify.feishu import card_color
    assert card_color("🌟超级信号 ETH") == "orange"
    assert card_color("📊 准确率回顾") == "wathet"
    assert card_color("🧱挂单墙 BTC bid墙") == "violet"
    assert card_color("💥 OKX 强平级联 多头被平") == "red"
    assert card_color("🐋 聪明钱净流向") == "turquoise"
    assert card_color("普通文本无标记") == "blue"


def test_feishu_card_without_secret_no_sign():
    """无 secret → 卡片仍构造, 但不带 sign(机器人未开签名校验场景)。"""
    from smc_tracker.notify.feishu import FeishuNotifier
    p = FeishuNotifier("https://open.feishu.cn/x", secret="")._payload("hi", "标题", "blue")
    assert p["msg_type"] == "interactive"
    assert "sign" not in p and "timestamp" not in p


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
