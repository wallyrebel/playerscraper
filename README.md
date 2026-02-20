# PlayerScraper - MaxPreps Mississippi Top Performers

Production-oriented Python automation for daily MaxPreps Mississippi box-score monitoring across multiple sports.

## What It Does

- Logs in to MaxPreps with Playwright and persistent `storage_state.json`.
- Processes all configured Mississippi sports for a target date.
- Defaults to **yesterday** in `America/Chicago`.
- Visits each game and opens **Box Score** when available.
- Parses only structured stat tables (no recap/narrative scraping).
- Applies deterministic sport-specific thresholds.
- Sends a plain-text Gmail summary email.
- Sends a no-qualifier email when nothing meets thresholds.
- Sends an error email with traceback if the run fails.
- Supports test mode and verbose logs.

## Supported Sports

```python
SPORTS = [
    "baseball",
    "softball",
    "basketball",
    "soccer",
    "football",
    "volleyball",
    "lacrosse",
    "golf",
    "track-field",
]
```

## Requirements

- Python 3.10+
- A Gmail account with an app password enabled

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Install Playwright browser binaries:

```bash
playwright install
```

3. Set environment variables:

```bash
# MaxPreps
export MAXPREPS_EMAIL="your_maxpreps_email"
export MAXPREPS_PASSWORD="your_maxpreps_password"

# Gmail SMTP
export GMAIL_SENDER="you@gmail.com"
export GMAIL_APP_PASSWORD="your_gmail_app_password"
export GMAIL_RECIPIENT="destination@email.com"
```

PowerShell example:

```powershell
$env:MAXPREPS_EMAIL="your_maxpreps_email"
$env:MAXPREPS_PASSWORD="your_maxpreps_password"
$env:GMAIL_SENDER="you@gmail.com"
$env:GMAIL_APP_PASSWORD="your_gmail_app_password"
$env:GMAIL_RECIPIENT="destination@email.com"
```

## Usage

Default run (yesterday in America/Chicago):

```bash
python top_performers.py
```

Test run for yesterday:

```bash
python top_performers.py --test
```

Test run for a specific date:

```bash
python top_performers.py --test --date 02/17/2026
```

Run with visible browser for debugging:

```bash
python top_performers.py --headed
```

## Email Behavior

- Qualifiers found:
  - Subject: `Daily Top Performers – MM/DD/YYYY`
- No qualifiers:
  - Subject: `No Qualifying Performances – MM/DD/YYYY`
- Test mode:
  - Subject starts with `TEST RUN – Top Performers`
- Error:
  - Subject: `ERROR – Top Performers Script Failed`
  - Body includes full traceback

## Reliability Features

- Retry logic: 2 attempts per page load
- Random delay: 1.5-3.0 seconds between page loads
- Deduplicates games and player records
- Logs per-sport:
  - sport processed
  - games found
  - games skipped (no box score)
  - games skipped (non-MS context)
  - players parsed
  - players qualified

## Mississippi Game Inclusion Rule

- A game is processed only when MaxPreps contest payload state includes `MS`.
- This supports Mississippi vs out-of-state matchups while excluding unrelated non-Mississippi games.

## Baseball/Softball Top-Performer Thresholds

- 4+ hits
- 4+ RBI
- 2+ home runs
- 3+ extra-base hits
- 12+ strikeouts
- complete-game shutout
- no-hitter
- perfect game

## Basketball Top-Performer Thresholds

- 32+ points
- triple-double
- scoring double-double (22+ points with 10+ rebounds or 10+ assists)
- high double-double (28+ points with a second 10+ category)
- 18+ rebounds
- 12+ assists
- 7+ three-pointers made
- 6+ steals
- 6+ blocks

## Football Top-Performer Thresholds

- 320+ passing yards
- 4+ passing TD
- 150+ rushing yards
- 3+ rushing TD
- 140+ receiving yards
- 3+ receiving TD
- 14+ tackles
- 3+ sacks
- 2+ interceptions

## Volleyball Top-Performer Thresholds

- 25+ kills
- 45+ assists
- 35+ digs
- 10+ aces
- 8+ blocks

## Notes

- `storage_state.json` is created/updated after successful login.
- Keep credentials in environment variables only.
- This script intentionally avoids AI usage and HTML email formatting.
- Lacrosse is still processed, but no qualifying threshold is applied because none was provided in the specification.

## Optional GitHub Actions

An optional workflow is included at `.github/workflows/daily_top_performers.yml`.

Set these repository secrets:

- `MAXPREPS_EMAIL`
- `MAXPREPS_PASSWORD`
- `GMAIL_SENDER`
- `GMAIL_APP_PASSWORD`
- `GMAIL_RECIPIENT`
