"""消息分段：把长文本按渠道单条上限切成多条，保证完整推送（不截断）。

Telegram 单条上限 4096、Discord 2000、Slack 较宽。优先按行边界切，单行超长才硬切，
让每条推送都是完整可读的片段（用户要求「输出到 tg 需要完整」→ 分段全发而非截断）。
"""
from __future__ import annotations


def split_message(text: str, limit: int) -> list[str]:
    """把 text 切成每段 ≤ limit 的列表（按 \\n 边界，单行超长则硬切）。空串→空列表。"""
    if limit <= 0:
        return [text] if text else []
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        # 单行本身超长：先冲掉 cur，再把长行硬切成多段
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = (cur + "\n" + line) if cur else line
        if len(candidate) > limit:
            if cur:
                chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks
