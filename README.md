# PlayerScraper - MaxPreps Mississippi Top Performers

Daily automation that scans MaxPreps Mississippi box scores across multiple sports
and emails the players who cleared sport-specific statistical thresholds.

## What It Does

- Scans a **rolling window of recent dates**, not just yesterday, so box scores
  entered days after a game are still caught.
- Keeps a **ledger of already-emailed performers** so re-scanning the same dates
  never re-sends the same player.
- Discovers games from the state scoreboard for each sport and gender.
- Verifies each game's date **from the game URL** before processing it.
- Filters to Mississippi games, including MS-vs-out-of-state matchups.
- Opens each game's **Stats** tab and parses both teams' stat tables.
- Applies deterministic, sport-specific thresholds.
- Sends a plain-text Gmail summary, grouped by the date the game was played,
  with late arrivals labelled.
- Sends an error email with traceback if the run fails.

## The late-stats problem

Mississippi coaches often enter a Friday night box score on Sunday or Monday. A
job that only ever looks at yesterday misses every one of those permanently.

This scraper instead scans the last `--days` dates (default **4**) on every run:

```
Run on Tuesday with --days 4  ->  scans Fri, Sat, Sun, Mon
```

Anything newly appearing for an older date is reported and flagged:

```
=== Fri 08/28/2026 (LATE ENTRY – posted 2 day(s) after the game) ===
```

The ledger at `state/reported.json` is what keeps this from becoming spam: each
performer is keyed by game date, sport, player, team and contest id, so a player
reported on Saturday is not reported again on Sunday, Monday or Tuesday.

If a stat line is later *revised upward* (a partial box score is completed), the
performer is re-sent once, marked `(UPDATED STAT LINE)`.

Raise `--days` if your stats routinely land later than four days.

## Supported Sports

Boys and girls are separate entries because MaxPreps distinguishes them with a
`?gender=` parameter on a shared slug:

| Key | Scoreboard | In-season months |
| --- | --- | --- |
| `football` | football / boys | Aug–Dec |
| `boys-basketball` | basketball / boys | Nov–Mar |
| `girls-basketball` | basketball / girls | Nov–Mar |
| `baseball` | baseball / boys | Feb–Jun |
| `softball` | softball / girls | Feb–Jun, Aug–Oct |
| `volleyball` | volleyball / girls | Aug–Nov |
| `boys-soccer` | soccer / boys | Nov–Mar |
| `girls-soccer` | soccer / girls | Nov–Mar |
| `boys-lacrosse` | lacrosse / boys | Feb–May |
| `girls-lacrosse` | lacrosse / girls | Feb–May |

Out-of-season sports are skipped so the daily run stays short. Override with
`--include-offseason`.

**Golf and track & field are not supported.** MaxPreps publishes no state-level
scoreboard for them — `/ms/golf/scores/` and `/ms/track-and-field/scores/` both
return 404 — so there is nothing to poll on a daily cadence.

Run `python top_performers.py --list-sports` to print the current set.

## Requirements

- Python 3.10+
- A Gmail account with an app password enabled

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Environment variables:

```bash
export GMAIL_SENDER="you@gmail.com"
export GMAIL_APP_PASSWORD="your_gmail_app_password"
export GMAIL_RECIPIENT="destination@email.com"

# Only needed if you pass --login. MaxPreps serves box scores anonymously,
# so authentication is off by default.
export MAXPREPS_EMAIL="your_maxpreps_email"
export MAXPREPS_PASSWORD="your_maxpreps_password"
```

PowerShell:

```powershell
$env:GMAIL_SENDER="you@gmail.com"; $env:GMAIL_APP_PASSWORD="app_password"; $env:GMAIL_RECIPIENT="dest@email.com"
```

## Usage

Normal daily run — last 4 days ending yesterday:

```bash
python top_performers.py
```

Seed the ledger on first deployment so the first real run does not email a backlog:

```bash
python top_performers.py --seed-ledger
```

Print the report without emailing:

```bash
python top_performers.py --no-email
```

Widen the window to a full week:

```bash
python top_performers.py --days 7
```

One sport, one date, verbose:

```bash
python top_performers.py --test --date 08/28/2026 --days 1 --sports football
```

Backfill and re-report performers already emailed:

```bash
python top_performers.py --date 08/28/2026 --days 1 --ignore-ledger
```

### Options

| Flag | Purpose |
| --- | --- |
| `--date MM/DD/YYYY` | End of the scan window (default: yesterday, America/Chicago) |
| `--days N` | How many days back to scan (default 4) |
| `--sports a,b` | Restrict to specific sport keys |
| `--seed-ledger` | Mark current findings as reported, send nothing |
| `--ignore-ledger` | Report everything found, even if emailed before |
| `--no-ledger-write` | Do not persist the ledger |
| `--no-email` | Print the report instead of sending it |
| `--only-when-new` | Send no email when nothing new qualified |
| `--include-offseason` | Do not skip out-of-season sports |
| `--include-out-of-state-players` | Also report opposing non-MS players in border games |
| `--max-games N` | Safety cap on games per sport per date (default 250) |
| `--login` | Authenticate to MaxPreps first |
| `--test` | Verbose logging and a TEST subject prefix |
| `--headed` | Run the browser visibly for debugging |
| `--list-sports` | Print configured sport keys and exit |

## Email Behavior

- New qualifiers: `Daily Top Performers – MM/DD/YYYY (3 new, 1 late)`
- Nothing new: `No New Qualifying Performances – MM/DD/YYYY`
- Test mode: subject prefixed `TEST RUN – `
- Failure: `ERROR – Top Performers Script Failed`, body contains the traceback

The body groups performers by the date the game was played, labels late
arrivals, and closes with a per-sport run summary plus a count of games in the
window that still have no box score entered.

## Mississippi Game Inclusion Rule

MaxPreps calls a contest a `/game/` for football, basketball, baseball and
softball, but a `/match/` for volleyball, soccer and lacrosse. Both are
recognised.

A game is included when its URL is state-scoped (`/ms/...`) or is an
`/inter-state/` contest whose slug names a Mississippi team. The contest payload
is then checked for a team with `"state": "MS"`.

By default only Mississippi teams' players are reported, so an out-of-state
opponent's star does not headline a Mississippi report. Pass
`--include-out-of-state-players` to include them.

## Thresholds

**Baseball / Softball** — 4+ hits, 4+ RBI, 2+ HR, 3+ extra-base hits, 12+ strikeouts,
complete-game shutout, no-hitter, perfect game.

**Basketball** — 32+ points, triple-double, scoring double-double (22+ pts with 10+
reb or ast), high double-double (28+ pts with a second 10+ category), 18+ rebounds,
12+ assists, 7+ threes, 6+ steals, 6+ blocks.

**Football** — 320+ passing yards, 4+ passing TD, 150+ rushing yards, 3+ rushing TD,
140+ receiving yards, 3+ receiving TD, 14+ tackles, 3+ sacks, 2+ interceptions.

**Volleyball** — 20+ kills, 30+ assists, 24+ digs, 10+ aces, 8+ blocks.
Calibrated against a full Mississippi slate; most matches here are three-set
sweeps, so five-set thresholds never fire.

**Soccer** — 3+ goals (hat trick), 3+ assists, shutout with 8+ saves.

**Lacrosse** — 5+ goals, 5+ assists, 7+ points, 15+ saves.

All thresholds are plain dictionaries at the top of `top_performers.py`.

## Reliability Notes

- Retries on scoreboard loads, game loads and SMTP sends.
- A failing sport or date does not abort the run; the failure is reported in the
  run summary and every other sport still completes.
- The ledger is only written after the email actually sends, so an SMTP failure
  does not silently swallow a day of results.
- Ledger entries are pruned after 120 days.
- Images, fonts and video are blocked in the browser context.

## GitHub Actions

`.github/workflows/daily_top_performers.yml` runs the scraper daily at 12:15 UTC
and commits the updated ledger back to the repository.

Required repository secrets:

- `GMAIL_SENDER`
- `GMAIL_APP_PASSWORD`
- `GMAIL_RECIPIENT`

The workflow needs `contents: write` permission to persist `state/reported.json`.
**The ledger must persist between runs** — without it every run re-emails the
whole window. It is committed to the repo rather than cached, because Actions
caches can be evicted at any time.

### Running a specific day on demand

To pull Friday's stats on Monday morning and have them emailed to you:

1. Repository → **Actions** → **Daily Top Performers** → **Run workflow**
2. Put the game date in **date_override**, e.g. `08/28/2026`
3. **Run workflow**

Leave everything else blank. Naming a date automatically scans that single day
(rather than the four-day catch-up window) and prefixes the email `ON-DEMAND`,
so it is never confused with the daily digest:

```
ON-DEMAND – Top Performers – Friday 08/28/2026 (2 new)
```

**You only get what has not already been emailed.** If the weekend's automatic
runs already sent you Friday's Wedgeworth and Green, Monday's run sends only the
box scores entered since — which is the point of running it. The footer tells
you what was held back:

```
(2 other qualifying performance(s) were already reported earlier.)
```

The date field accepts `08/28/2026`, `8/28/2026`, `08-28-2026`, `2026-08-28`,
or `Aug 28 2026`.

### Other dispatch options

| Input | Effect |
| --- | --- |
| `date_override` | Game date to report on. Blank = yesterday. |
| `days` | Days to scan ending at that date. Blank = 1 when a date is given, else 4. |
| `sports` | Comma-separated sport keys. Blank = all in season. |
| `include_offseason` | Scan sports listed as out of season for that month. |
| `resend_already_reported` | Re-send performers already emailed. Normally leave unchecked. |
| `test_mode` | Verbose logging and a `TEST` subject prefix. |

Check **resend_already_reported** only when you want the full slate for a date
again — a re-send does not update the ledger, so the next scheduled run behaves
as though it never happened.

A manual run started while the scheduled one is going will queue behind it
rather than race it for the ledger.
