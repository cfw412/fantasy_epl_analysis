#!/usr/bin/env python3
"""Pull post-gameweek review data for one FPL entry -> data/gw_review.json.

Stdlib only. Uses the public FPL API (no login needed for these endpoints).
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENTRY_ID = 4455838  # British Diving Team
BASE = "https://fantasy.premierleague.com/api"
OUT = Path("data/gw_review.json")
POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def get(path):
    req = urllib.request.Request(
        f"{BASE}/{path}", headers={"User-Agent": "Mozilla/5.0 (fpl-review-pull)"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def main():
    boot = get("bootstrap-static/")
    players = {p["id"]: p for p in boot["elements"]}
    teams = {t["id"]: t["name"] for t in boot["teams"]}
    finished = [e for e in boot["events"] if e["finished"]]

    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "entry_id": ENTRY_ID}
    if not finished:
        out["gameweek"] = None
        out["note"] = "No finished gameweek yet."
    else:
        ev = max(finished, key=lambda e: e["id"])
        gw = ev["id"]
        live = {x["id"]: x["stats"] for x in get(f"event/{gw}/live/")["elements"]}
        picks = get(f"entry/{ENTRY_ID}/event/{gw}/picks/")
        history = get(f"entry/{ENTRY_ID}/history/")
        transfers = [t for t in get(f"entry/{ENTRY_ID}/transfers/") if t["event"] == gw]

        def name(pid):
            return players[pid]["web_name"]

        pick_rows = [
            {
                "name": name(p["element"]),
                "team": teams[players[p["element"]]["team"]],
                "pos": POS[players[p["element"]]["element_type"]],
                "squad_position": p["position"],
                "starter": p["position"] <= 11,
                "multiplier": p["multiplier"],
                "captain": p["is_captain"],
                "vice_captain": p["is_vice_captain"],
                "stats": live.get(p["element"], {}),
                "points_counted": live.get(p["element"], {}).get("total_points", 0)
                * p["multiplier"],
            }
            for p in picks["picks"]
        ]

        points_counted_total = sum(p["points_counted"] for p in pick_rows)
        captain_row = next((p for p in pick_rows if p["captain"]), None)
        vice_captain_row = next((p for p in pick_rows if p["vice_captain"]), None)
        # entry_history.points is the gross score (before any transfer-hit
        # deduction); the hit itself is tracked separately in
        # event_transfers_cost and is not subtracted from "points".
        entry_history_points = picks["entry_history"]["points"]

        out.update(
            {
                "gameweek": gw,
                "finished": ev["finished"],
                "data_checked": ev["data_checked"],
                "average_entry_score": ev["average_entry_score"],
                "highest_score": ev["highest_score"],
                "entry_history": picks["entry_history"],
                "active_chip": picks["active_chip"],
                "chips_used_season": history["chips"],
                "captain": captain_row["name"] if captain_row else None,
                "vice_captain": vice_captain_row["name"] if vice_captain_row else None,
                "bench_points": picks["entry_history"].get("points_on_bench"),
                "points_counted_total": points_counted_total,
                "transfer_cost": picks["entry_history"].get("event_transfers_cost", 0),
                "reconciles_with_entry_history": points_counted_total == entry_history_points,
                "automatic_subs": [
                    {"in": name(s["element_in"]), "out": name(s["element_out"])}
                    for s in picks["automatic_subs"]
                ],
                "transfers": [
                    {
                        "in": name(t["element_in"]),
                        "out": name(t["element_out"]),
                        "in_cost": t["element_in_cost"],
                        "out_cost": t["element_out_cost"],
                    }
                    for t in transfers
                ],
                "picks": pick_rows,
                "season_history": history["current"],
            }
        )

        if points_counted_total != entry_history_points:
            print(
                f"WARNING: computed points ({points_counted_total}) do not match "
                f"entry_history.points ({entry_history_points}) for GW{gw}",
                file=sys.stderr,
            )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))
    print(f"Wrote {OUT} (gameweek={out.get('gameweek')})")


if __name__ == "__main__":
    main()