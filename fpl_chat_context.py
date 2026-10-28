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

PLAYER_FIELDS = [
    "id", "web_name", "team", "position", "price_m", "status",
    "chance_of_playing_next_round", "news", "form", "total_points",
    "points_per_game", "selected_by_percent", "expected_goal_involvements",
    "avg_fixture_difficulty_next_n", "upcoming_fixtures",
]
SQUAD_EXTRA_FIELDS = ["is_captain", "is_vice_captain", "multiplier", "squad_position"]

PICK_FIELDS = [
    "name", "pos", "starter", "captain", "vice_captain", "multiplier",
    "points_counted",
]
PICK_STAT_FIELDS = ["minutes", "goals_scored", "assists", "bonus"]


def trim_player(p, extra_fields=()):
    row = {k: p.get(k) for k in PLAYER_FIELDS}
    for k in extra_fields:
        row[k] = p.get(k)
    return row


def top_transfer_candidates(all_players, squad_ids):
    by_position = {}
    for p in all_players:
        if p["id"] in squad_ids or p.get("status") != "a":
            continue
        by_position.setdefault(p["position"], []).append(p)

    candidates = {}
    for position, players in by_position.items():
        players.sort(key=lambda p: (p["form"], p["total_points"]), reverse=True)
        candidates[position] = [
            trim_player(p) for p in players[:CANDIDATES_PER_POSITION]
        ]
    return candidates


def trim_squad(squad):
    if not squad:
        return None
    return {
        "gameweek": squad.get("gameweek"),
        "points": squad.get("points"),
        "bank": squad.get("bank"),
        "team_value": squad.get("team_value"),
        "active_chip": squad.get("active_chip"),
        "picks": [
            trim_player(p, SQUAD_EXTRA_FIELDS) for p in squad.get("picks", [])
        ],
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

    squad = trim_squad(data.get("squad"))
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
        "free_transfers_note": (
            "Not reliably exposed by the public FPL API -- tell Claude how "
            "many free transfers and how much bank you have each week."
        ),
        "team_fixture_runs_best_to_worst": data.get("team_fixture_runs_best_to_worst"),
        "transfer_candidates_by_position": top_transfer_candidates(
            data.get("all_players", []), squad_ids
        ),
        "last_gameweek_review": trim_gw_review(review),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    write_if_changed(OUT, out)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
