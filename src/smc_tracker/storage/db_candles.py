"""K 线读写 mixin（从 Store 拆出，降 db.py 行数；CLAUDE.md 模块化扁平 + ≤800）。

CandleStoreMixin 混入 Store：所有方法仅依赖 `self.conn`（由 Store.__init__ 提供），
零其它 Store 方法耦合，故可独立 mixin。bitget_candles 表的全部读写都在这里。
"""
from __future__ import annotations

from typing import Any, Iterable


class CandleStoreMixin:
    """bitget_candles 读写方法集（混入 Store，依赖继承来的 self.conn）。"""

    conn: Any  # 由 Store.__init__ 提供（类型标注，供 mixin 方法引用）

    def upsert_candles(self, rows: Iterable[tuple]) -> None:
        """批量写入 K 线，同 (coin, tf, open_ms) 覆盖旧值（去重）。

        rows: Iterable[(coin, tf, open_ms, o, h, l, c, v)]
        空 rows 安全返回（executemany 处理 0 行，不 commit 无事务）。
        """
        rows_list = list(rows)
        if not rows_list:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO bitget_candles(coin,tf,open_ms,o,h,l,c,v) "
            "VALUES(?,?,?,?,?,?,?,?)",
            rows_list,
        )

    def prune_candles_to(self, max_bars: int = 3000) -> int:
        """每 (coin,tf) 滚动保留最新 max_bars 根 K 线，删更旧，返回删除行数。

        历史 + 实时统一上限：超 max_bars 的旧 bar 删除（防 bitget_candles 无界增长）。
        用窗口函数 ROW_NUMBER 按 open_ms 降序分区排名（sqlite ≥3.25），rn>max_bars 即删。
        max_bars<=0 视为不限制（返回 0）。
        """
        if max_bars <= 0:
            return 0
        try:
            self.conn.execute("BEGIN")
            cur = self.conn.execute(
                "DELETE FROM bitget_candles WHERE rowid IN ("
                "  SELECT rowid FROM ("
                "    SELECT rowid, ROW_NUMBER() OVER "
                "      (PARTITION BY coin, tf ORDER BY open_ms DESC) AS rn"
                "    FROM bitget_candles"
                "  ) WHERE rn > ?"
                ")",
                (max_bars,),
            )
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            self.conn.execute("COMMIT")
            return n
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def get_candles(self, coin: str, tf: str, limit: int = 1000,
                    since_ms: int | None = None) -> list:
        """读取 K 线，升序返回 list[Candle]。

        两种模式（只增参数，旧调用零改动）：
        - since_ms=None（默认）：取最近 limit 根，升序；与原行为完全一致。
        - since_ms=<整数>：返回 open_ms > since_ms 的所有 K 线（严格大于），
          不受 limit 截断，升序——用于增量游标场景（candle_ingest/HarmonicState 喂新 bar）。

        tf 不在 GRANULARITY_MS 中时 close_time_ms 偏移量为 0（兜底，不抛）。
        空结果返回 []。
        """
        from ..models import Candle
        from ..bitget.rest import GRANULARITY_MS

        gran_ms = GRANULARITY_MS.get(tf, 0)

        if since_ms is None:
            # 原行为：DESC 取最新 limit 根，再反转升序
            raw = self.conn.execute(
                "SELECT open_ms,o,h,l,c,v FROM bitget_candles "
                "WHERE coin=? AND tf=? ORDER BY open_ms DESC LIMIT ?",
                (coin, tf, limit),
            ).fetchall()
            if not raw:
                return []
            raw_asc = list(reversed(raw))
        else:
            # 游标模式：open_ms 严格大于 since_ms，全量返回不截断
            raw_asc = self.conn.execute(
                "SELECT open_ms,o,h,l,c,v FROM bitget_candles "
                "WHERE coin=? AND tf=? AND open_ms>? ORDER BY open_ms ASC",
                (coin, tf, since_ms),
            ).fetchall()
            if not raw_asc:
                return []

        return [
            Candle(
                coin=coin,
                interval=tf,
                open_time_ms=row[0],
                close_time_ms=row[0] + gran_ms,
                o=row[1],
                h=row[2],
                l=row[3],
                c=row[4],
                v=row[5],
                n=0,
            )
            for row in raw_asc
        ]

    def candles_for_draw(self, coin: str, tf: str, limit: int = 300
                         ) -> list[tuple[int, float, float, float, float]]:
        """读取最近 limit 根 K 线用于 SVG 绘制，升序返回轻量元组列表。

        返回：list of (open_ms, o, h, l, c)，按 open_ms 升序。
        与 get_candles 相同的数据窗口（最近 limit 根），但不构造 Candle 对象，
        避免 models/bitget 模块依赖，供 dashboard SVG 渲染直接消费（只读，零副作用）。

        coin 或 tf 无数据时返回 []，不抛。
        """
        raw = self.conn.execute(
            "SELECT open_ms,o,h,l,c FROM bitget_candles "
            "WHERE coin=? AND tf=? ORDER BY open_ms DESC LIMIT ?",
            (coin, tf, limit),
        ).fetchall()
        if not raw:
            return []
        # DB 取最新 N 根后反转成升序
        return list(reversed(raw))

    def count_candles(self, coin: str, tf: str) -> int:
        """返回指定 coin/tf 的 K 线行数。"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM bitget_candles WHERE coin=? AND tf=?",
            (coin, tf),
        ).fetchone()
        return row[0] if row else 0

    def latest_candle_ms(self, coin: str, tf: str) -> int | None:
        """返回指定 coin/tf 最新 K 线的 open_ms；无数据时返回 None。

        供 candle_ingest.detect_and_fill_gap 判断是否需要回填。
        利用 ix_bitget_candles_coin_tf_ms 索引（O(log N) 查询）。
        """
        row = self.conn.execute(
            "SELECT MAX(open_ms) FROM bitget_candles WHERE coin=? AND tf=?",
            (coin, tf),
        ).fetchone()
        # fetchone 永不返回 None（COUNT/MAX 始终有行），但 MAX 在空表时值为 NULL
        return row[0] if row and row[0] is not None else None
