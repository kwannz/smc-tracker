"""supervisor — 周期任务无限重启监督器。

A3：顶层 gather 无 return_exceptions/无 supervisor → 任一 _periodic_* 抛非 CancelledError
    异常导致 gather 取消其余全部。此模块提供 supervise() 兜住每个任务，让其指数退避重启。

用法（app.py gather 中）：
    supervise(lambda: self._periodic_xxx(), name="periodic_xxx", log=log)
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Awaitable, Callable


def _calc_backoff(error_count: int, base_backoff: float, max_backoff: float) -> float:
    """计算第 error_count 次（从 0 起）错误后的退避秒数（指数，封顶 max_backoff）。

    error_count=0 → base_backoff；1 → 2×base；n → min(2^n * base, max_backoff)。
    **夹住指数(非仅夹结果)**：任务永久失败时 error_count 无限增长，原 `2**error_count`
    会计算千位大整数(每轮浪费 CPU)，而结果早已封顶 max_backoff。指数封顶 32
    （base×2^32 ≈ 4.3e9 远超任何合理 max_backoff，必被 min 夹住），行为完全不变。
    """
    exp = error_count if error_count < 32 else 32
    return min(base_backoff * (2 ** exp), max_backoff)


async def supervise(
    factory: Callable[[], Awaitable[None]],
    *,
    name: str,
    base_backoff: float = 1.0,
    max_backoff: float = 60.0,
    reset_after: float = 30.0,
    log: logging.Logger,
) -> None:
    """无限重启监督：调用 factory() 得到 coro 并 await；

    - 正常返回：记 info 后退避 base_backoff 重启（periodic 任务本应永不返回，返回视为异常退出）。
    - 抛非 CancelledError 异常：log.exception + 指数退避后重启（不连累其余任务）。
    - CancelledError：向上抛（响应 stop()，不吞）。
    - 成功运行 ≥ reset_after 秒后退避复位为 base_backoff（避免崩溃循环放大退避）。
    """
    error_count = 0
    while True:
        start = asyncio.get_event_loop().time()
        try:
            await factory()
            # 正常返回（periodic 任务本应永不结束）
            elapsed = asyncio.get_event_loop().time() - start
            # 立即正常返回（elapsed < base_backoff）= no-op/禁用任务主动结束
            # （如 LLM disabled / ticker_board 某条件直接 return）。此时按 base_backoff
            # 反复重启会造成 busy-loop（实跑暴露：ticker_board/llm 每 1s 刷屏空转 CPU）。
            # 任务主动结束 = 它选择不运行 → 停止监督，不再重启。
            if elapsed < base_backoff:
                log.info("supervisor[%s] 任务立即正常返回（elapsed=%.2fs，疑似 no-op/禁用），停止监督",
                         name, elapsed)
                return
            if elapsed >= reset_after:
                error_count = 0
            log.info("supervisor[%s] 任务正常返回（elapsed=%.1fs），%.1fs 后重启",
                     name, elapsed, base_backoff)
            await asyncio.sleep(base_backoff)
        except asyncio.CancelledError:
            # 响应 stop()：向上传播，不吞
            log.debug("supervisor[%s] 收到 CancelledError，向上传播", name)
            raise
        except Exception:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed >= reset_after:
                error_count = 0     # 成功运行够久，重置退避计数
            backoff = _calc_backoff(error_count, base_backoff, max_backoff)
            log.exception("supervisor[%s] 任务异常（elapsed=%.1fs），%.1fs 后重启",
                          name, elapsed, backoff)
            error_count += 1
            await asyncio.sleep(backoff)
