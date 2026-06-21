"""输出层：Webhook + Telegram 推送 + 摘要日报。"""
from .webhook import WebhookNotifier
from .telegram import TelegramNotifier
from .multi import MultiNotifier
from .report import build_report
from .chunk import split_message


def build_notifier(cfg: object) -> MultiNotifier:
    """按 config 组装多渠道推送器（webhook + Telegram，自动跳过未配置的）。"""
    out = getattr(cfg, "output", None)
    tg = getattr(cfg, "telegram", None)
    return MultiNotifier([
        WebhookNotifier(getattr(out, "webhook_url", "") if out else ""),
        TelegramNotifier(getattr(tg, "bot_token", "") if tg else "",
                         getattr(tg, "chat_id", "") if tg else ""),
    ])


__all__ = ["WebhookNotifier", "TelegramNotifier", "MultiNotifier",
           "build_notifier", "build_report", "split_message"]
