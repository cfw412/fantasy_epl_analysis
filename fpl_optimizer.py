#!/usr/bin/env python3
"""Transfer planner: expected points per gameweek -> best transfer plan.

Stdlib only, no network. Called by fpl_chat_context.py; returns a compact
`transfer_plan` dict that is merged into data/fpl_chat_context.json.

Method (deliberately lightweight -- inputs are noisy, so no solver):
  1. Expected points per player per gameweek = P(playing) x (shrunk points/90
     blended with a shrunk xG/xA/clean-sheet/bonus component model) x a mild
     per-fixture difficulty factor. Double gameweeks add, blanks give 0.
  2. A squad's value in a gameweek = its best legal XI's expected points.
  3. Enumerate every legal single swap (same position, affordable using
     SELLING price, max 3 per club) and keep a diverse top set.
  4. Search two-gameweek plans over those moves: 0/1/2 moves at the planning
     gameweek, 0/1/2 moves the gameweek after, free-transfer banking and -4
     hits included. Value = expected XI points over the horizon vs doing nothing.
  5. Re-run under several model variants; only call the leading plan robust if
     most variants agree on this gameweek's action.

Known limits: no rotation/press-conference news, penalty or set-piece duties,
defensive-contribution points, or manager changes. Treat output as one input
alongside the news research, not a verdict.
"""
import itertools

GOAL_POINTS = {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}
CS_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
ASSIST_POINTS = 3
APPEARANCE_POINTS = 1.8      # ~2 for 60+ minutes, shaded for subs
SHRINK_90S = 3               # ~3 matches of the positional prior
MIN_MINUTES_PRIOR = 270      # players used to build the positional priors
MIN_MINUTES_CANDIDATE = 180  # filter out near-zero-minute transfer targets
HORIZON_GWS = 5
TOP_TEMPLATES = 12           # single moves carried into plan search
MAX_PER_IN, MAX_PER_OUT = 2, 3   # keep the template pool diverse
HIT_COST = 4
MIN_GAIN_OVER_BANK = 1.5     # expected points a move must add vs banking
MIN_EXTRA_FOR_HIT = 2.0      # net (after the -4) margin a hit plan needs over the best no-hit plan
ROBUST_MIN_VOTES = 4         # of the 6 variants must agree on this GW's action

BASE_VARIANT = {"slope": 0.04, "blend": 0.5}
VARIANTS = [
    {"slope": s, "blend": b} for s in (0.0, 0.04, 0.10) for b in (0.3, 0.7)
]
FORMATIONS = [
    (d, m, f)
    for d in (3, 4, 5) for m in (2, 3, 4, 5) for f in (1, 2, 3)
    if d + m + f == 10
]

CAVEATS = [
    "Model uses season rates + status flags only: no rotation or press-conference news, penalties/set pieces, defensive contribution points or manager changes.",
    "Fixture difficulty carries a mild weight because it was only weakly predictive in backtests.",
    "Expected points are averages; hauls (single big games) are variance and are not predicted. Weekly point rank correlation is only ~0.3 for any model.",
    "Confirm the free-transfer count in the app before recommending a hit.",
]


def selling_price_tenths(buy, now):
    """Half of any rise since purchase (rounded down to 0.1m); falls in full."""
    return buy + (now - buy) // 2 if now > buy else now


def _tenths(x):
    return int(round(x * 10))


# --------------------------------------------------------------------------
# Expected points
# --------------------------------------------------------------------------
def _priors(players):
    keys = ("expected_goals", "expected_assists", "clean_sheets", "bonus", "total_points")
    priors = {}
    for pos in GOAL_POINTS:
        ps = [p for p in players if p["position"] == pos and (p.get("minutes") or 0) >= MIN_MINUTES_PRIOR]
        n90 = sum(p["minutes"] for p in ps) / 90 or 1.0
        priors[pos] = {k: sum((p.get(k) or 0) for p in ps) / n90 for k in keys}
    return priors


def expected_points(players, played_gws, slope, blend):
    """-> {player_id: {gw: expected_points}} for the gameweeks in upcoming_fixtures."""
    priors = _priors(players)
    played = max(1, played_gws)
    out = {}
    for p in players:
        n90 = max(p.get("minutes") or 0, 1) / 90
        pos = p["position"]
        pr = priors[pos]

        def shrunk(key):
            return ((p.get(key) or 0) + pr[key] * SHRINK_90S) / (n90 + SHRINK_90S)

        p_start = min(1.0, (p.get("minutes") or 0) / (90 * played))
        chance = p.get("chance_of_playing_next_round")
        if p.get("status") in ("i", "s", "u"):
            p_start = 0.0
        elif chance is not None:
            p_start *= chance / 100

        component = (
            shrunk("expected_goals") * GOAL_POINTS[pos]
            + shrunk("expected_assists") * ASSIST_POINTS
            + shrunk("clean_sheets") * CS_POINTS[pos]
            + shrunk("bonus")
            + APPEARANCE_POINTS
        )
        rate = blend * shrunk("total_points") + (1 - blend) * component
        by_gw = {}
        for fx in p.get("upcoming_fixtures") or []:
            by_gw[fx["gw"]] = by_gw.get(fx["gw"], 0.0) + p_start * rate * (1 + slope * (3 - fx["fdr"]))
        out[p["id"]] = by_gw
    return out


class Model:
    """Caches best-XI values for a squad (set of ids) in a gameweek."""

    def __init__(self, players, played_gws, variant):
        self.E = expected_points(players, played_gws, variant["slope"], variant["blend"])
        self.pos = {p["id"]: p["position"] for p in players}
        self._cache = {}

    def xi(self, squad, gw):
        key = (squad, gw)
        if key in self._cache:
            return self._cache[key]
        by = {k: [] for k in GOAL_POINTS}
        for i in squad:
            by[self.pos[i]].append(self.E[i].get(gw, 0.0))
        for v in by.values():
            v.sort(reverse=True)
        best = max(
            sum(by["DEF"][:d]) + sum(by["MID"][:m]) + sum(by["FWD"][:f])
            for d, m, f in FORMATIONS
        )
        val = (by["GKP"][0] if by["GKP"] else 0.0) + best
        self._cache[key] = val
        return val

    def total(self, squad, gws):
        return sum(self.xi(squad, g) for g in gws)


# --------------------------------------------------------------------------
# Squad state + legal moves
# --------------------------------------------------------------------------
class State:
    __slots__ = ("ids", "bank", "sell")

    def __init__(self, ids, bank, sell):
        self.ids, self.bank, self.sell = frozenset(ids), bank, sell


def can_swap(state, out, inn, P):
    if out not in state.ids or inn in state.ids:
        return False
    if P[out]["position"] != P[inn]["position"]:
        return False
    if P[inn]["price10"] > state.bank + state.sell[out]:
        return False
    club = P[inn]["team"]
    n = sum(1 for i in state.ids if P[i]["team"] == club) - (1 if P[out]["team"] == club else 0)
    return n < 3


def apply_swap(state, out, inn, P):
    sell = dict(state.sell)
    price = P[inn]["price10"]
    bank = state.bank + sell.pop(out) - price
    sell[inn] = price
    return State((state.ids - {out}) | {inn}, bank, sell)


def try_apply(state, moves, P):
    """Apply a tuple of (out, in) swaps in order; None if any is illegal."""
    for out, inn in moves:
        if not can_swap(state, out, inn, P):
            return None
        state = apply_swap(state, out, inn, P)
    return state


# --------------------------------------------------------------------------
# Plan search
# --------------------------------------------------------------------------
def single_moves(model, state, P, gws, candidates):
    base = model.total(state.ids, gws)
    base_n = model.xi(state.ids, gws[0])
    rows = []
    for out in state.ids:
        for inn in candidates:
            if not can_swap(state, out, inn, P):
                continue
            new = apply_swap(state, out, inn, P)
            rows.append({
                "out": out, "in": inn,
                "gain": model.total(new.ids, gws) - base,
                "gain_now": model.xi(new.ids, gws[0]) - base_n,
            })
    rows.sort(key=lambda r: (-r["gain"], r["out"], r["in"]))
    return rows


def pick_templates(rows):
    picked, per_in, per_out = [], {}, {}
    for r in rows:
        if r["gain"] <= 0:
            break
        if per_in.get(r["in"], 0) >= MAX_PER_IN or per_out.get(r["out"], 0) >= MAX_PER_OUT:
            continue
        picked.append(r)
        per_in[r["in"]] = per_in.get(r["in"], 0) + 1
        per_out[r["out"]] = per_out.get(r["out"], 0) + 1
        if len(picked) >= TOP_TEMPLATES:
            break
    return picked


def _actions(templates):
    acts = [()]
    acts += [((t["out"], t["in"]),) for t in templates]
    for a, b in itertools.combinations(templates, 2):
        if a["out"] != b["out"] and a["in"] != b["in"]:
            acts.append(((a["out"], a["in"]), (b["out"], b["in"])))
    return acts


def search_plans(model, state, P, gws, templates, ft_now, cap):
    """All plans (moves at gws[0], moves at gws[1]) -> list of dicts with net gain."""
    baseline = model.total(state.ids, gws)
    acts = _actions(templates)
    plans = []
    for a_now in acts:
        s_now = try_apply(state, a_now, P)
        if s_now is None:
            continue
        hits_now = max(0, len(a_now) - ft_now)
        ft_after = min(cap, ft_now - min(len(a_now), ft_now) + 1)
        if len(gws) < 2:
            net = model.total(s_now.ids, gws) - baseline - HIT_COST * hits_now
            plans.append({"now": a_now, "next": (), "hits": hits_now, "net": net})
            continue
        v_now = model.xi(s_now.ids, gws[0])
        for a_next in acts:
            s_next = try_apply(s_now, a_next, P)
            if s_next is None:
                continue
            hits_next = max(0, len(a_next) - ft_after)
            value = v_now + model.total(s_next.ids, gws[1:])
            hits = hits_now + hits_next
            plans.append({
                "now": a_now, "next": a_next, "hits": hits,
                "net": value - baseline - HIT_COST * hits,
            })
    plans.sort(key=lambda p: (-p["net"], len(p["now"]) + len(p["next"]), p["now"], p["next"]))
    return plans


def choose(plans):
    """-> (recommended_plan, bank_plan). Hit plans need a margin over the best no-hit plan;
    a move now needs a margin over banking."""
    best = plans[0]
    best_nohit = next(p for p in plans if p["hits"] == 0)
    rec = best if best["hits"] == 0 or best["net"] >= best_nohit["net"] + MIN_EXTRA_FOR_HIT else best_nohit
    bank = next(p for p in plans if not p["now"])
    if rec["now"] and rec["net"] - bank["net"] < MIN_GAIN_OVER_BANK:
        rec = bank
    return rec, bank


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def roll_free_transfers(ft_enter, first_event, target_event, made_by_event, cap):
    """Free transfers available at the start of `target_event`, before any moves
    already made in that gameweek (those are returned separately)."""
    ft = ft_enter
    for e in range(first_event, target_event):
        used = min(made_by_event.get(e, 0), ft)
        ft = min(cap, ft - used + 1)
    return ft


def build_transfer_plan(data, free_transfers_enter):
    """data = fpl_data.json dict. free_transfers_enter = derived free transfers at the
    start of last_finished_gameweek + 1. Returns the transfer_plan dict."""
    players = data["all_players"]
    P = {p["id"]: dict(p, price10=_tenths(p["price_m"])) for p in players}
    squad = data.get("squad")
    if not squad or not squad.get("picks"):
        raise ValueError("no squad in data")

    last_finished = data.get("last_finished_gameweek") or 0
    plan_gw = data["next_gameweek"]
    cap = 1 + (data.get("max_extra_free_transfers") if data.get("max_extra_free_transfers") is not None else 4)

    ids, sell = [], {}
    for pk in squad["picks"]:
        ids.append(pk["id"])
        sp = pk.get("selling_price_m")
        sell[pk["id"]] = _tenths(sp if sp is not None else pk["price_m"])
    bank = _tenths(squad["bank"])

    # Apply transfers already made since the squad snapshot (e.g. a move for a
    # gameweek whose deadline has passed or an early move for the next one).
    pending = sorted(data.get("pending_transfers") or [], key=lambda t: (t["event"], t["time"]))
    applied, made_by_event = [], {}
    for t in pending:
        out, inn = t["element_out"], t["element_in"]
        if out not in ids or inn in ids or inn not in P:
            continue
        ids = [i for i in ids if i != out] + [inn]
        bank += t["element_out_cost"] - t["element_in_cost"]
        sell.pop(out, None)
        sell[inn] = selling_price_tenths(t["element_in_cost"], P[inn]["price10"])
        made_by_event[t["event"]] = made_by_event.get(t["event"], 0) + 1
        applied.append({"gw": t["event"], "out": P[out]["web_name"], "in": P[inn]["web_name"]})
    state = State(ids, bank, sell)

    ft_at_plan_gw = roll_free_transfers(free_transfers_enter, last_finished + 1, plan_gw, made_by_event, cap)
    made_now = made_by_event.get(plan_gw, 0)
    ft_now = max(0, ft_at_plan_gw - made_now)

    avail = {g for p in players for g in (f["gw"] for f in p.get("upcoming_fixtures") or [])}
    gws = [g for g in range(plan_gw, plan_gw + HORIZON_GWS) if g in avail]
    if not gws:
        raise ValueError("no fixture data for the planning horizon")
    played = last_finished
    # Don't suggest buying back a player just sold in a pending transfer (churn).
    just_sold = {t["element_out"] for t in pending}
    candidates = [
        p["id"] for p in players
        if (p.get("minutes") or 0) >= MIN_MINUTES_CANDIDATE
        and p.get("status") not in ("i", "s", "u")
        and p["id"] not in just_sold
    ]

    def run(variant):
        model = Model(players, played, variant)
        rows = single_moves(model, state, P, gws, candidates)
        templates = pick_templates(rows)
        plans = search_plans(model, state, P, gws, templates, ft_now, cap)
        return model, rows, templates, plans

    model, rows, templates, plans = run(BASE_VARIANT)
    rec, bank_plan = choose(plans)
    best_nohit = next(p for p in plans if p["hits"] == 0)

    # Robustness: does the rest of the variants agree on this gameweek's action?
    votes = 0
    for v in VARIANTS:
        _, _, _, vplans = run(v)
        vrec, _ = choose(vplans)
        if set(vrec["now"]) == set(rec["now"]):
            votes += 1
    robust = votes >= ROBUST_MIN_VOTES

    def name_move(m):
        out, inn = m
        return {
            "out": P[out]["web_name"], "in": P[inn]["web_name"], "pos": P[inn]["position"],
            "price_in_m": P[inn]["price10"] / 10, "sell_out_m": state.sell[out] / 10,
        }

    def fmt_plan(p, extra=None):
        d = {
            "this_gw": [name_move(m) for m in p["now"]],
            "next_gw": [_name_next(m) for m in p["next"]],
            "hits": p["hits"],
            "net_gain": round(p["net"], 2),
        }
        if extra:
            d.update(extra)
        return d

    def _name_next(m):  # next-gw moves are described by name only (prices depend on the first move)
        return {"out": P[m[0]]["web_name"], "in": P[m[1]]["web_name"], "pos": P[m[1]]["position"]}

    top_singles = [
        {**name_move((r["out"], r["in"])), "gain_horizon": round(r["gain"], 2), "gain_this_gw": round(r["gain_now"], 2)}
        for r in rows[:8]
    ]

    # Ordering effect for the best plans that make one move each week.
    ordering = []
    for p in plans:
        if len(ordering) >= 3:
            break
        if len(p["now"]) == 1 and len(p["next"]) == 1 and p["hits"] == 0 and len(gws) >= 2:
            rev = _reverse_net(model, state, P, gws, p)
            if rev is not None:
                a, b = p["now"][0], p["next"][0]
                ordering.append({
                    "first": P[a[1]]["web_name"] + " (for " + P[a[0]]["web_name"] + ")",
                    "second": P[b[1]]["web_name"] + " (for " + P[b[0]]["web_name"] + ")",
                    "net_in_this_order": round(p["net"], 2),
                    "net_if_reversed": round(rev, 2),
                    "better_order": "as listed" if p["net"] >= rev else "reversed",
                })

    if rec["now"]:
        hint = "move" if robust else "bank"
        reason = (
            f"Best plan beats banking by {rec['net'] - bank_plan['net']:.1f} expected points over GW{gws[0]}-{gws[-1]}; "
            + (f"{votes}/{len(VARIANTS)} model variants agree." if robust
               else f"only {votes}/{len(VARIANTS)} model variants agree on this gameweek's move, so default to banking unless news supports it.")
        )
    else:
        hint = "bank"
        best_move = next((p for p in plans if p["now"] and p["hits"] == 0), None)
        gap = (bank_plan["net"] - best_move["net"]) if best_move else 0
        reason = (
            "No move this gameweek clears the thresholds (>= "
            f"{MIN_GAIN_OVER_BANK} expected points over banking"
            + (f"; best single move is {gap:.1f} points worse than banking" if best_move and gap > 0 else "")
            + ")."
        )

    return {
        "note": (
            "Expected-points optimizer. Gains are expected XI points over the horizon relative to making no "
            "transfers (hit costs included). Compare with news before acting. See caveats."
        ),
        "planning_gameweek": plan_gw,
        "horizon_gameweeks": gws,
        "free_transfers_at_planning_gw": ft_at_plan_gw,
        "free_transfers_left_after_pending": ft_now,
        "pending_transfers_applied": applied,
        "bank_m_after_pending": state.bank / 10,
        "squad_expected_xi_points_this_gw": round(model.xi(state.ids, gws[0]), 1),
        "recommendation_hint": hint,
        "hint_reason": reason,
        "robust_votes": f"{votes}/{len(VARIANTS)}",
        "recommended_plan": fmt_plan(rec),
        "best_plan_if_banking_now": fmt_plan(bank_plan),
        "top_plans": [fmt_plan(p) for p in _dedupe(plans)[:5]],
        "top_single_moves": top_singles,
        "ordering_effects": ordering,
        "caveats": CAVEATS,
    }


def _dedupe(plans):
    seen, out = set(), []
    for p in plans:
        key = (frozenset(p["now"]), frozenset(p["next"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _reverse_net(model, state, P, gws, plan):
    a, b = plan["now"][0], plan["next"][0]
    s1 = try_apply(state, (b,), P)
    if s1 is None:
        return None
    s2 = try_apply(s1, (a,), P)
    if s2 is None:
        return None
    baseline = model.total(state.ids, gws)
    value = model.xi(s1.ids, gws[0]) + model.total(s2.ids, gws[1:])
    return value - baseline
