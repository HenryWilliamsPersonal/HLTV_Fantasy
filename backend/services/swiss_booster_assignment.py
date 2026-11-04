import json
import math
from collections import deque
from typing import Any

try:  # C-implemented Hungarian solver: ~50x faster than the flow below on 18x35
    from scipy.optimize import linear_sum_assignment as _linear_sum_assignment
except Exception:  # pragma: no cover - scipy is in requirements, but keep the pure-Python path
    _linear_sum_assignment = None


BOOSTER_POINT_VALUE = 5.0
BOOSTER_NAMES = {
    0: "Best Pistol Round",
    1: "Bottom of scoreboard",
    2: "Clutch",
    3: "Top of scoreboard",
    4: "Avenger",
    5: "Bait",
    6: "Rambo",
    7: "Flash",
    8: "Mister consistent",
    9: "Kobe",
    10: "Saver",
    11: "Assist",
    12: "Aim bot",
    13: "Quad",
    14: "Carry",
    15: "Cannon fodder",
    16: "Farmer",
    17: "Hellcase",
}

# HLTV's trigger rate is the chance over a BEST-OF-THREE (the user's word,
# 2026-10-27); a best-of-three runs 2.4 maps on average, so a series of m
# expected maps triggers with 1 - (1 - R)^(m / 2.4).
EXPECTED_MAPS = {1: 1.0, 2: 2.0, 3: 2.4, 5: 3.9}


def match_rate(rate_bo3: float, bo: int) -> float:
    """The chance a booster triggers in a series of this length, from
    HLTV's best-of-three rate."""
    maps = EXPECTED_MAPS.get(int(bo or 3), 2.4)
    r = min(1.0, max(0.0, float(rate_bo3)))
    if r >= 1.0:
        return 1.0
    return 1.0 - (1.0 - r) ** (maps / EXPECTED_MAPS[3])


def normalize_probability(value: Any) -> float:
    try:
        p = float(value)
    except Exception:
        return 0.0
    if not math.isfinite(p):
        return 0.0
    if p > 1.0:
        p = p / 100.0
    return max(0.0, min(1.0, p))


def parse_booster_rates(raw: Any) -> dict[int, float]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        return {}
    rates = {}
    for idx in range(18):
        rates[idx] = normalize_probability(raw.get(str(idx), raw.get(idx, 0.0)))
    return rates


class _Edge:
    __slots__ = ("to", "rev", "cap", "cost")

    def __init__(self, to: int, rev: int, cap: int, cost: float):
        self.to = to
        self.rev = rev
        self.cap = cap
        self.cost = cost


def _add_edge(graph: list[list[_Edge]], fr: int, to: int, cap: int, cost: float) -> None:
    graph[fr].append(_Edge(to, len(graph[to]), cap, cost))
    graph[to].append(_Edge(fr, len(graph[fr]) - 1, 0, -cost))


def _max_weight_assignment(weights: list[list[float]]) -> list[tuple[int, int, float]]:
    """Rows to distinct columns, maximum cardinality, maximum total weight.
    Returns (row, col, weight) per assigned row."""
    if not weights or not weights[0]:
        return []
    if _linear_sum_assignment is not None:
        import numpy as np

        w = np.asarray(weights, dtype=np.float64)
        rows_idx, cols_idx = _linear_sum_assignment(w, maximize=True)
        return [(int(r), int(c), float(w[r, c])) for r, c in zip(rows_idx, cols_idx)]
    rows = len(weights)
    cols = len(weights[0])
    source = 0
    row_start = 1
    col_start = row_start + rows
    sink = col_start + cols
    graph = [[] for _ in range(sink + 1)]

    for r in range(rows):
        _add_edge(graph, source, row_start + r, 1, 0.0)
        for c in range(cols):
            _add_edge(graph, row_start + r, col_start + c, 1, -float(weights[r][c]))
    for c in range(cols):
        _add_edge(graph, col_start + c, sink, 1, 0.0)

    flow = 0
    while flow < rows:
        dist = [math.inf] * len(graph)
        in_queue = [False] * len(graph)
        prev_node = [-1] * len(graph)
        prev_edge = [-1] * len(graph)
        dist[source] = 0.0
        q = deque([source])
        in_queue[source] = True
        while q:
            v = q.popleft()
            in_queue[v] = False
            for i, edge in enumerate(graph[v]):
                if edge.cap <= 0:
                    continue
                nd = dist[v] + edge.cost
                if nd + 1e-12 < dist[edge.to]:
                    dist[edge.to] = nd
                    prev_node[edge.to] = v
                    prev_edge[edge.to] = i
                    if not in_queue[edge.to]:
                        q.append(edge.to)
                        in_queue[edge.to] = True
        if prev_node[sink] < 0:
            break
        v = sink
        while v != source:
            edge = graph[prev_node[v]][prev_edge[v]]
            edge.cap -= 1
            graph[v][edge.rev].cap += 1
            v = prev_node[v]
        flow += 1

    assignments = []
    for r in range(rows):
        node = row_start + r
        for edge in graph[node]:
            if col_start <= edge.to < col_start + cols and edge.cap == 0:
                c = edge.to - col_start
                assignments.append((r, c, float(weights[r][c])))
                break
    return assignments


