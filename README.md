# FPL Weekly Data Pull

## One-time setup
```
pip install requests
```

## Each week
```
python fpl_pull.py --team-id 4455838 --lookahead 5 --out fpl_data.json
```

This pulls, in one shot:
- Every player's price, form, total points, points-per-game, ownership %,
  underlying stats (xG, xA, xGI, bonus, BPS, ICT)
- Injury/availability status (`status`, `chance_of_playing_next_round`, `news`)
  straight from the FPL API — no need to separately track injury news
- Each player's next 5 fixtures with opponent and difficulty rating (FDR),
  home/away
- Your current squad, bank balance, team value, and gameweek points

Then upload `fpl_data.json` to Claude. Also tell Claude:
- How many free transfers you currently have (not reliably exposed by the API)
- Any chips available/used (wildcard, bench boost, triple captain, free hit)

## Notes
- No login required — all endpoints are public.
- `--lookahead` controls how many future fixtures are pulled per player
  (5-6 is a good default for spotting favorable runs a few weeks out).
- Re-run any time; it always pulls fresh live data.
- Output now also includes `team_fixture_runs_best_to_worst` — a ranked
  list of every club by upcoming fixture difficulty, useful for spotting
  favorable swings a few gameweeks ahead.

## Full automation (no manual weekly work)
This script alone still requires you to run it and paste output somewhere.
To make the whole thing — pull, research, and analysis — run automatically
on a schedule with zero manual steps:

1. Open **Claude Code Desktop** (a different app from claude.ai chat —
   it runs on your own machine with real internet access, unlike the
   sandboxed chat environment).
2. Go to **Routines → New Task → Local**, point it at the folder
   containing `fpl_pull.py`.
3. Paste the prompt from `TASK_PROMPT.md` into the task.
4. Set the cadence (weekly, a few hours before your deadline is ideal).
5. Each run will pull live data, search the web for injury/news updates,
   and hand you a season-aware transfer + lineup report — no manual
   pulling or pasting required going forward.

See `TASK_PROMPT.md` for the exact prompt and setup details.
