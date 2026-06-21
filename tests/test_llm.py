"""LLM 研判层单测：提示词构建 + Codex 子进程管道(cat/sleep 桩) + 优雅降级 + 编排。

不调用真实 codex(沙箱会挂起)；用 cat/sleep/false 等系统命令验证子进程契约，
prompt/降级/编排逻辑全覆盖。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from smc_tracker.config import Config, LLMCfg
from smc_tracker.llm import (CodexClient, MarketAnalyst, SYSTEM_PROMPT,
                             build_analyst, build_user_prompt)


# ---- 提示词 ----
def test_system_prompt_is_forward_looking():
    assert "抓庄" in SYSTEM_PROMPT and "未来" in SYSTEM_PROMPT
    assert "非投资建议" in SYSTEM_PROMPT          # 合规结尾约定
    assert "数据不足" in SYSTEM_PROMPT             # 诚实/数据质量约束


def test_user_prompt_embeds_report():
    up = build_user_prompt("共振信号 3 条 BTC 做多")
    assert "共振信号 3 条 BTC 做多" in up
    assert "抓庄研判" in up


def test_user_prompt_truncates_keeping_tail():
    long = "旧" * 100 + "最新尾部标记"
    up = build_user_prompt(long, max_chars=20)
    assert "最新尾部标记" in up                    # 截断保留最新(尾部)
    assert "已截断" in up


def test_user_prompt_extra_appended():
    up = build_user_prompt("基础", extra="前瞻：BTC 资金流加速")
    assert "基础" in up and "前瞻：BTC 资金流加速" in up


# ---- CodexClient argv ----
def test_argv_without_model():
    c = CodexClient(command=["codex", "exec"])
    assert c.build_argv() == ["codex", "exec"]


def test_argv_appends_model():
    c = CodexClient(command=["codex", "exec"], model="gpt-5.4")
    assert c.build_argv() == ["codex", "exec", "-m", "gpt-5.4"]


def test_build_prompt_has_sections():
    p = CodexClient.build_prompt("SYS", "USR")
    assert "系统指令" in p and "SYS" in p and "任务数据" in p and "USR" in p


# ---- CodexClient 子进程管道(用 cat 桩：回显 stdin) ----
def test_complete_pipes_stdin_to_stdout():
    c = CodexClient(command=["cat"])               # cat 回显合并后的 prompt
    out = asyncio.run(c.complete("角色指令", "数据正文"))
    assert out is not None
    assert "角色指令" in out and "数据正文" in out


def test_complete_missing_binary_returns_none():
    c = CodexClient(command=["__definitely_no_such_cmd__"])
    assert asyncio.run(c.complete("s", "u")) is None     # 优雅降级，不抛


def test_complete_nonzero_exit_returns_none():
    c = CodexClient(command=["false"])             # 退出码 1
    assert asyncio.run(c.complete("s", "u")) is None


def test_complete_timeout_returns_none():
    c = CodexClient(command=["sleep", "5"], timeout_sec=0.3)
    assert asyncio.run(c.complete("s", "u")) is None     # 超时杀进程


def test_complete_empty_output_returns_none():
    c = CodexClient(command=["true"])              # 退出 0 但无 stdout
    assert asyncio.run(c.complete("s", "u")) is None


# ---- MarketAnalyst 编排 ----
def test_analyst_disabled_returns_none():
    async def backend(s, u):
        raise AssertionError("禁用时不应调用后端")
    a = MarketAnalyst(backend, enabled=False)
    assert asyncio.run(a.analyze("有数据")) is None


def test_analyst_empty_report_returns_none():
    async def backend(s, u):
        raise AssertionError("空数据不应调用后端")
    a = MarketAnalyst(backend, enabled=True)
    assert asyncio.run(a.analyze("   ")) is None


def test_analyst_returns_backend_output_and_counts():
    async def backend(system, user):
        assert system == SYSTEM_PROMPT
        assert "态势数据X" in user
        return "【方向研判】偏多 65%"
    a = MarketAnalyst(backend, enabled=True)
    out = asyncio.run(a.analyze("态势数据X"))
    assert out == "【方向研判】偏多 65%"
    assert a.calls == 1 and a.ok == 1


def test_analyst_backend_exception_graceful():
    async def backend(s, u):
        raise RuntimeError("后端炸了")
    a = MarketAnalyst(backend, enabled=True)
    assert asyncio.run(a.analyze("数据")) is None        # 不抛，降级为 None
    assert a.calls == 1 and a.ok == 0


# ---- build_analyst 工厂 ----
def test_build_analyst_disabled_by_default():
    assert build_analyst(Config()) is None               # 默认 enabled=False


def test_build_analyst_enabled():
    cfg = Config(llm=LLMCfg(enabled=True, model="gpt-5.4", timeout_sec=12.0,
                            max_input_chars=1234))
    a = build_analyst(cfg)
    assert a is not None and a.enabled and a.max_input_chars == 1234


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"  ✓ {name}")
    print("✅ 全部通过")
