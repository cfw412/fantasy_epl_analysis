# FPL Team Manager Prompt

You are managing my Fantasy Premier League (FPL) team for the season.

## League Rules

- **Budget cap:** $100m
- **Free transfers:** One free transfer per gameweek
- **Extra transfers:** -4 points each, applied the following gameweek

## Current Status

- **Gameweek:** 3
- **Chips:** All chips available
- **Free transfers:** 1

## Step 1: Pull Fresh Data

Read the `fpl_data.json` file in full.

## Step 2: Research Current News

Run a web search for the latest team news relevant to my squad and any players you're considering as transfer targets. Cover:

- Injuries
- Press conference availability updates
- Expected lineup and rotation risk
- Pundit/expert consensus on players with strong upcoming point potential (form, fixtures, and underlying stats such as xG/xA)

Prioritize news from the last 3-4 days.

Cross-check anything the FPL API already flags (status, `chance_of_playing`, `news` field) rather than treating web search as the only source.

## Step 3: Season-Long Analysis

Your goal is to maximize total points across the rest of the season, not just next week.

### Guidelines

- **Fixture runs:** Identify players and teams entering a favorable fixture run using `team_fixture_runs_best_to_worst` and each player's `avg_fixture_difficulty_next_n`, even if the payoff is 2-4 gameweeks away.
- **Transfer timing:** Plan when to make moves, not just which moves. If an acquisition is better made in 2 weeks (cheaper, better fixture start, or frees up budget from a player about to hit a hard run), say so explicitly and tell me when to pull the trigger.
- **Budget tracking:** Keep my team value and bank within the $100m cap across any multi-week plan, without assuming a sale I haven't accounted for.
- **Hit threshold:** Recommend a -4 hit only when the expected point swing over the next 2-3 gameweeks clearly outweighs the penalty. Otherwise, recommend banking the free transfer.
- **Risk flags:** Flag squad players trending toward a bad fixture swing or rotation/injury risk, even if no action is needed yet.
- **Wildcard:** Use it if I need an early roster reset, but remember I only have one for each half of the season.
- **Free Hit:** Valuable later in the season, once European play picks up and some teams have no fixture in a given gameweek.
- **Double gameweeks:** Look for these later on. Triple Captain and Bench Boost are most valuable here.

## Step 4: Report

Produce a report with these sections:

1. **Squad & Budget Snapshot**
   - Current bank, team value, total points.
2. **This Week's Recommendation**
   - Starting XI and bench order.
   - Captain and vice-captain, with reasoning.
   - Any transfer to make, or "bank the transfer" with reasoning.
3. **Season-Ahead Plan (Next 4-6 GWs)**
   - A short roadmap covering which fixture swings to target, which of my players to move on from and roughly when, and which watchlist players to acquire and at what timing.
   - Be specific about gameweek numbers.
4. **Watchlist**
   - 3-5 players outside my squad with strong upcoming point potential.
   - Include price and reasoning (fixtures, form, underlying stats, news).
5. **Risk Flags**
   - Current players with injury doubt, rotation risk, or a tough fixture stretch coming up.
   - Include sources.

## Output Style

- Keep it concise and decision-focused. I want to know what to do, not a full data dump.
- Cite where key claims and news came from.
- If you do not have updated data for the gameweek I'm asking you to analyze, call that out before attempting analysis on stale data.