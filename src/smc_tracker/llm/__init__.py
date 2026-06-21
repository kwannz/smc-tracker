"""LLM 研判层：实时双所态势 → 系统/用户提示词 → Codex(OAuth GPT-5.4) → 前瞻抓庄研判。

无 apikey：复用本机 Codex 的 OAuth 登录。默认关闭(config.llm.enabled=false)，
在已 `codex login` 的环境开启即用；失败优雅降级，绝不阻塞监控热路径。
"""
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .codex_client import CodexClient
from .analyst import MarketAnalyst, build_analyst

__all__ = ["SYSTEM_PROMPT", "build_user_prompt", "CodexClient",
           "MarketAnalyst", "build_analyst"]
