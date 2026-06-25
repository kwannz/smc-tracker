"""输出层：Webhook + Telegram + 飞书 推送 + 摘要日报 + HL 分类汇总。"""
from .webhook import WebhookNotifier
from .telegram import TelegramNotifier
from .feishu import FeishuNotifier
from .multi import MultiNotifier
from .report import build_report, build_all_signals_report
from .chunk import split_message
from .digest import HLDigest


def build_notifier(cfg: object) -> MultiNotifier:
    """按 config 组装多渠道推送器（webhook + Telegram + 飞书，自动跳过未配置的）。"""
    out = getattr(cfg, "output", None)
    tg = getattr(cfg, "telegram", None)
    fs = getattr(cfg, "feishu", None)
    return MultiNotifier([
        WebhookNotifier(getattr(out, "webhook_url", "") if out else ""),
        TelegramNotifier(getattr(tg, "bot_token", "") if tg else "",
                         getattr(tg, "chat_id", "") if tg else ""),
        FeishuNotifier(getattr(fs, "webhook_url", "") if fs else "",
                       getattr(fs, "secret", "") if fs else ""),
    ])


__all__ = ["WebhookNotifier", "TelegramNotifier", "FeishuNotifier", "MultiNotifier",
           "build_notifier", "build_report", "build_all_signals_report",
           "split_message", "HLDigest"]
