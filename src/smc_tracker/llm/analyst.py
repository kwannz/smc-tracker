"""市场研判编排：实时态势摘要 → 系统/用户提示词 → LLM 后端 → 前瞻研判文本。

后端可插拔(默认 CodexClient/GPT-5.4，单测注入桩)。analyze() 永不抛异常、永不阻塞主流程，
失败返回 None；上层据此决定是否推送。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .prompts import SYSTEM_PROMPT, build_user_prompt

log = logging.getLogger("llm.analyst")

# 后端契约：async (system, user) -> str | None
Backend = Callable[[str, str], Awaitable[str | None]]


class MarketAnalyst:
    def __init__(self, backend: Backend, *, enabled: bool = True,
                 max_input_chars: int = 6000) -> None:
        self.backend = backend
        self.enabled = enabled
        self.max_input_chars = max_input_chars
        self.calls = 0
        self.ok = 0

    async def analyze(self, report_text: str, *, extra: str = "") -> str | None:
        """对态势摘要做一次抓庄研判。禁用/数据空/后端失败 → None。"""
        if not self.enabled or not (report_text or extra).strip():
            return None
        user = build_user_prompt(report_text, extra=extra, max_chars=self.max_input_chars)
        self.calls += 1
        try:
            out = await self.backend(SYSTEM_PROMPT, user)
        except Exception as e:                            # noqa: BLE001 — 研判失败不影响监控
            log.warning("研判后端异常：%s", e)
            return None
        if out:
            self.ok += 1
        return out


def build_analyst(cfg: Any) -> MarketAnalyst | None:
    """按 config.llm 组装研判器；未配置/禁用返回 None(系统照常运行)。"""
    llm = getattr(cfg, "llm", None)
    if llm is None or not getattr(llm, "enabled", False):
        return None
    from .codex_client import CodexClient
    client = CodexClient(
        command=list(getattr(llm, "command", None)
                     or ["codex", "exec", "--skip-git-repo-check"]),
        model=getattr(llm, "model", "") or "",
        timeout_sec=float(getattr(llm, "timeout_sec", 90.0)),
    )
    return MarketAnalyst(client.complete, enabled=True,
                         max_input_chars=int(getattr(llm, "max_input_chars", 6000)))
