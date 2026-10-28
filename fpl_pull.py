#!/usr/bin/env python3
"""
FPL Data Pull Tool
-------------------
Pulls live data from the official Fantasy Premier League API and produces
a single clean JSON file summarizing:
  - All player metrics (price, form, total points, ownership, injury status)
  - Each player's next N fixtures with difficulty rating (FDR) and home/away
  - Your current squad (picks, bank, free transfers) for a given entry ID

Run this locally (needs internet access to fantasy.premierleague.com), then
upload the resulting JSON file to Claude for analysis.

Usage:
    python fpl_pull.py --team-id 4455838 --lookahead 5 --out fpl_data.json

No login/auth required — all endpoints used here are public.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://fantasy.premierleague.com/api"


def write_if_changed(path, data, ts_key="generated_at"):
    """Skip rewriting the file if nothing but the timestamp changed, so the
    workflow's commit step doesn't produce a no-op commit every run."""
    path = Path(path)
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


def get(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_bootstrap():
    return get(f"{BASE}/bootstrap-static/")


def fetch_fixtures():
    return get(f"{BASE}/fixtures/")


def fetch_entry(team_id):
    return get(f"{BASE}/entry/{team_id}/")


def fetch_entry_history(team_id):
    return get(f"{BASE}/entry/{team_id}/history/")


def latest_finished_event(events):
    finished = [e for e in events if e.get("finished") or e.get("data_checked")]
    if not finished:
        return events[0]["id"] if events else 1
    return max(finished, key=lambda e: e["id"])["id"]


def current_or_next_event(events):
    for e in events:
        if e.get("is_current"):
            return e["id"]
    for e in events:
        if e.get("is_next"):
            return e["id"]
    return latest_finished_event(events)


def current_event_id(events):
    for e in events:
        if e.get("is_current"):
            return e["id"]
    return None


def next_event_id(events):
    for e in events:
        if e.get("is_next"):
            return e["id"]
    return None


def summarize_gameweeks(events):
    """Per-gameweek deadline/finished/data_checked status, so a downstream
    report can tell whether a pre- or post-gameweek write-up is due."""
    return [
        {
            "id": e["id"],
            "deadline_time": e.get("deadline_time"),
            "finished": e.get("finished", False),
            "data_checked": e.get("data_checked", False),
            "is_current": e.get("is_current", False),
            "is_next": e.get("is_next", False),
            "average_entry_score": e.get("average_entry_score"),
            "highest_score": e.get("highest_score"),
        }
        for e in events
    ]


def fetch_picks(team_id, event_id):
    try:
        return get(f"{BASE}/entry/{team_id}/event/{event_id}/picks/")
    except requests.HTTPError:
        return None


def build_team_lookup(teams):
    return {t["id"]: t for t in teams}


def build_upcoming_fixtures_by_team(fixtures, lookahead):
    """Return {team_id: [ {event, opponent_id, is_home, difficulty}, ... ]}"""
    upcoming = {}
    future = [f for f in fixtures if not f.get("finished") and f.get("event")]
    future.sort(key=lambda f: (f["event"], f.get("kickoff_time") or ""))

    for f in future:
        for side, opp_side, home_flag in [
            ("team_h", "team_a", True),
            ("team_a", "team_h", False),
        ]:
            team_id = f[side]
            opp_id = f[opp_side]
            difficulty = f["team_h_difficulty"] if home_flag else f["team_a_difficulty"]
            upcoming.setdefault(team_id, [])
            if len(upcoming[team_id]) < lookahead:
                upcoming[team_id].append({
                    "event": f["event"],
                    "opponent_team_id": opp_id,
                    "is_home": home_flag,
                    "difficulty": difficulty,
                })
    return upcoming


def summarize_team_fixture_runs(upcoming_by_team, team_lookup):
    """Team-level view: which clubs have the easiest/hardest upcoming runs.
    Useful for spotting players worth targeting a few gameweeks ahead of a
    favorable fixture swing, not just the immediate week."""
    runs = []
    for team_id, fixtures in upcoming_by_team.items():
        if not fixtures:
            continue
        avg_fdr = sum(fx["difficulty"] for fx in fixtures) / len(fixtures)
        runs.append({
            "team": team_lookup[team_id]["short_name"],
            "team_id": team_id,
            "avg_fdr_next_n": round(avg_fdr, 2),
            "fixtures": [
                {
                    "gw": fx["event"],
                    "opponent": team_lookup[fx["opponent_team_id"]]["short_name"],
                    "venue": "H" if fx["is_home"] else "A",
                    "fdr": fx["difficulty"],
                }
                for fx in fixtures
            ],
        })
    runs.sort(key=lambda r: r["avg_fdr_next_n"])
    return runs


def summarize_players(bootstrap, upcoming_by_team, team_lookup):
    positions = {p["id"]: p["singular_name_short"] for p in bootstrap["element_types"]}
    players = []
    for el in bootstrap["elements"]:
        team_id = el["team"]
        fixtures = upcoming_by_team.get(team_id, [])
        fixture_summary = [
            {
                "gw": fx["event"],
                "opponent": team_lookup[fx["opponent_team_id"]]["short_name"],
                "venue": "H" if fx["is_home"] else "A",
                "fdr": fx["difficulty"],
            }
            for fx in fixtures
        ]
        avg_fdr = (
            round(sum(fx["difficulty"] for fx in fixtures) / len(fixtures), 2)
            if fixtures else None
        )
        players.append({
            "id": el["id"],
            "name": f"{el['first_name']} {el['second_name']}".strip(),
            "web_name": el["web_name"],
            "team": team_lookup[team_id]["short_name"],
            "position": positions.get(el["element_type"], "?"),
            "price_m": el["now_cost"] / 10,
            "selected_by_percent": float(el["selected_by_percent"]),
            "total_points": el["total_points"],
            "points_per_game": float(el["points_per_game"]),
            "form": float(el["form"]),
            "minutes": el["minutes"],
            "goals_scored": el["goals_scored"],
            "assists": el["assists"],
            "clean_sheets": el["clean_sheets"],
            "expected_goals": float(el.get("expected_goals", 0) or 0),
            "expected_assists": float(el.get("expected_assists", 0) or 0),
            "expected_goal_involvements": float(el.get("expected_goal_involvements", 0) or 0),
            "bonus": el["bonus"],
            "bps": el["bps"],
            "ict_index": float(el["ict_index"]),
            "status": el["status"],  # a=available, d=doubtful, i=injured, s=suspended, u=unavailable
            "chance_of_playing_next_round": el.get("chance_of_playing_next_round"),
            "news": el.get("news", ""),
            "news_added": el.get("news_added"),
            "transfers_in_event": el.get("transfers_in_event"),
            "transfers_out_event": el.get("transfers_out_event"),
            "cost_change_event": el.get("cost_change_event"),
            "upcoming_fixtures": fixture_summary,
            "avg_fixture_difficulty_next_n": avg_fdr,
        })
    return players


def summarize_squad(picks_data, players_by_id, bootstrap):
    if not picks_data:
        return None
    entry_history = picks_data.get("entry_history", {})
    picks = picks_data.get("picks", [])
    squad = []
    for pk in picks:
        p = players_by_id.get(pk["element"])
        if not p:
            continue
        squad.append({
            **p,
            "is_captain": pk.get("is_captain", False),
            "is_vice_captain": pk.get("is_vice_captain", False),
            "multiplier": pk.get("multiplier"),
            "squad_position": pk.get("position"),
        })
    return {
        "gameweek": entry_history.get("event"),
        "points": entry_history.get("points"),
        "bank": entry_history.get("bank", 0) / 10,
        "team_value": entry_history.get("value", 0) / 10,
        "free_transfers": None,  # not directly in this endpoint; see note below
        "picks": squad,
        "active_chip": picks_data.get("active_chip"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team-id", type=int, default=4455838, help="Your FPL entry/team ID")
    ap.add_argument("--lookahead", type=int, default=5, help="Number of future fixtures to include per player")
    ap.add_argument("--out", type=str, default="fpl_data.json")
    args = ap.parse_args()

    print("Fetching bootstrap data (players, teams, gameweeks)...", file=sys.stderr)
    bootstrap = fetch_bootstrap()
    teams = bootstrap["teams"]
    events = bootstrap["events"]
    team_lookup = build_team_lookup(teams)

    print("Fetching fixtures...", file=sys.stderr)
    fixtures = fetch_fixtures()
    upcoming_by_team = build_upcoming_fixtures_by_team(fixtures, args.lookahead)

    print("Summarizing player metrics + upcoming schedules...", file=sys.stderr)
    players = summarize_players(bootstrap, upcoming_by_team, team_lookup)
    players_by_id = {p["id"]: p for p in players}

    print("Ranking teams by upcoming fixture difficulty...", file=sys.stderr)
    team_fixture_runs = summarize_team_fixture_runs(upcoming_by_team, team_lookup)

    print(f"Fetching your entry ({args.team_id})...", file=sys.stderr)
    entry_event = current_or_next_event(events)
    last_finished = latest_finished_event(events)
    entry_info = None
    picks_summary = None
    try:
        entry_info = fetch_entry(args.team_id)
        picks_data = fetch_picks(args.team_id, last_finished)
        picks_summary = summarize_squad(picks_data, players_by_id, bootstrap)
    except requests.HTTPError as e:
        print(f"Warning: could not fetch entry {args.team_id}: {e}", file=sys.stderr)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current_or_next_gameweek": entry_event,
        "current_gameweek": current_event_id(events),
        "next_gameweek": next_event_id(events),
        "last_finished_gameweek": last_finished,
        "gameweeks": summarize_gameweeks(events),
        "team_id": args.team_id,
        "manager_summary": {
            "name": f"{entry_info.get('player_first_name','')} {entry_info.get('player_last_name','')}".strip()
                    if entry_info else None,
            "team_name": entry_info.get("name") if entry_info else None,
            "overall_points": entry_info.get("summary_overall_points") if entry_info else None,
            "overall_rank": entry_info.get("summary_overall_rank") if entry_info else None,
        } if entry_info else None,
        "squad": picks_summary,
        "team_fixture_runs_best_to_worst": team_fixture_runs,
        "all_players": players,
    }

    write_if_changed(args.out, output)

    print(f"\nDone. Wrote {len(players)} players to {args.out}", file=sys.stderr)
    if picks_summary:
        print(f"Squad pulled for GW{picks_summary['gameweek']} — {len(picks_summary['picks'])} players, "
              f"bank £{picks_summary['bank']}m", file=sys.stderr)
    print("\nNOTE: 'free_transfers' isn't exposed on the public picks endpoint reliably — "
          "just tell Claude how many free transfers you have each week.", file=sys.stderr)
    print("Upload this JSON file to Claude for analysis.", file=sys.stderr)


if __name__ == "__main__":
    main()
