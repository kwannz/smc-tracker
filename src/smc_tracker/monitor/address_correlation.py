"""地址关联性分析 —— 发现协同行动的地址群（庄家常用多钱包/同一实体）。

数据源：hl_meme_trades（每笔公开成交带 buyer/seller/taker/coin/time）。核心是**确定性硬编码算法**：
- co_movers：同币同向、时间相近(滑窗)反复一起主动成交的地址对 = 高相关。
  用**滑动窗口 + 不应期**而非固定分桶 `t//w`：固定分桶有边界伪影(相隔1秒但跨桶漏判)，
  且把一次拉盘的人群算成协同；滑窗按真实时间差判定，不应期(≥w)使一次持续狂热只记一次「协同事件」。
- 跨币数(distinct coins)：一对地址若在**多个不同币**上反复同向协同 → 远强于单币人群 → 庄家集团硬证据。
- counterparties：频繁互为对手方的地址对（疑似关联钱包/自成交）。
- correlated_with / clusters / clusters_detailed：最相关伙伴、并查集聚合、带跨币与协同次数的群画像。

B2 升级：_pair_stats 额外返回活跃度/总事件数；_union_groups 加二项显著性门；
  co_movers 按 lift 归一排序（消高频偏向）；lead_lag 计数÷活跃度归一净领先。
只读现有表，不新增表/不改 schema。
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from typing import Any

from smc_tracker.util import to_float
from smc_tracker.monitor.cooccur_stats import pair_lift, is_significant


class AddressCorrelation:
    def __init__(self, store: Any, cfg: Any = None) -> None:
        """
        store: Store 实例（提供 conn）
        cfg:   CorrelationCfg 或 None（None → 使用默认阈值）
        """
        self.store = store
        # 延迟导入避免循环依赖；cfg=None 时构造默认
        if cfg is None:
            from smc_tracker.config import CorrelationCfg
            cfg = CorrelationCfg()
        self._cfg = cfg

    # ---- 核心：滑窗 + 不应期统计「协同事件」 ----
    def _pair_stats(
        self, since_ms: int, window_sec: int = 60
    ) -> tuple[Counter, dict[tuple[str, str], set[str]], dict[str, int], int]:
        """返回 (pair_counts, pair_coins, activity, total_events)。

        pair_counts[(A,B)] = A、B 一起主动成交的「协同事件」次数(不应期去重，防单次狂热膨胀)；
        pair_coins[(A,B)]  = 这对地址协同过的不同币集合(跨币数越多→越像同一实体)；
        activity[addr]     = 该地址参与的协同事件总次数（暴露度归一基）；
        total_events       = Σ pair_counts.values()（全体协同事件数）。
        """
        rows = self.store.conn.execute(
            "SELECT taker, coin, taker_side, time_ms FROM hl_meme_trades "
            "WHERE time_ms>=? AND taker!='' ORDER BY time_ms", (since_ms,)).fetchall()
        w = window_sec * 1000
        # 按 (coin, side) 分组，组内按时间已序(SQL ORDER BY time_ms)
        groups: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
        for taker, coin, side, t in rows:
            groups[(coin, side)].append((int(t), taker))

        counts: Counter = Counter()
        coins: dict[tuple[str, str], set[str]] = defaultdict(set)
        for (coin, side), lst in groups.items():
            # 不应期字典按 (coin, side) 组局部化：避免买/卖两组共享状态导致跨方向去重错乱
            last_evt: dict[tuple, int] = {}    # (pair) -> 上次记事件时间(本组内)
            win: deque[tuple[int, str]] = deque()
            for t, addr in lst:
                while win and t - win[0][0] > w:    # 维护 [t-w, t] 滑动窗口
                    win.popleft()
                partners = {a for _, a in win if a != addr}   # 窗内其他地址
                for p in partners:
                    key = (addr, p) if addr < p else (p, addr)
                    # 不应期：同一对在本(coin,side)组 < w 内的连续重叠只记一次协同事件
                    if t - last_evt.get(key, -10 ** 18) >= w:
                        counts[key] += 1
                        coins[key].add(coin)
                        last_evt[key] = t
                win.append((t, addr))

        # 计算每地址活跃度：该地址出现在所有 pair_counts 的总计数
        activity: dict[str, int] = defaultdict(int)
        total_events = 0
        for (a, b), c in counts.items():
            activity[a] += c
            activity[b] += c
            total_events += c

        return counts, coins, dict(activity), total_events

    def co_movers(self, since_ms: int, window_sec: int = 60, min_shared: int = 3,
                  limit: int = 30) -> list[tuple[str, str, int]]:
        """返回 [(地址A, 地址B, 协同事件次数), ...]，按 lift 归一排序（消高频偏向），过滤 < min_shared。

        排序由 lift（活跃度归一强度）决定，而非绝对 count——消除高频地址结构性偏向。
        c 仍为原始协同事件次数（向后兼容）。
        """
        counts, _, activity, total_events = self._pair_stats(since_ms, window_sec)
        pairs = [(a, b, c) for (a, b), c in counts.items() if c >= min_shared]

        # 计算 lift 用于排序
        def _lift(a: str, b: str, c: int) -> float:
            l, _ = pair_lift(c, activity.get(a, 0), activity.get(b, 0), total_events)
            return l

        pairs.sort(key=lambda t: _lift(t[0], t[1], t[2]), reverse=True)
        return pairs[:limit]

    def counterparties(self, since_ms: int, min_count: int = 5,
                       limit: int = 30) -> list[tuple[str, str, int]]:
        """频繁互为对手方的地址对（疑似关联钱包/自成交）。"""
        return self.store.conn.execute(
            "SELECT buyer, seller, COUNT(*) c FROM hl_meme_trades "
            "WHERE time_ms>=? AND buyer!='' AND seller!='' AND buyer!=seller "
            "GROUP BY buyer, seller HAVING c>=? ORDER BY c DESC LIMIT ?",
            (since_ms, min_count, limit)).fetchall()

    def correlated_with(self, address: str, since_ms: int, window_sec: int = 60,
                        min_shared: int = 2, limit: int = 15) -> list[tuple[str, int]]:
        """与指定地址协同事件最多的地址（已按 lift 排序的 co_movers 结果中过滤）。"""
        a = address                      # 与存储一致(HL 地址本即小写)，不强转
        out: list[tuple[str, int]] = []
        for x, y, c in self.co_movers(since_ms, window_sec, min_shared, limit=10_000):
            if x == a:
                out.append((y, c))
            elif y == a:
                out.append((x, c))
        out.sort(key=lambda t: t[1], reverse=True)
        return out[:limit]

    def _union_groups(
        self,
        counts: Counter,
        coins: dict,
        activity: dict[str, int],
        total_events: int,
        min_shared: int,
        min_coins: int,
        min_lift: float,
        max_p: float,
    ) -> list[list[str]]:
        """并查集：满足 协同次数≥min_shared 且 跨币数≥min_coins 且显著(lift/p)的对进群。

        显著性门：pair_lift 二项 null model → is_significant → 过滤随机人群（高频追涨散户）。
        """
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for (a, b), c in counts.items():
            if c < min_shared:
                continue
            if len(coins[(a, b)]) < min_coins:
                continue
            # B2 显著性门：lift + p-value 双重过滤
            lift, p = pair_lift(c, activity.get(a, 0), activity.get(b, 0), total_events)
            if not is_significant(lift, p, min_lift, max_p):
                continue
            parent[find(a)] = find(b)

        groups: dict[str, list[str]] = defaultdict(list)
        for node in list(parent):
            groups[find(node)].append(node)
        return [sorted(g) for g in groups.values() if len(g) >= 2]

    def clusters(self, since_ms: int, window_sec: int = 60, min_shared: int | None = None,
                 min_coins: int | None = None) -> list[list[str]]:
        """把高相关地址对并查集聚合成地址群（庄家集团候选）。

        min_coins>1 时要求成对地址跨≥min_coins 个不同币协同——过滤「单币拉盘人群」误判。
        min_shared/min_coins 不传时使用 cfg 默认值。
        """
        if min_shared is None:
            min_shared = self._cfg.min_shared
        if min_coins is None:
            min_coins = self._cfg.min_coins
        counts, coins, activity, total_events = self._pair_stats(since_ms, window_sec)
        return self._union_groups(
            counts, coins, activity, total_events,
            min_shared, min_coins, self._cfg.min_lift, self._cfg.max_p,
        )

    def clusters_detailed(self, since_ms: int, window_sec: int = 60,
                          min_shared: int | None = None,
                          min_coins: int | None = None) -> list[dict[str, Any]]:
        """带画像的地址群：{members, size, links(内部成对数), events(总协同次数), coins(跨币数)}。

        coins/events 越大 → 越像同一实体的庄家集团(硬证据)，供告警与 LLM 分析层使用。
        min_shared/min_coins 不传时使用 cfg 默认值。
        """
        if min_shared is None:
            min_shared = self._cfg.min_shared
        if min_coins is None:
            min_coins = self._cfg.min_coins
        counts, coins, activity, total_events = self._pair_stats(since_ms, window_sec)
        groups = self._union_groups(
            counts, coins, activity, total_events,
            min_shared, min_coins, self._cfg.min_lift, self._cfg.max_p,
        )
        out: list[dict[str, Any]] = []
        for g in groups:
            members = set(g)
            links = 0
            events = 0
            coinset: set[str] = set()
            for (a, b), c in counts.items():
                if a in members and b in members and c >= min_shared \
                        and len(coins[(a, b)]) >= min_coins:
                    links += 1
                    events += c
                    coinset |= coins[(a, b)]
            out.append({"members": g, "size": len(g), "links": links,
                        "events": events, "coins": len(coinset),
                        "coin_list": sorted(coinset)})
        out.sort(key=lambda d: (d["coins"], d["events"], d["size"]), reverse=True)
        return out

    # ---- 协同 lead-lag：识别群内核心 leader（谁先动）----
    def lead_lag(self, addresses: list[str], since_ms: int,
                 window_sec: int = 60) -> list[tuple[str, float, int, int]]:
        """时滞互相关——对指定地址集合，找出谁在同币同向建仓时始终更早（领先者）。

        算法：time-lagged cross-correlation（方向性）。
        - 查 hl_meme_trades，since_ms 起、仅限 addresses 集合内的 taker，time_ms 升序。
        - 按 (coin, taker_side) 分组（同向才比先后）。
        - 组内对每事件 e_j，回看窗口 w=window_sec*1000ms 内更早不同地址事件 e_i，
          记 addr_i 领先 addr_j 一次，net[(addr_i,addr_j)]+=1。
        - 不应期：同一有序对 (addr_i,addr_j) 在同(coin,side)组内 < w 内连续重叠只记一次，
          防单次狂热膨胀（参考 _pair_stats 写法）。
        - B2 归一：score[a] = Σ_b (net[(a,b)] - net[(b,a)]) / max(activity[a], 1)
          （活跃度归一净领先，消超高频地址结构性膨胀）。
        返回 [(address, score, leads, lags), ...] 按 score 降序，空数据返 []。
        score 为归一化浮点数（符号不变，cluster_leader 判 score>0 零破坏）。
        """
        if not addresses:
            return []
        addr_set = set(addresses)
        rows = self.store.conn.execute(
            "SELECT taker, coin, taker_side, time_ms FROM hl_meme_trades "
            "WHERE time_ms>=? AND taker!='' ORDER BY time_ms", (since_ms,)).fetchall()

        w = window_sec * 1000

        # 按 (coin, side) 分组，仅保留 addr_set 内的 taker
        groups: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
        for taker, coin, side, t in rows:
            if taker in addr_set:
                groups[(coin, side)].append((int(to_float(t, 0.0)), taker))

        # net[(addr_i, addr_j)] = addr_i 领先 addr_j 的次数（有向）
        net: Counter = Counter()

        for (_coin, _side), lst in groups.items():
            # 不应期：记录有序对 (leader, follower) 上次触发时间（本(coin,side)组内局部化）
            last_lead: dict[tuple[str, str], int] = {}
            win: deque[tuple[int, str]] = deque()
            for t, addr in lst:
                # 维护 [t-w, t) 的滑动窗口（仅保留 addr!=当前的早者）
                while win and t - win[0][0] > w:
                    win.popleft()
                # 窗内所有比 addr 更早的不同地址均领先本次 addr
                for t_i, addr_i in win:
                    if addr_i == addr:
                        continue
                    pair = (addr_i, addr)      # addr_i 领先 addr
                    # 不应期：同一有序对在本(coin,side)组 < w 内的连续重叠只记一次
                    if t - last_lead.get(pair, -10 ** 18) >= w:
                        net[pair] += 1
                        last_lead[pair] = t
                win.append((t, addr))

        if not net:
            return []

        # 汇总每地址的 leads/lags
        leads: Counter = Counter()
        lags: Counter = Counter()
        for (addr_i, addr_j), cnt in net.items():
            leads[addr_i] += cnt
            lags[addr_j] += cnt

        # B2 归一：净领先 ÷ 活跃度（leads + lags）消超高频地址偏向
        result: list[tuple[str, float, int, int]] = []
        for addr in addr_set:
            l_cnt = int(leads.get(addr, 0))
            g_cnt = int(lags.get(addr, 0))
            raw_score = l_cnt - g_cnt
            activity = l_cnt + g_cnt
            # 活跃度归一净领先（符号保持，cluster_leader 判 score>0 零破坏）
            score: float = raw_score / max(activity, 1)
            result.append((addr, score, l_cnt, g_cnt))
        result.sort(key=lambda x: x[1], reverse=True)
        return result

    def cluster_leader(self, members: list[str], since_ms: int,
                       window_sec: int = 60) -> tuple[str, float] | None:
        """返回群内得分最高的 leader，score>0 才视为显著领先；否则返回 None（诚实）。

        调 lead_lag，取 score 最高的地址：
        - 最高 score <= 0（无领先关系 / 无数据）→ None。
        - 最高 score > 0 → (leader_address, score)。
        score 为归一化浮点（B2 活跃度归一后）。
        """
        if not members:
            return None
        ll = self.lead_lag(members, since_ms, window_sec)
        if not ll:
            return None
        top_addr, top_score, _leads, _lags = ll[0]
        if top_score <= 0:
            return None
        return (top_addr, top_score)
