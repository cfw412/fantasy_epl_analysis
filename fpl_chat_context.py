#!/usr/bin/env python3
"""Build a small, chat-sized summary from fpl_data.json + data/gw_review.json.

Both inputs already exist on disk (written by fpl_pull.py and
fpl_review_pull.py) -- this script makes no network calls, it just trims
them down to what a gameweek-strategy chat actually needs: your squad,
the current gameweek schedule, team fixture runs, a shortlist of transfer
candidates per position (instead of all ~700 players), and a condensed
review of the last finished gameweek. fpl_data.json alone is ~1MB, too
large to fetch and re-read on every chat turn.
"""
import json
import sys
from pathlib import Path

DATA_IN = Path("fpl_data.json")
REVIEW_IN = Path("data/gw_review.json")
OUT = Path("data/fpl_chat_context.json")

CANDIDATES_PER_POSITION = 15
MIN_MINUTES_FOR_CANDIDACY = 90  # filter out players with ~no game time this season
XGI_PRIOR_PER90 = 0.15  # league-average-ish attacking rate, for shrinkage below
XGI_SHRINKAGE_90S = 3  # blend in ~3 matches of the prior to damp small samples

PLAYER_FIELDS = [
    "id", "web_name", "team", "position", "price_m", "status",
    "chance_of_playing_next_round", "news", "form", "total_points",
    "points_per_game", "selected_by_percent", "expected_goal_involvements",
    "avg_fixture_difficulty_next_n", "upcoming_fixtures", "minutes",
    "cost_change_event",
]
SQUAD_EXTRA_FIELDS = ["is_captain", "is_vice_captain", "multiplier", "squad_position"]

PICK_FIELDS = [
    "name", "pos", "starter", "captain", "vice_captain", "multiplier",
    "points_counted",
]
PICK_STAT_FIELDS = ["minutes", "goals_scored", "assists", "bonus"]

OUTLOOK_SCORE_NOTE = (
    "outlook_score is a forward-looking heuristic, not a verdict: recent form "
    "+ underlying attacking rate (xG+xA per 90, shrunk toward a league-average "
    "rate for small minute samples so one big cameo can't dominate) + a bonus "
    "for an easy upcoming fixture run (avg_fixture_difficulty_next_n). It "
    "exists so a good fixture swing or strong xG/xA can surface a player even "
    "when recent form alone wouldn't. Combine with live news/press conference "
    "research before acting on it."
)


def outlook_score(p):
    """Blend recent form with forward-looking signals (fixture run, underlying
    attacking rate) so transfer targets reflect expected outlook, not just
    current form."""
    form = p.get("form") or 0.0
    minutes = p.get("minutes") or 0
    xgi = p.get("expected_goal_involvements") or 0.0
    nineties = minutes / 90
    # Empirical-Bayes shrinkage: blend in a few 90s of a league-average rate
    # so a small sample (e.g. one explosive 20-minute cameo) can't produce an
    # inflated per-90 rate.
    xgi_rate = (xgi + XGI_PRIOR_PER90 * XGI_SHRINKAGE_90S) / (nineties + XGI_SHRINKAGE_90S)

    avg_fdr = p.get("avg_fixture_difficulty_next_n")
    fixture_bonus = max(0.0, 3.0 - avg_fdr) if avg_fdr is not None else 0.0

    return round(form + xgi_rate * 10 + fixture_bonus * 1.5, 2)


def trim_player(p, extra_fields=()):
    row = {k: p.get(k) for k in PLAYER_FIELDS}
    row["outlook_score"] = outlook_score(p)
    for k in extra_fields:
        row[k] = p.get(k)
    return row


def top_transfer_candidates(all_players, squad_ids, team_trends):
    by_position = {}
    for p in all_players:
        if (
            p["id"] in squad_ids
            or p.get("status") != "a"
            or (p.get("minutes") or 0) < MIN_MINUTES_FOR_CANDIDACY
        ):
            continue
        by_position.setdefault(p["position"], []).append(p)

    candidates = {}
    for position, players in by_position.items():
        players.sort(key=lambda p: (outlook_score(p), p["total_points"]), reverse=True)
        rows = []
        for p in players[:CANDIDATES_PER_POSITION]:
            row = trim_player(p)
            row["fixture_trend"] = team_trends.get(p.get("team"), {}).get("trend", "unknown")
            rows.append(row)
        candidates[position] = rows
    return candidates


FIXTURE_TREND_THRESHOLD = 0.3  # min FDR-point gap (on a 1-5 scale) to call it a trend


def fixture_trend(fixtures):
    """Split a team's upcoming fixtures into near-term (next 2) vs later (GW
    3-5 of the lookahead window) so a schedule swing 2-4 GWs out is visible
    without scanning the full fixture list by hand."""
    if not fixtures:
        return {"near_term_avg_fdr": None, "later_avg_fdr": None, "trend": "unknown"}
    near = fixtures[:2]
    later = fixtures[2:]
    near_avg = round(sum(f["fdr"] for f in near) / len(near), 2)
    later_avg = round(sum(f["fdr"] for f in later) / len(later), 2) if later else None
    if later_avg is None:
        trend = "unknown"
    elif later_avg <= near_avg - FIXTURE_TREND_THRESHOLD:
        trend = "improving"
    elif later_avg >= near_avg + FIXTURE_TREND_THRESHOLD:
        trend = "toughening"
    else:
        trend = "steady"
    return {"near_term_avg_fdr": near_avg, "later_avg_fdr": later_avg, "trend": trend}


def detect_fixture_anomalies(team_fixture_runs):
    """Double/blank gameweeks within the lookahead window, derived from the
    per-team fixture lists already in fpl_data.json -- no extra data needed."""
    doubles, blanks = [], []
    for team in team_fixture_runs:
        gws = [fx["gw"] for fx in team["fixtures"]]
        if not gws:
            continue
        counts = {}
        for gw in gws:
            counts[gw] = counts.get(gw, 0) + 1
        for gw, count in sorted(counts.items()):
            if count >= 2:
                doubles.append({"team": team["team"], "gw": gw, "fixture_count": count})
        for gw in range(min(gws), max(gws) + 1):
            if gw not in counts:
                blanks.append({"team": team["team"], "gw": gw})
    return doubles, blanks


def squad_risk_flags(squad_picks, team_trends):
    """Bundle status/news/fixture-trend into one explicit list for the
    squad, so chat doesn't have to re-scan every pick for risk signals."""
    flags = []
    for p in squad_picks:
        reasons = []
        if p.get("status") != "a":
            reasons.append(f"status={p['status']}")
        if p.get("news"):
            reasons.append(f"news: {p['news']}")
        if p.get("chance_of_playing_next_round") is not None and p["chance_of_playing_next_round"] < 100:
            reasons.append(f"chance_of_playing_next_round={p['chance_of_playing_next_round']}%")
        trend = team_trends.get(p.get("team"), {})
        if trend.get("trend") == "toughening":
            reasons.append(
                f"fixture run toughening (avg FDR {trend['near_term_avg_fdr']} -> {trend['later_avg_fdr']})"
            )
        if reasons:
            flags.append({"name": p["web_name"], "team": p.get("team"), "reasons": reasons})
    return flags


def trim_squad(squad, team_trends):
    if not squad:
        return None
    picks = [trim_player(p, SQUAD_EXTRA_FIELDS) for p in squad.get("picks", [])]
    for pick, raw in zip(picks, squad.get("picks", [])):
        pick["fixture_trend"] = team_trends.get(raw.get("team"), {}).get("trend", "unknown")
    return {
        "gameweek": squad.get("gameweek"),
        "points": squad.get("points"),
        "bank": squad.get("bank"),
        "team_value": squad.get("team_value"),
        "active_chip": squad.get("active_chip"),
        "picks": picks,
    }


def trim_gw_review(review):
    if not review or review.get("gameweek") is None:
        return {"gameweek": None, "note": review.get("note") if review else None}
    row = {
        "gameweek": review["gameweek"],
        "finished": review["finished"],
        "data_checked": review["data_checked"],
        "average_entry_score": review["average_entry_score"],
        "highest_score": review["highest_score"],
        "captain": review.get("captain"),
        "vice_captain": review.get("vice_captain"),
        "points_counted_total": review.get("points_counted_total"),
        "bench_points": review.get("bench_points"),
        "transfer_cost": review.get("transfer_cost"),
        "reconciles_with_entry_history": review.get("reconciles_with_entry_history"),
        "active_chip": review.get("active_chip"),
        "chips_used_season": review.get("chips_used_season"),
        "automatic_subs": review.get("automatic_subs"),
        "transfers": review.get("transfers"),
        "entry_history": review.get("entry_history"),
        "picks": [],
    }
    for p in review.get("picks", []):
        pick_row = {k: p.get(k) for k in PICK_FIELDS}
        stats = p.get("stats", {})
        for k in PICK_STAT_FIELDS:
            pick_row[k] = stats.get(k)
        row["picks"].append(pick_row)
    return row


def write_if_changed(path, data, ts_key="generated_at"):
    """Skip rewriting the file if nothing but the timestamp changed, so the
    workflow's commit step doesn't produce a no-op commit every run."""
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = None
        if existing is not None:
            existing_compare = {k: v for k, v in existing.items() if k != ts_key}
            new_compare = {k: v for k, v in data.items() if k != ts_key}
            if existing_compare == new_compare:
                print(f"No material change to {path}; leaving file as-is.", file=sys.stderr)
                return
    path.write_text(json.dumps(data, indent=2))


def main():
    data = json.loads(DATA_IN.read_text())
    review = json.loads(REVIEW_IN.read_text()) if REVIEW_IN.exists() else None

    team_fixture_runs = data.get("team_fixture_runs_best_to_worst", [])
    team_trends = {t["team"]: fixture_trend(t["fixtures"]) for t in team_fixture_runs}
    for t in team_fixture_runs:
        t["fixture_trend"] = team_trends[t["team"]]
    double_gws, blank_gws = detect_fixture_anomalies(team_fixture_runs)

    squad = trim_squad(data.get("squad"), team_trends)
    squad_ids = {p["id"] for p in squad["picks"]} if squad else set()

    gameweeks = data.get("gameweeks", [])
    next_deadline = next(
        (gw["deadline_time"] for gw in gameweeks if gw["id"] == data.get("next_gameweek")),
        None,
    )

    out = {
        "generated_at": data.get("generated_at"),
        "entry_id": data.get("team_id"),
        "manager_summary": data.get("manager_summary"),
        "current_gameweek": data.get("current_gameweek"),
        "next_gameweek": data.get("next_gameweek"),
        "next_deadline": next_deadline,
        "last_finished_gameweek": data.get("last_finished_gameweek"),
        "gameweeks": gameweeks,
        "squad": squad,
        "squad_risk_flags": squad_risk_flags(squad["picks"], team_trends) if squad else [],
        "free_transfers_note": (
            "Not reliably exposed by the public FPL API -- tell Claude how "
            "many free transfers and how much bank you have each week."
        ),
        "team_fixture_runs_best_to_worst": team_fixture_runs,
        "fixture_alerts": {
            "note": (
                "Double/blank gameweeks within the current lookahead window. "
                "Usually empty well in advance of the season's congested "
                "periods -- check back as the window the data pulls covers "
                "moves closer to any known blank/double round."
            ),
            "double_gameweeks": double_gws,
            "blank_gameweeks": blank_gws,
        },
        "transfer_candidates_note": OUTLOOK_SCORE_NOTE,
        "transfer_candidates_by_position": top_transfer_candidates(
            data.get("all_players", []), squad_ids, team_trends
        ),
        "last_gameweek_review": trim_gw_review(review),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    write_if_changed(OUT, out)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
