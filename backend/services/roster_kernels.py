"""Compiled roster searches (numba) with pure-Python fallbacks.

Two kernels, both over ONE outcome's additive player scores:

* best roster: the highest-scoring legal 5-player roster (budget, per-team
  cap, forced players, excluded players) — a depth-first search over the
  players sorted by score with an exact prefix bound and a cheapest-completion
  budget check. Batched and parallel across outcomes for the joint-outcome
  winner tables.
* top-k rosters: the k best legal rosters of one outcome (ceiling search).

The searches themselves are the ones groups.py ran in Python at ~0.8 ms per
outcome; compiled they run in a few microseconds, so per-outcome winners can
be recomputed live for any include / exclude filter.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

try:  # numba is in requirements; the Python fallbacks keep everything working without it
    from numba import njit, prange

    HAVE_NUMBA = True
except Exception:  # pragma: no cover
    HAVE_NUMBA = False

    def njit(*args, **kwargs):  # type: ignore[misc]
        def wrap(fn):
            return fn

        if args and callable(args[0]) and not kwargs:
            return args[0]
        return wrap

    prange = range  # type: ignore[assignment]


ROSTER_SIZE = 5


@njit(cache=True)
def _prepare(scores, prices, team_of, allowed):
    """Players allowed in the search, sorted by score descending, plus the
    cheapest-completion table: cheap[i, m] = cheapest total price of m players
    taken from sorted position i onwards (0 when fewer than m remain)."""
    n_all = scores.shape[0]
    n = 0
    for i in range(n_all):
        if allowed[i]:
            n += 1
    idx = np.empty(n, dtype=np.int64)
    j = 0
    for i in range(n_all):
        if allowed[i]:
            idx[j] = i
            j += 1
    sub = np.empty(n, dtype=np.float64)
    for j in range(n):
        sub[j] = -scores[idx[j]]
    order = np.argsort(sub)
    sorted_idx = np.empty(n, dtype=np.int64)
    sc = np.empty(n, dtype=np.float64)
    pr = np.empty(n, dtype=np.int64)
    tm = np.empty(n, dtype=np.int64)
    for j in range(n):
        k = idx[order[j]]
        sorted_idx[j] = k
        sc[j] = scores[k]
        pr[j] = prices[k]
        tm[j] = team_of[k]
    # prefix sums of the sorted scores: best completion of m from position i = pref[i+m] - pref[i]
    pref = np.zeros(n + 1, dtype=np.float64)
    for j in range(n):
        pref[j + 1] = pref[j] + sc[j]
    cheap = np.zeros((n + 1, ROSTER_SIZE + 1), dtype=np.int64)
    tail = np.empty(ROSTER_SIZE, dtype=np.int64)
    tail_n = 0
    for i in range(n - 1, -1, -1):
        # insert pr[i] into the sorted tail of the cheapest ROSTER_SIZE prices
        p = pr[i]
        if tail_n < ROSTER_SIZE:
            pos = tail_n
            tail_n += 1
        else:
            if p >= tail[ROSTER_SIZE - 1]:
                pos = -1
            else:
                pos = ROSTER_SIZE - 1
        if pos >= 0:
            while pos > 0 and tail[pos - 1] > p:
                tail[pos] = tail[pos - 1]
                pos -= 1
            tail[pos] = p
        acc = 0
        for m in range(1, ROSTER_SIZE + 1):
            if m <= tail_n:
                acc += tail[m - 1]
            cheap[i, m] = acc
    return sorted_idx, sc, pr, tm, pref, cheap


@njit(cache=True)
def _search(sc, pr, tm, pref, cheap, need, budget, cap, base_cost, base_score, base_counts, n_teams, k, out_scores, out_rosters):
    """Depth-first search for the k best rosters of `need` more players.
    out_scores (k, desc), out_rosters (k, need) receive the results (positions
    into the sorted arrays); returns how many were found."""
    n = sc.shape[0]
    found = 0
    counts = base_counts.copy()
    chosen = np.empty(need, dtype=np.int64)
    # explicit stack: next candidate position per depth
    pos = np.zeros(need + 1, dtype=np.int64)
    cost = np.zeros(need + 1, dtype=np.int64)
    score = np.zeros(need + 1, dtype=np.float64)
    depth = 0
    pos[0] = 0
    cost[0] = base_cost
    score[0] = base_score
    if need == 0:
        out_scores[0] = base_score
        return 1
    while depth >= 0:
        remaining = need - depth
        i = pos[depth]
        if i > n - remaining:
            # exhausted this level: backtrack
            depth -= 1
            if depth >= 0:
                t = tm[chosen[depth]]
                counts[t] -= 1
                pos[depth] += 1
            continue
        # bound: nothing at or after i can beat the k-th best
        bar = out_scores[k - 1] if found >= k else -1e300
        if score[depth] + (pref[i + remaining] - pref[i]) <= bar:
            depth -= 1
            if depth >= 0:
                t = tm[chosen[depth]]
                counts[t] -= 1
                pos[depth] += 1
            continue
        if cost[depth] + pr[i] + cheap[i + 1, remaining - 1] > budget:
            pos[depth] += 1
            continue
        t = tm[i]
        if counts[t] >= cap:
            pos[depth] += 1
            continue
        # take i
        chosen[depth] = i
        counts[t] += 1
        if remaining == 1:
            s = score[depth] + sc[i]
            if found < k or s > out_scores[k - 1]:
                # insert into the sorted top-k
                slot = found if found < k else k - 1
                while slot > 0 and out_scores[slot - 1] < s:
                    out_scores[slot] = out_scores[slot - 1]
                    for d in range(need):
                        out_rosters[slot, d] = out_rosters[slot - 1, d]
                    slot -= 1
                out_scores[slot] = s
                for d in range(need):
                    out_rosters[slot, d] = chosen[d]
                if found < k:
                    found += 1
            counts[t] -= 1
            pos[depth] += 1
            continue
        cost[depth + 1] = cost[depth] + pr[i]
        score[depth + 1] = score[depth] + sc[i]
        pos[depth + 1] = i + 1
        depth += 1
    return found


@njit(cache=True)
def _solve_one(scores, prices, team_of, n_teams, allowed, forced, budget, cap, k, out_scores, out_rosters):
    """k best rosters for one outcome. forced players are always in; allowed
    excludes them and the excluded players. Returns the count found; rosters
    are player indices (forced first)."""
    n_forced = 0
    base_cost = 0
    base_score = 0.0
    base_counts = np.zeros(n_teams, dtype=np.int64)
    for i in range(scores.shape[0]):
        if forced[i]:
            n_forced += 1
            base_cost += prices[i]
            base_score += scores[i]
            base_counts[team_of[i]] += 1
    need = ROSTER_SIZE - n_forced
    if need < 0 or base_cost > budget:
        return 0
    for t in range(n_teams):
        if base_counts[t] > cap:
            return 0
    sorted_idx, sc, pr, tm, pref, cheap = _prepare(scores, prices, team_of, allowed)
    tmp_scores = np.empty(k, dtype=np.float64)
    tmp_rosters = np.empty((k, max(need, 1)), dtype=np.int64)
    found = _search(sc, pr, tm, pref, cheap, need, budget, cap, base_cost, base_score, base_counts, n_teams, k, tmp_scores, tmp_rosters)
    for r in range(found):
        out_scores[r] = tmp_scores[r]
        d = 0
        for i in range(scores.shape[0]):
            if forced[i]:
                out_rosters[r, d] = i
                d += 1
        for j in range(need):
            out_rosters[r, d] = sorted_idx[tmp_rosters[r, j]]
            d += 1
    return found


@njit(parallel=True, cache=True)
def _best_batch(M, prices, team_of, n_teams, allowed, forced, budget, cap, out):
    """Best roster per outcome column of M (players × outcomes) → out (outcomes × 5), -1 when none."""
    n_out = M.shape[1]
    for c in prange(n_out):
        scores = M[:, c].copy()
        sc = np.empty(1, dtype=np.float64)
        ro = np.empty((1, ROSTER_SIZE), dtype=np.int64)
        found = _solve_one(scores, prices, team_of, n_teams, allowed, forced, budget, cap, 1, sc, ro)
        if found > 0:
            for d in range(ROSTER_SIZE):
                out[c, d] = ro[0, d]
        else:
            for d in range(ROSTER_SIZE):
                out[c, d] = -1


def best_rosters_batch(
    M: np.ndarray, prices: np.ndarray, team_of: np.ndarray, budget: int, cap: int,
    forced: Optional[np.ndarray] = None, excluded: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Best legal roster in every outcome column of M: (outcomes × 5) player
    indices, -1 rows where no legal roster exists. team_of must be small
    non-negative integers (dense team indices)."""
    M = np.ascontiguousarray(M, dtype=np.float64)
    n_players = M.shape[0]
    prices = np.asarray(prices, dtype=np.int64)
    team_of = np.asarray(team_of, dtype=np.int64)
    forced_arr = np.zeros(n_players, dtype=np.bool_) if forced is None else np.asarray(forced, dtype=np.bool_)
    excluded_arr = np.zeros(n_players, dtype=np.bool_) if excluded is None else np.asarray(excluded, dtype=np.bool_)
    allowed = ~(forced_arr | excluded_arr)
    n_teams = int(team_of.max()) + 1 if n_players else 1
    out = np.empty((M.shape[1], ROSTER_SIZE), dtype=np.int64)
    if HAVE_NUMBA:
        _best_batch(M, prices, team_of, n_teams, allowed, forced_arr, int(budget), int(cap), out)
        return out
    for c in range(M.shape[1]):
        res = top_rosters_one(M[:, c], prices, team_of, budget, cap, 1, forced_arr, excluded_arr)
        out[c] = res[0][1] if res else np.full(ROSTER_SIZE, -1)
    return out


def top_rosters_one(
    scores: np.ndarray, prices: np.ndarray, team_of: np.ndarray, budget: int, cap: int, k: int,
    forced: Optional[np.ndarray] = None, excluded: Optional[np.ndarray] = None,
) -> List[Tuple[float, np.ndarray]]:
    """The k best legal rosters of one outcome: [(score, player indices)], best first."""
    scores = np.ascontiguousarray(scores, dtype=np.float64)
    n_players = scores.shape[0]
    prices = np.asarray(prices, dtype=np.int64)
    team_of = np.asarray(team_of, dtype=np.int64)
    forced_arr = np.zeros(n_players, dtype=np.bool_) if forced is None else np.asarray(forced, dtype=np.bool_)
    excluded_arr = np.zeros(n_players, dtype=np.bool_) if excluded is None else np.asarray(excluded, dtype=np.bool_)
    allowed = ~(forced_arr | excluded_arr)
    n_teams = int(team_of.max()) + 1 if n_players else 1
    out_scores = np.empty(k, dtype=np.float64)
    out_rosters = np.empty((k, ROSTER_SIZE), dtype=np.int64)
    if HAVE_NUMBA:
        found = _solve_one(scores, prices, team_of, n_teams, allowed, forced_arr, int(budget), int(cap), int(k), out_scores, out_rosters)
    else:
        found = _solve_one_py(scores, prices, team_of, allowed, forced_arr, int(budget), int(cap), int(k), out_scores, out_rosters)
    return [(float(out_scores[r]), out_rosters[r].copy()) for r in range(found)]


def _solve_one_py(scores, prices, team_of, allowed, forced, budget, cap, k, out_scores, out_rosters) -> int:
    """Pure-Python fallback of _solve_one (same search, same results)."""
    import heapq

    n = len(scores)
    forced_idx = [i for i in range(n) if forced[i]]
    need = ROSTER_SIZE - len(forced_idx)
    base_cost = sum(int(prices[i]) for i in forced_idx)
    base_score = sum(float(scores[i]) for i in forced_idx)
    counts: dict = {}
    for i in forced_idx:
        counts[int(team_of[i])] = counts.get(int(team_of[i]), 0) + 1
    if need < 0 or base_cost > budget or any(v > cap for v in counts.values()):
        return 0
    pool = sorted((i for i in range(n) if allowed[i]), key=lambda i: -float(scores[i]))
    sc = [float(scores[i]) for i in pool]
    pr = [int(prices[i]) for i in pool]
    tm = [int(team_of[i]) for i in pool]
    m = len(pool)
    suffix = [[0] * (ROSTER_SIZE + 1) for _ in range(m + 1)]
    tail: List[int] = []
    for i in range(m - 1, -1, -1):
        tail.append(pr[i])
        tail.sort()
        if len(tail) > ROSTER_SIZE:
            tail.pop()
        acc = 0
        sums = [0]
        for q in range(1, ROSTER_SIZE + 1):
            acc += tail[q - 1] if q <= len(tail) else 0
            sums.append(acc)
        suffix[i] = sums
    heap: list = []
    counter = 0
    chosen: List[int] = []

    def dfs(start: int, cost: int, score: float) -> None:
        nonlocal counter
        remaining = need - len(chosen)
        if remaining == 0:
            if len(heap) < k or score > heap[0][0]:
                counter += 1
                entry = (score, counter, tuple(chosen))
                if len(heap) < k:
                    heapq.heappush(heap, entry)
                else:
                    heapq.heappushpop(heap, entry)
            return
        for i in range(start, m - remaining + 1):
            bar = heap[0][0] if len(heap) >= k else -1e300
            if score + sum(sc[i:i + remaining]) <= bar:
                return
            if cost + pr[i] + suffix[i + 1][remaining - 1] > budget:
                continue
            t = tm[i]
            if counts.get(t, 0) >= cap:
                continue
            counts[t] = counts.get(t, 0) + 1
            chosen.append(i)
            dfs(i + 1, cost + pr[i], score + sc[i])
            chosen.pop()
            counts[t] -= 1

    dfs(0, base_cost, base_score)
    ranked = sorted(heap, key=lambda e: -e[0])
    for r, (score, _c, picks) in enumerate(ranked):
        out_scores[r] = score
        out_rosters[r, :] = forced_idx + [pool[i] for i in picks]
    return len(ranked)


# ---------------------------------------------------------------------------
# Batched top-k rosters (ceiling search), per-outcome roster bounds and the
# fused roster-plan statistics used by Best Single Outcome / Most Likely Winner.
# ---------------------------------------------------------------------------


@njit(parallel=True, cache=True)
def _top_batch(M, prices, team_of, n_teams, allowed, forced, budget, cap, k, out_scores, out_rosters, out_found):
    """k best rosters per outcome column of M (players x outcomes), parallel
    over columns -> out_scores (outcomes x k), out_rosters (outcomes x k x 5),
    out_found (outcomes)."""
    n_out = M.shape[1]
    for c in prange(n_out):
        scores = M[:, c].copy()
        sc = np.empty(k, dtype=np.float64)
        ro = np.empty((k, ROSTER_SIZE), dtype=np.int64)
        found = _solve_one(scores, prices, team_of, n_teams, allowed, forced, budget, cap, k, sc, ro)
        out_found[c] = found
        for r in range(found):
            out_scores[c, r] = sc[r]
            for d in range(ROSTER_SIZE):
                out_rosters[c, r, d] = ro[r, d]


def top_rosters_batch(
    M: np.ndarray, prices: np.ndarray, team_of: np.ndarray, budget: int, cap: int, k: int,
    forced: Optional[np.ndarray] = None, excluded: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The k best legal rosters of every outcome column of M, best first:
    (scores (outcomes x k), player-index rosters (outcomes x k x 5), found
    count per outcome). Entries past the found count are undefined."""
    M = np.ascontiguousarray(M, dtype=np.float64)
    n_players = M.shape[0]
    n_out = M.shape[1]
    prices = np.asarray(prices, dtype=np.int64)
    team_of = np.asarray(team_of, dtype=np.int64)
    forced_arr = np.zeros(n_players, dtype=np.bool_) if forced is None else np.asarray(forced, dtype=np.bool_)
    excluded_arr = np.zeros(n_players, dtype=np.bool_) if excluded is None else np.asarray(excluded, dtype=np.bool_)
    allowed = ~(forced_arr | excluded_arr)
    n_teams = int(team_of.max()) + 1 if n_players else 1
    out_scores = np.empty((n_out, k), dtype=np.float64)
    out_rosters = np.empty((n_out, k, ROSTER_SIZE), dtype=np.int64)
    out_found = np.zeros(n_out, dtype=np.int64)
    if HAVE_NUMBA:
        _top_batch(M, prices, team_of, n_teams, allowed, forced_arr, int(budget), int(cap), int(k), out_scores, out_rosters, out_found)
        return out_scores, out_rosters, out_found
    for c in range(n_out):
        res = top_rosters_one(M[:, c], prices, team_of, budget, cap, k, forced_arr, excluded_arr)
        out_found[c] = len(res)
        for r, (s, roster) in enumerate(res):
            out_scores[c, r] = s
            out_rosters[c, r] = roster
    return out_scores, out_rosters, out_found


@njit(parallel=True, cache=True)
def _lagrangian_bound_cols(M, prices, lams, budget, out):
    """Per outcome column: min over lambda of (sum of the five largest
    M[:, c] - lambda * price) + lambda * budget, an upper bound on any
    five-player roster within budget (lambda = 0 is the plain five-best sum)."""
    n_players = M.shape[0]
    n_out = M.shape[1]
    n_lam = lams.shape[0]
    for c in prange(n_out):
        best = 1e300
        top = np.empty(ROSTER_SIZE, dtype=np.float64)
        for li in range(n_lam):
            lam = lams[li]
            for d in range(ROSTER_SIZE):
                top[d] = -1e300
            for j in range(n_players):
                v = M[j, c] - lam * prices[j]
                if v > top[ROSTER_SIZE - 1]:
                    slot = ROSTER_SIZE - 1
                    while slot > 0 and top[slot - 1] < v:
                        top[slot] = top[slot - 1]
                        slot -= 1
                    top[slot] = v
            s = lam * budget
            for d in range(ROSTER_SIZE):
                s += top[d]
            if s < best:
                best = s
        out[c] = best


def lagrangian_bound_cols(M: np.ndarray, prices: np.ndarray, lams, budget: float) -> np.ndarray:
    """Upper bound of the best roster in every outcome column (see
    _lagrangian_bound_cols); lams should include 0."""
    M = np.ascontiguousarray(M, dtype=np.float64)
    prices_f = np.asarray(prices, dtype=np.float64)
    lams_arr = np.asarray(list(lams), dtype=np.float64)
    out = np.empty(M.shape[1], dtype=np.float64)
    if HAVE_NUMBA:
        _lagrangian_bound_cols(M, prices_f, lams_arr, float(budget), out)
        return out
    out[:] = np.inf
    for lam in lams_arr:
        adj = M - lam * prices_f[:, None]
        out = np.minimum(out, np.partition(adj, -ROSTER_SIZE, axis=0)[-ROSTER_SIZE:].sum(axis=0) + lam * float(budget))
    return out


@njit(parallel=True, cache=True)
def _plan_block_stats(RW, MP, MR, probs, rows, role_pm, tables, block, avg_b, max_b, argmax_b, mass_b, best_i):
    """Fused statistics of every candidate roster's plan score
        S[i, c] = sum_p RW[j_p, c] + role_pm[i, p] * MR[j_p, c] + tables[i, p, MP[j_p, c]]
    over blocks of outcome columns (parallel over blocks, so each block's slice
    of the player tables stays in cache across all rosters). Per block b and
    roster i: the probability-weighted sum, the max and its first outcome,
    and the probability mass tied at that max; per outcome, the first roster
    beating the previous best by more than 1e-9 (best_i)."""
    C = rows.shape[0]
    N = RW.shape[1]
    nb = (N + block - 1) // block
    for b in prange(nb):
        c0 = b * block
        c1 = min(N, c0 + block)
        w = c1 - c0
        bv = np.full(w, -1e300)
        bi = np.full(w, -1, dtype=np.int64)
        sb = np.empty(w, dtype=np.float64)
        for i in range(C):
            j0 = rows[i, 0]
            j1 = rows[i, 1]
            j2 = rows[i, 2]
            j3 = rows[i, 3]
            j4 = rows[i, 4]
            r0 = role_pm[i, 0]
            r1 = role_pm[i, 1]
            r2 = role_pm[i, 2]
            r3 = role_pm[i, 3]
            r4 = role_pm[i, 4]
            a = 0.0
            mx = -1e300
            am = -1
            for c in range(c0, c1):
                s = (
                    RW[j0, c] + r0 * MR[j0, c] + tables[i, 0, MP[j0, c]]
                    + RW[j1, c] + r1 * MR[j1, c] + tables[i, 1, MP[j1, c]]
                    + RW[j2, c] + r2 * MR[j2, c] + tables[i, 2, MP[j2, c]]
                    + RW[j3, c] + r3 * MR[j3, c] + tables[i, 3, MP[j3, c]]
                    + RW[j4, c] + r4 * MR[j4, c] + tables[i, 4, MP[j4, c]]
                )
                sb[c - c0] = s
                a += s * probs[c]
                if s > mx:
                    mx = s
                    am = c
                if s > bv[c - c0] + 1e-9:
                    bv[c - c0] = s
                    bi[c - c0] = i
            # probability mass tied at the block max, and the first tied
            # outcome (deterministic argmax whatever the summation order)
            m = 0.0
            am = -1
            for c in range(c0, c1):
                if sb[c - c0] >= mx - 1e-9:
                    m += probs[c]
                    if am < 0:
                        am = c
            avg_b[b, i] = a
            max_b[b, i] = mx
            argmax_b[b, i] = am
            mass_b[b, i] = m
        for c in range(c0, c1):
            best_i[c] = bi[c - c0]


def plan_block_stats(RW, MP, MR, probs, rows, role_pm, tables, block: int = 512):
    """For candidate rosters (rows: C x 5 player indices) with plan parameters
    role_pm (C x 5) and booster prefix tables (C x 5 x slots): (avg, ceiling,
    argmax outcome, ceiling probability, best roster per outcome) over the
    outcome tables RW / MP / MR (players x outcomes) and the outcome
    probabilities. Returns None without numba (callers keep their numpy path)."""
    if not HAVE_NUMBA:
        return None
    RW = np.ascontiguousarray(RW, dtype=np.float64)
    MR = np.ascontiguousarray(MR, dtype=np.float64)
    MP = np.ascontiguousarray(MP)
    if not np.issubdtype(MP.dtype, np.integer):
        MP = MP.astype(np.int64)
    probs = np.ascontiguousarray(probs, dtype=np.float64)
    rows = np.ascontiguousarray(rows, dtype=np.int64)
    role_pm = np.ascontiguousarray(role_pm, dtype=np.float64)
    tables = np.ascontiguousarray(tables, dtype=np.float64)
    C = rows.shape[0]
    N = RW.shape[1]
    best_i = np.full(N, -1, dtype=np.int64)
    if C == 0 or N == 0:
        return np.zeros(C), np.zeros(C), np.full(C, -1, dtype=np.int64), np.zeros(C), best_i
    block = max(1, min(int(block), N))
    nb = (N + block - 1) // block
    avg_b = np.zeros((nb, C), dtype=np.float64)
    max_b = np.full((nb, C), -1e300, dtype=np.float64)
    argmax_b = np.full((nb, C), -1, dtype=np.int64)
    mass_b = np.zeros((nb, C), dtype=np.float64)
    _plan_block_stats(RW, MP, MR, probs, rows, role_pm, tables, int(block), avg_b, max_b, argmax_b, mass_b, best_i)
    avg = avg_b.sum(axis=0)
    ceil = max_b.max(axis=0)
    tied = max_b >= ceil[None, :] - 1e-9
    bsel = tied.argmax(axis=0)  # first block within 1e-9 of the ceiling
    argmax = argmax_b[bsel, np.arange(C)]
    ceil_p = (mass_b * tied).sum(axis=0)
    return avg, ceil, argmax, ceil_p, best_i


@njit(cache=True)
def _plan_score(RW, MP, MR, rows, role_pm, tables, c, o):
    s = 0.0
    for p in range(ROSTER_SIZE):
        j = rows[c, p]
        s += RW[j, o] + role_pm[c, p] * MR[j, o] + tables[c, p, MP[j, o]]
    return s


@njit(parallel=True, cache=True)
def _season_tier_mass(RW, MP, MR, probs, f_rows, f_rpm, f_tab, cut_pos, c_rows, c_rpm, c_tab, block, mass_b):
    N = RW.shape[1]
    nf = f_rows.shape[0]
    C = c_rows.shape[0]
    K = cut_pos.shape[0]
    nb = mass_b.shape[0]
    for b in prange(nb):
        buf = np.empty(nf)
        cuts = np.empty(K)
        o1 = min(N, (b + 1) * block)
        for o in range(b * block, o1):
            for f in range(nf):
                buf[f] = -_plan_score(RW, MP, MR, f_rows, f_rpm, f_tab, f, o)
            srt = np.sort(buf)  # best first (negated)
            for k in range(K):
                cuts[k] = -srt[cut_pos[k]]
            w = probs[o]
            for c in range(C):
                s = _plan_score(RW, MP, MR, c_rows, c_rpm, c_tab, c, o) + 1e-9
                t = 0
                for k in range(K):
                    if s < cuts[k]:
                        t += 1
                mass_b[b, c, t] += w


def season_tier_mass(RW, MP, MR, probs, f_rows, f_rpm, f_tab, cut_pos, c_rows, c_rpm, c_tab, block: int = 256):
    """(candidates x tiers) probability mass of each tier a candidate roster
    reaches against the field: per outcome the field's scores (rows / role
    per-match points / booster prefix tables, as plan_block_stats) are sorted
    and read at cut_pos (best first) to give the score each tier needs. None
    without numba (callers keep their numpy path)."""
    if not HAVE_NUMBA:
        return None
    RW = np.ascontiguousarray(RW, dtype=np.float64)
    MR = np.ascontiguousarray(MR, dtype=np.float64)
    MP = np.ascontiguousarray(MP)
    if not np.issubdtype(MP.dtype, np.integer):
        MP = MP.astype(np.int64)
    N = RW.shape[1]
    C = c_rows.shape[0]
    block = max(1, min(int(block), N))
    nb = (N + block - 1) // block
    mass_b = np.zeros((nb, C, len(cut_pos) + 1), dtype=np.float64)
    _season_tier_mass(
        RW, MP, MR, np.ascontiguousarray(probs, dtype=np.float64),
        np.ascontiguousarray(f_rows, dtype=np.int64), np.ascontiguousarray(f_rpm, dtype=np.float64), np.ascontiguousarray(f_tab, dtype=np.float64),
        np.ascontiguousarray(cut_pos, dtype=np.int64),
        np.ascontiguousarray(c_rows, dtype=np.int64), np.ascontiguousarray(c_rpm, dtype=np.float64), np.ascontiguousarray(c_tab, dtype=np.float64),
        int(block), mass_b,
    )
    return mass_b.sum(axis=0)
