"""仪表盘共享数据层 helper（纯叶子模块，被 dashboard / dashboard_harmonic 复用）。

抽出以打破 dashboard ↔ dashboard_harmonic 循环导入：两边都只依赖本模块。
"""
from __future__ import annotations

from typing import Any


def _safe_rows(conn: Any, sql: str, params: tuple = ()) -> list[tuple]:
    """防御性 SQL 查询：表不存在/列缺失时返回 []，不抛。"""
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001
        return []


def _row_to_dict(row: tuple, keys: list[str]) -> dict[str, Any]:
    """tuple 行 → dict，按 keys 映射，缺失字段填 None。"""
    return {k: (row[i] if i < len(row) else None) for i, k in enumerate(keys)}
