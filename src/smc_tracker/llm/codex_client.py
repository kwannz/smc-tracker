"""Codex CLI 后端：以子进程方式调用本机 `codex`(OAuth GPT-5.4)做一次性研判。

为何用 CLI 而非 HTTP：用户用 Codex 的 OAuth 登录(GPT-5.4)，无需独立 API KEY——
契合本项目「无 apikey」约束。本机已 `codex login` 即可用。

低延迟与稳健(对齐 CLAUDE.md)：
  · 异步子进程(asyncio)，绝不阻塞监控事件循环；
  · 硬超时 → 超时杀进程并返回 None；
  · 任何异常/非零退出/CLI 缺失 → 返回 None(优雅降级，不影响主流程)；
  · 命令模板完全可配(config.llm.command/model)，不同环境可调 flag 而无需改代码。

注意：在受限沙箱里 `codex` 启动会因网络/鉴权初始化而挂起——故默认 enabled=False，
由用户在已登录 Codex 的真实环境开启。argv 构造与子进程管道逻辑均有单测(用 cat/sleep 桩验证)。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("llm.codex")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")     # 去除终端颜色码，保证文本干净


@dataclass(slots=True)
class CodexClient:
    """非交互调用 `codex exec`：system+user 提示词 → 经 stdin 喂入 → 取最终回复。"""
    command: list[str] = field(
        default_factory=lambda: ["codex", "exec", "--skip-git-repo-check"])
    model: str = ""              # 形如 "gpt-5.4"；非空则追加 `-m <model>`
    timeout_sec: float = 90.0
    cwd: str | None = None

    def build_argv(self) -> list[str]:
        argv = list(self.command)
        if self.model:
            argv += ["-m", self.model]
        return argv

    @staticmethod
    def build_prompt(system: str, user: str) -> str:
        """合并 system+user：Codex 一次性模式无独立 system 通道，用清晰分节传递。"""
        return f"# 系统指令(角色与方法论)\n{system}\n\n# 任务数据\n{user}"

    async def complete(self, system: str, user: str) -> str | None:
        """运行子进程，stdin 喂提示词，返回 stdout(最终回复)；失败/超时返回 None。"""
        argv = self.build_argv()
        prompt = self.build_prompt(system, user)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd)
        except (FileNotFoundError, OSError) as e:        # CLI 不存在等
            log.warning("Codex 启动失败(%s)：%s", " ".join(argv[:2]), e)
            return None
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=self.timeout_sec)
        except asyncio.TimeoutError:
            log.warning("Codex 超时 %.0fs，已终止", self.timeout_sec)
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return None
        except Exception as e:                            # noqa: BLE001
            log.warning("Codex 通信异常：%s", e)
            return None
        if proc.returncode != 0:
            log.warning("Codex 非零退出 %s：%s", proc.returncode,
                        (err or b"").decode("utf-8", "replace")[:200])
            return None
        text = _ANSI.sub("", (out or b"").decode("utf-8", "replace")).strip()
        return text or None
