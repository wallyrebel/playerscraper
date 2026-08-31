#!/usr/bin/env python3
"""MaxPreps Mississippi top-performer scraper.

Scans a rolling window of recent dates (not just yesterday) so that box scores
entered late -- a Friday night game keyed in on Sunday or Monday -- are still
picked up. A persisted ledger records what has already been emailed, so
re-scanning the same dates never re-sends the same performer twice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import smtplib
import ssl
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import pytz
from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Page, sync_playwright

BASE_URL = "https://www.maxpreps.com"
LOGIN_URL = f"{BASE_URL}/login/"
STATE = "ms"
STORAGE_STATE_PATH = Path("storage_state.json")
LEDGER_PATH = Path("state/reported.json")
TZ = pytz.timezone("America/Chicago")
PAGE_TIMEOUT_MS = 45000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Ledger entries are kept well beyond the largest sane lookback window so a
# pruned entry can never come back and be re-reported.
LEDGER_RETENTION_DAYS = 120

DEFAULT_LOOKBACK_DAYS = 4


# ---------------------------------------------------------------------------
# Sport configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SportConfig:
    key: str  # unique internal identifier
    label: str  # email heading
    slug: str  # MaxPreps URL slug
    gender: str  # "boys" | "girls"
    rules: str  # which evaluator family to apply
    season_months: Tuple[int, ...]  # months the sport can plausibly be played

    def scoreboard_url(self, date_value: str) -> str:
        # The date slashes must stay literal. MaxPreps answers 406 to a
        # percent-encoded ?date=8%2F28%2F2026.
        query = f"date={date_value}"
        if self.gender:
            query += f"&gender={self.gender}"
        return f"{BASE_URL}/{STATE}/{self.slug}/scores/?{query}"


# Season months are deliberately generous (they include preseason and
# playoffs). They exist only to skip scoreboard fetches that cannot possibly
# return games, which keeps the daily run short in the off-season.
SPORT_CONFIGS: Tuple[SportConfig, ...] = (
    SportConfig("football", "FOOTBALL", "football", "boys", "football", (8, 9, 10, 11, 12)),
    SportConfig("boys-basketball", "BOYS BASKETBALL", "basketball", "boys", "basketball", (11, 12, 1, 2, 3)),
    SportConfig("girls-basketball", "GIRLS BASKETBALL", "basketball", "girls", "basketball", (11, 12, 1, 2, 3)),
    SportConfig("baseball", "BASEBALL", "baseball", "boys", "baseball", (2, 3, 4, 5, 6)),
    SportConfig("softball", "SOFTBALL", "softball", "girls", "baseball", (2, 3, 4, 5, 6, 8, 9, 10)),
    SportConfig("volleyball", "VOLLEYBALL", "volleyball", "girls", "volleyball", (8, 9, 10, 11)),
    SportConfig("boys-soccer", "BOYS SOCCER", "soccer", "boys", "soccer", (11, 12, 1, 2, 3)),
    SportConfig("girls-soccer", "GIRLS SOCCER", "soccer", "girls", "soccer", (11, 12, 1, 2, 3)),
    SportConfig("boys-lacrosse", "BOYS LACROSSE", "lacrosse", "boys", "lacrosse", (2, 3, 4, 5)),
    SportConfig("girls-lacrosse", "GIRLS LACROSSE", "lacrosse", "girls", "lacrosse", (2, 3, 4, 5)),
)

# NOTE: golf and track & field are intentionally absent. MaxPreps publishes no
# state-level scoreboard for them -- https://www.maxpreps.com/ms/golf/scores/
# and .../track-and-field/scores/ both return 404 -- so there is nothing to
# poll daily. They previously cost one guaranteed 404 per run and could never
# produce a result.

SPORTS_BY_KEY = {cfg.key: cfg for cfg in SPORT_CONFIGS}

PLAYER_HEADERS = {"player", "name", "athlete", "competitor", "golfer", "runner"}

NON_PLAYER_ROW_NAMES = {
    "totals",
    "total",
    "teamtotals",
    "teamtotal",
    "team",
    "opponent",
    "opponents",
    "opponenttotals",
}

BASEBALL_SOFTBALL_TOP_THRESHOLDS = {
    "hits": 4,
    "rbi": 4,
    "hr": 2,
    "xbh": 3,
    "strikeouts": 12,
}

BASKETBALL_TOP_THRESHOLDS = {
    "points": 32,
    "high_double_double_points_floor": 28,
    "scoring_double_double_points_floor": 22,
    "scoring_double_double_secondary_floor": 10,
    "double_double_stat_min": 10,
    "triple_double_categories": 3,
    "rebounds": 18,
    "assists": 12,
    "threes_made": 7,
    "steals": 6,
    "blocks": 6,
}

FOOTBALL_TOP_THRESHOLDS = {
    "passing_yards": 320,
    "passing_td": 4,
    "rushing_yards": 150,
    "rushing_td": 3,
    "receiving_yards": 140,
    "receiving_td": 3,
    "tackles": 14,
    "sacks": 3,
    "interceptions": 2,
}

# Calibrated against a full Mississippi slate (8/27/2026, 1,245 parsed rows).
# The original 25/45/35 for kills/assists/digs sat above the best performance
# in the state that night -- the top kill total was 24, the top assist total
# 35, the top dig total 27 -- so those three could never fire. Most matches
# here are three-set sweeps, not five-set marathons. Aces and blocks were
# already well calibrated and are unchanged.
VOLLEYBALL_TOP_THRESHOLDS = {
    "kills": 20,
    "assists": 30,
    "digs": 24,
    "aces": 10,
    "blocks": 8,
}

SOCCER_TOP_THRESHOLDS = {
    "goals": 3,  # hat trick
    "assists": 3,
    "saves_with_shutout": 8,
}

LACROSSE_TOP_THRESHOLDS = {
    "goals": 5,
    "assists": 5,
    "points": 7,
    "saves": 15,
}

# Upper bounds used to reject rows whose columns clearly misaligned. A value
# above the bound means the row was parsed wrong, not that a record was set.
SANITY_CAPS = {
    "passing_yards": 800.0,
    "rushing_yards": 600.0,
    "receiving_yards": 500.0,
    "touchdowns": 12.0,
    "tackles": 40.0,
    "sacks": 12.0,
    "interceptions": 7.0,
    "points": 100.0,
    "rebounds": 45.0,
    "assists": 40.0,
    "kills": 80.0,
    "vb_assists": 120.0,
    "digs": 90.0,
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class GameMeta:
    game_url: str = ""
    game_date: str = ""  # MM/DD/YYYY
    teams: List[str] = field(default_factory=list)
    scores: Dict[str, str] = field(default_factory=dict)
    ms_teams: Set[str] = field(default_factory=set)


@dataclass
class Qualifier:
    sport_key: str
    sport_label: str
    game_date: str  # MM/DD/YYYY the game was actually played
    game_url: str
    player_name: str
    team: str
    opponent: str
    team_score: str
    opponent_score: str
    stat_line: str
    reasons: Tuple[str, ...] = ()

    def ledger_key(self) -> str:
        return "|".join(
            (
                self.game_date,
                self.sport_key,
                normalize_key(self.player_name),
                normalize_key(self.team),
                contest_id(self.game_url) or normalize_key(self.opponent),
            )
        )

    def reason_fingerprint(self) -> str:
        payload = ";".join(sorted(self.reasons))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class SportDateResult:
    config: SportConfig
    date_value: str
    qualifiers: List[Qualifier] = field(default_factory=list)
    metrics: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def is_player_header_key(norm_key: str) -> bool:
    if not norm_key:
        return False
    if norm_key in PLAYER_HEADERS:
        return True
    if norm_key.endswith("name"):
        return True
    return "athlete" in norm_key or "player" in norm_key


def parse_number(value: str) -> float:
    text = normalize_text(value).replace(",", "")
    if not text or text in {"-", "--", "N/A", "n/a", "E"}:
        return 0.0
    # The leading-dot alternative is required: MaxPreps writes half sacks as
    # ".5" and rate stats as ".359". Without it, ".5" matched only the "5" and
    # a half sack was read as five sacks.
    match = re.search(r"-?(?:\d+(?:\.\d+)?|\.\d+)", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except ValueError:
        return 0.0


def format_num(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def canonical_url(url: str) -> str:
    parsed = urlparse(urljoin(BASE_URL, url))
    if "maxpreps.com" not in parsed.netloc.lower():
        return ""
    path = parsed.path.rstrip("/") or "/"
    # Keep only the contest id, dropping tab/tracking params, so the same game
    # discovered through several different links collapses to one URL.
    keep = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() == "c"]
    return urlunparse(parsed._replace(path=path, query=urlencode(keep), fragment=""))


def contest_id(url: str) -> str:
    for key, value in parse_qsl(urlparse(url).query):
        if key.lower() == "c":
            return value.lower()
    return ""


def with_tab(url: str, tab: str) -> str:
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() != "tab"]
    query.append(("tab", tab))
    return urlunparse(parsed._replace(query=urlencode(query)))


def random_delay(low: float = 0.6, high: float = 1.4) -> None:
    time.sleep(random.uniform(low, high))


def clean_player_name(value: str) -> str:
    """Strip the grade suffix MaxPreps appends, e.g. 'T. Washington (Jr)'."""
    name = normalize_text(value)
    return normalize_text(re.sub(r"\s*\((?:Fr|So|Jr|Sr|\d{1,2})\.?\)\s*$", "", name, flags=re.IGNORECASE))


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


DATE_INPUT_FORMATS = ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y", "%b %d %Y", "%B %d %Y")


def parse_mdy(value: str) -> date_cls:
    """Parse a date typed by a human.

    Accepts several spellings because this value is often typed by hand into
    the GitHub Actions dispatch form, where a rejected format costs a whole
    round trip.
    """
    text = normalize_text(value).replace(".", "")
    for fmt in DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(
        f"Could not read date {value!r}. Use MM/DD/YYYY, for example 08/28/2026."
    )


def fmt_mdy(value: date_cls) -> str:
    return value.strftime("%m/%d/%Y")


def maxpreps_date_param(value: date_cls) -> str:
    """MaxPreps scoreboards use unpadded M/D/YYYY."""
    return f"{value.month}/{value.day}/{value.year}"


def today_central() -> date_cls:
    return datetime.now(TZ).date()


def build_date_window(anchor: Optional[str], days: int) -> List[date_cls]:
    """Return the scan window, oldest first.

    The anchor defaults to yesterday in America/Chicago. `days` extends the
    window backwards so late-entered box scores are picked up: with days=4 a
    Tuesday run still re-checks the previous Friday.
    """
    if days < 1:
        raise ValueError("--days must be at least 1")
    if anchor:
        try:
            end = parse_mdy(anchor)
        except ValueError as exc:
            raise ValueError("--date must use MM/DD/YYYY") from exc
    else:
        end = today_central() - timedelta(days=1)
    return [end - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class Ledger:
    """Records which performers have already been emailed.

    Without this, widening the scan window to catch late-entered stats would
    re-send every performer from every date in the window, every day.
    """

    def __init__(self, path: Path, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self.entries: Dict[str, dict] = {}
        self._dirty = False

    def load(self) -> None:
        if not self.path.exists():
            logging.info("No ledger at %s; starting a new one.", self.path)
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logging.warning("Ledger at %s is unreadable; starting a new one.", self.path)
            return
        entries = payload.get("entries")
        if isinstance(entries, dict):
            self.entries = entries
        logging.info("Loaded %s ledger entries from %s", len(self.entries), self.path)

    def classify(self, item: Qualifier) -> Optional[str]:
        """Return 'new', 'updated', or None if this was already reported."""
        if not self.enabled:
            return "new"
        record = self.entries.get(item.ledger_key())
        if record is None:
            return "new"
        if record.get("fingerprint") == item.reason_fingerprint():
            return None
        # Stats were revised after the first report. Only re-send when the
        # performance actually grew, so a cosmetic edit stays quiet.
        if len(item.reasons) > int(record.get("reason_count", 0)):
            return "updated"
        return None

    def record(self, item: Qualifier) -> None:
        now = datetime.now(TZ).isoformat(timespec="seconds")
        key = item.ledger_key()
        existing = self.entries.get(key, {})
        self.entries[key] = {
            "fingerprint": item.reason_fingerprint(),
            "reason_count": len(item.reasons),
            "game_date": item.game_date,
            "sport": item.sport_key,
            "player": item.player_name,
            "team": item.team,
            "first_reported": existing.get("first_reported", now),
            "last_reported": now,
        }
        self._dirty = True

    def prune(self, today: date_cls) -> int:
        cutoff = today - timedelta(days=LEDGER_RETENTION_DAYS)
        stale = []
        for key, record in self.entries.items():
            raw = record.get("game_date", "")
            try:
                if parse_mdy(raw) < cutoff:
                    stale.append(key)
            except ValueError:
                stale.append(key)
        for key in stale:
            del self.entries[key]
        if stale:
            self._dirty = True
        return len(stale)

    def save(self) -> None:
        if not self.enabled or not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated": datetime.now(TZ).isoformat(timespec="seconds"),
            "entries": self.entries,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        logging.info("Wrote %s ledger entries to %s", len(self.entries), self.path)


# ---------------------------------------------------------------------------
# Discovery: scoreboard -> game URLs
# ---------------------------------------------------------------------------

# MaxPreps calls a contest a "game" for football, basketball, baseball and
# softball, but a "match" for volleyball, soccer and lacrosse. Matching only
# /game/ made every match-based sport silently return zero contests.
GAME_URL_RE = re.compile(
    r"https://www\.maxpreps\.com/(?P<scope>[a-z\-]+)/(?P<sport>[a-z0-9\-]+)/(?:game|match)/"
    # The trailing slash is optional so this also matches canonical_url() output,
    # which strips it.
    r"(?P<slug>[a-z0-9\-]+)/(?P<month>\d{1,2})-(?P<day>\d{1,2})-(?P<year>\d{4})/?"
    r"[^\"'\s<>]*",
    re.IGNORECASE,
)


def game_url_date(url: str) -> Optional[date_cls]:
    match = GAME_URL_RE.match(url)
    if not match:
        return None
    try:
        return date_cls(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    except ValueError:
        return None


def game_url_is_mississippi(url: str) -> bool:
    """Decide MS relevance from the URL alone.

    The MS scoreboard also lists national games and out-of-state matchups
    (a CA-vs-PA game showed up on the MS football board). State-scoped games
    live under /ms/; cross-border games live under /inter-state/ and encode
    each team's state in the slug, e.g. 'lafayette-oxford-ms-vs-wooddale-tn'.
    """
    match = GAME_URL_RE.match(url)
    if not match:
        return False
    scope = match.group("scope").lower()
    if scope == STATE:
        return True
    if scope != "inter-state":
        return False
    slug = match.group("slug").lower()
    return any(part.endswith(f"-{STATE}") or part == STATE for part in slug.split("-vs-"))


def fetch_scoreboard(page: Page, url: str, attempts: int = 3) -> str:
    """Load a scoreboard through the browser.

    Plain HTTP works for an unfiltered scoreboard but MaxPreps answers 406 to
    any request carrying a ?gender= parameter unless it comes from a real
    browser, and boys/girls share a slug, so the parameter is not optional.
    Reusing the Playwright context also keeps one consistent session.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            status = response.status if response else 0
            if status and status >= 400:
                raise RuntimeError(f"Scoreboard returned HTTP {status}: {url}")
            html = page.content()
            if "/game/" in html or "no games" in html.lower():
                return html
            # A scoreboard with neither games nor an explicit empty state was
            # probably served before hydration finished.
            page.wait_for_timeout(1500)
            return page.content()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logging.warning("Scoreboard fetch failed (%s/%s): %s", attempt, attempts, exc)
            if attempt < attempts:
                page.wait_for_timeout(1500 * attempt)
    raise RuntimeError(f"Could not fetch scoreboard {url}") from last_exc


def discover_games(html: str, target: date_cls) -> Tuple[List[str], Dict[str, int]]:
    """Extract MS game URLs for exactly `target` from a scoreboard page.

    Both filters matter. The scoreboard lists out-of-state games, and for
    out-of-season dates MaxPreps ignores the ?date= parameter entirely and
    serves the next season's schedule instead -- so the date encoded in each
    game URL is the only trustworthy source of truth.
    """
    metrics = {"links_seen": 0, "wrong_date": 0, "non_ms": 0}
    keep: Dict[str, str] = {}
    seen: Set[str] = set()
    for match in GAME_URL_RE.finditer(html):
        raw_url = match.group(0)
        if raw_url in seen:
            continue
        seen.add(raw_url)
        metrics["links_seen"] += 1
        if game_url_date(raw_url) != target:
            metrics["wrong_date"] += 1
            continue
        if not game_url_is_mississippi(raw_url):
            metrics["non_ms"] += 1
            continue
        clean = canonical_url(raw_url)
        if not clean:
            continue
        key = contest_id(clean) or clean
        keep.setdefault(key, clean)

    # Order in-state games first. Plain alphabetical sorting puts every
    # /inter-state/ game ahead of every /ms/ one, so a truncating --max-games
    # would drop purely Mississippi matchups in favour of border games.
    def rank(url: str) -> Tuple[int, str]:
        return (0 if f"/{STATE}/" in url else 1, url)

    return sorted(keep.values(), key=rank), metrics


# ---------------------------------------------------------------------------
# Game page parsing
# ---------------------------------------------------------------------------

TEAM_PAYLOAD_RE = re.compile(
    r'"name":"(?P<name>[^"]{2,60})","city":"[^"]{0,60}","state":"(?P<state>[A-Z]{2})"'
)
SCORE_DESC_RE = re.compile(
    r"\bthe\s+(?P<subject>[A-Za-z0-9'.&\- ]{2,50}?)\s+varsity\s+[a-z\- ]+?\s*team\s+"
    r"(?P<verb>won|lost|tied)\b[^.(]*?"
    # The opponent is followed by a location gloss that is sometimes just the
    # state -- "Brandon (MS)" -- and sometimes city and state -- "Faith
    # Academy (Mobile, AL)". Skip whatever is in the parentheses.
    r"\b(?:against|to|with)\s+(?P<other>[^(]{2,60}?)\s*(?:\([^)]*\))?\s*"
    r"by\s+a\s+score\s+of\s+(?P<first>\d+)\s*-\s*(?P<second>\d+)",
    re.IGNORECASE,
)


def parse_game_meta(html: str, soup: BeautifulSoup, game_url: str) -> GameMeta:
    meta = GameMeta(game_url=game_url)
    game_date = game_url_date(game_url)
    if game_date:
        meta.game_date = fmt_mdy(game_date)

    # Team names and their states come from the contest payload embedded in
    # the page. This is per-team, unlike a blanket "does 'MS' appear anywhere"
    # search, which matches nav chrome on every page.
    unescaped = html.encode("utf-8", "ignore").decode("unicode_escape", "ignore")
    for match in TEAM_PAYLOAD_RE.finditer(unescaped):
        name = normalize_text(match.group("name"))
        if not name or name in meta.teams:
            continue
        meta.teams.append(name)
        if match.group("state").upper() == STATE.upper():
            meta.ms_teams.add(name)

    if len(meta.teams) < 2:
        for script in soup.find_all("script", type="application/ld+json"):
            text = script.string or script.get_text()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            name = normalize_text(str(payload.get("name", "")))
            pair = re.match(r"(.+?)\s+vs\.?\s+(.+)", name, re.IGNORECASE)
            if pair:
                for side in (pair.group(1), pair.group(2)):
                    side = normalize_text(re.sub(r"\s+varsity\s+.*$", "", side, flags=re.IGNORECASE))
                    if side and side not in meta.teams:
                        meta.teams.append(side)

    meta.teams = meta.teams[:2]
    meta.scores = parse_scores(event_description(soup), meta.teams)
    return meta


def event_description(soup: BeautifulSoup) -> str:
    """The schema.org SportsEvent description sentence, as plain text.

    Taken from the parsed ld+json rather than by regexing the page, so the
    pattern cannot accidentally match across HTML tags and meta content.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        for item in payload if isinstance(payload, list) else [payload]:
            if not isinstance(item, dict):
                continue
            description = normalize_text(str(item.get("description", "")))
            if description:
                return description
    return ""


def parse_scores(description: str, teams: Sequence[str]) -> Dict[str, str]:
    """Pull the final score out of the schema.org description sentence.

    MaxPreps' ld+json SportsEvent has no homeTeam/awayTeam score fields, but
    its description reads '... won their home game against X by a score of
    38-14', which is the only structured score on the page.
    """
    if not description:
        return {}
    match = SCORE_DESC_RE.search(description)
    if not match:
        return {}
    first = int(match.group("first"))
    second = int(match.group("second"))
    high, low = max(first, second), min(first, second)
    subject_raw = normalize_text(match.group("subject"))
    other_raw = normalize_text(match.group("other"))
    verb = match.group("verb").lower()

    if verb == "won":
        subject_score, other_score = high, low
    elif verb == "lost":
        subject_score, other_score = low, high
    else:
        subject_score = other_score = first

    scores: Dict[str, str] = {}
    for raw, value in ((subject_raw, subject_score), (other_raw, other_score)):
        resolved = match_team_name(raw, teams) or raw
        if resolved:
            scores[resolved] = str(value)
    return scores


def match_team_name(candidate: str, teams: Sequence[str]) -> str:
    """Resolve a toggle label like 'Bay (Bay St. Louis)' to a team name.

    Exact matches win, then the longest containment. Taking the first
    containment instead would collapse 'St. Joseph' and 'St. Joseph Catholic'
    -- both real teams in one game -- onto whichever came first.
    """
    key = normalize_key(candidate)
    if not key:
        return ""
    for team in teams:
        if normalize_key(team) == key:
            return team
    best, best_len = "", 0
    for team in teams:
        team_key = normalize_key(team)
        if team_key and (team_key in key or key in team_key) and len(team_key) > best_len:
            best, best_len = team, len(team_key)
    return best


def dedupe_headers(headers: Sequence[str]) -> List[str]:
    """Normalize headers, disambiguating collisions.

    MaxPreps football passing tables carry both 'C' (completions) and 'C%'
    (completion pct); both normalize to 'c', and a plain dict would silently
    keep the percentage.
    """
    out: List[str] = []
    counts: Dict[str, int] = {}
    for header in headers:
        norm = normalize_header(header)
        if not norm:
            out.append("")
            continue
        counts[norm] = counts.get(norm, 0) + 1
        out.append(norm if counts[norm] == 1 else f"{norm}__{counts[norm]}")
    return out


def parse_table(table) -> Tuple[List[str], List[str], List[Dict[str, object]]]:
    """Return (raw headers, normalized headers, rows)."""
    thead = table.find("thead")
    header_row = thead.find("tr") if thead else table.find("tr")
    if not header_row:
        return [], [], []

    headers = [normalize_text(c.get_text(" ", strip=True)) for c in header_row.find_all(["th", "td"])]
    if len(headers) < 2:
        return [], [], []

    norm_headers = dedupe_headers(headers)
    header_sig = "|".join(normalize_header(h) for h in headers)
    rows: List[Dict[str, object]] = []

    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = [normalize_text(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        if "|".join(normalize_header(v) for v in cells) == header_sig:
            continue
        # A row whose width does not match the header is a section divider or
        # a rowspan artifact. Padding it would shift every stat one column.
        if len(cells) != len(headers):
            continue

        raw: Dict[str, str] = {}
        numeric: Dict[str, float] = {}
        for key, value in zip(norm_headers, cells):
            if not key:
                continue
            raw[key] = value
            numeric[key] = parse_number(value)
        rows.append({"cells": cells, "raw": raw, "stats": numeric})

    return headers, norm_headers, rows


def table_section(table) -> str:
    """The <h3>/<h2> heading a stat table sits under, e.g. 'Passing'."""
    parts: List[str] = []
    for tag in ("h3", "h2"):
        node = table.find_previous(tag)
        if node:
            text = normalize_text(node.get_text(" ", strip=True))
            if text and len(text) <= 60:
                parts.append(text)
    caption = table.find("caption")
    if caption:
        parts.append(normalize_text(caption.get_text(" ", strip=True)))
    # The page title block is deliberately excluded: it carries the school
    # name, and a school like "Pass Christian" would make every one of its
    # tables look like a passing table.
    return " | ".join(parts).lower()


def row_player_name(norm_headers: Sequence[str], row: Dict[str, object]) -> str:
    raw = row.get("raw", {})
    if not isinstance(raw, dict):
        return ""
    for key in norm_headers:
        if is_player_header_key(key):
            name = clean_player_name(str(raw.get(key, "")))
            if name:
                return name
    return ""


def is_non_player_row(name: str) -> bool:
    key = normalize_key(name)
    if not key:
        return True
    if key in NON_PLAYER_ROW_NAMES:
        return True
    return key.endswith("totals")


def stat_value(stats: Dict[str, float], aliases: Sequence[str]) -> float:
    """Exact-alias lookup only.

    The previous substring fallback matched 'g' inside 'gp'/'avg', so soccer
    goals were read out of the games-played column.
    """
    for alias in aliases:
        key = normalize_header(alias)
        if key in stats:
            return stats[key]
    return 0.0


def within(value: float, cap_key: str) -> float:
    cap = SANITY_CAPS.get(cap_key)
    if cap is None:
        return value
    if value < 0 or value > cap:
        return 0.0
    return value


# ---------------------------------------------------------------------------
# Evaluators. Each returns (stat_line, reasons) or None.
# ---------------------------------------------------------------------------

Evaluation = Optional[Tuple[str, List[str]]]


def eval_basketball(stats: Dict[str, float], section: str) -> Evaluation:
    _ = section
    pts = within(stat_value(stats, ("pts", "points", "p")), "points")
    reb = within(stat_value(stats, ("reb", "trb", "totreb", "totalrebounds", "rebs")), "rebounds")
    ast = within(stat_value(stats, ("ast", "assists", "asst")), "assists")
    threes = stat_value(stats, ("3ptm", "3pm", "3fgm", "fg3m", "3pt", "3s", "threes", "3ptmade"))
    stl = stat_value(stats, ("stl", "steals", "st"))
    blk = stat_value(stats, ("blk", "blocks", "bs"))

    reasons: List[str] = []
    if pts >= BASKETBALL_TOP_THRESHOLDS["points"]:
        reasons.append(f"{format_num(pts)} pts")
    if (
        pts >= BASKETBALL_TOP_THRESHOLDS["scoring_double_double_points_floor"]
        and reb >= BASKETBALL_TOP_THRESHOLDS["scoring_double_double_secondary_floor"]
    ):
        reasons.append("scoring double-double (pts/reb)")
    if (
        pts >= BASKETBALL_TOP_THRESHOLDS["scoring_double_double_points_floor"]
        and ast >= BASKETBALL_TOP_THRESHOLDS["scoring_double_double_secondary_floor"]
    ):
        reasons.append("scoring double-double (pts/ast)")
    categories_10 = sum(v >= BASKETBALL_TOP_THRESHOLDS["double_double_stat_min"] for v in (pts, reb, ast, stl, blk))
    if categories_10 >= BASKETBALL_TOP_THRESHOLDS["triple_double_categories"]:
        reasons.append("triple-double")
    elif categories_10 >= 2 and pts >= BASKETBALL_TOP_THRESHOLDS["high_double_double_points_floor"]:
        reasons.append("high double-double")
    if reb >= BASKETBALL_TOP_THRESHOLDS["rebounds"]:
        reasons.append(f"{format_num(reb)} reb")
    if ast >= BASKETBALL_TOP_THRESHOLDS["assists"]:
        reasons.append(f"{format_num(ast)} ast")
    if threes >= BASKETBALL_TOP_THRESHOLDS["threes_made"]:
        reasons.append(f"{format_num(threes)} 3PM")
    if stl >= BASKETBALL_TOP_THRESHOLDS["steals"]:
        reasons.append(f"{format_num(stl)} stl")
    if blk >= BASKETBALL_TOP_THRESHOLDS["blocks"]:
        reasons.append(f"{format_num(blk)} blk")

    if not reasons:
        return None
    line = (
        f"PTS {format_num(pts)}, REB {format_num(reb)}, AST {format_num(ast)}, "
        f"3PM {format_num(threes)}, STL {format_num(stl)}, BLK {format_num(blk)}"
    )
    return line, reasons


def eval_baseball(stats: Dict[str, float], norm_headers: Sequence[str], section: str) -> Evaluation:
    header_set = {h for h in norm_headers if h}
    is_batting = "batting" in section or ({"ab", "pa"} & header_set and "rbi" in header_set)
    is_pitching = "pitching" in section or ({"ip", "era"} & header_set)

    reasons: List[str] = []
    details: List[str] = []

    if is_batting and not ("ip" in header_set or "era" in header_set):
        hits = stat_value(stats, ("h", "hits"))
        rbi = stat_value(stats, ("rbi", "rbis"))
        hr = stat_value(stats, ("hr", "homeruns"))
        doubles = stat_value(stats, ("2b", "doubles"))
        triples = stat_value(stats, ("3b", "triples"))
        ab = stat_value(stats, ("ab", "atbats"))
        xbh = doubles + triples + hr

        # A hitter cannot have more hits than at-bats. When that happens the
        # row was misparsed, so drop it rather than report a fake 6-for-2.
        plausible = ab <= 0 or hits <= ab
        if plausible:
            if hits >= BASEBALL_SOFTBALL_TOP_THRESHOLDS["hits"]:
                reasons.append(f"{format_num(hits)} hits")
            if rbi >= BASEBALL_SOFTBALL_TOP_THRESHOLDS["rbi"]:
                reasons.append(f"{format_num(rbi)} RBI")
            if hr >= BASEBALL_SOFTBALL_TOP_THRESHOLDS["hr"]:
                reasons.append(f"{format_num(hr)} HR")
            if xbh >= BASEBALL_SOFTBALL_TOP_THRESHOLDS["xbh"]:
                reasons.append(f"{format_num(xbh)} XBH")
            details.append(
                f"Batting: {format_num(hits)}-for-{format_num(ab)}, RBI {format_num(rbi)}, "
                f"HR {format_num(hr)}, XBH {format_num(xbh)}"
            )
        else:
            logging.debug("Dropping implausible batting row: H=%s AB=%s", hits, ab)

    if is_pitching and "rbi" not in header_set:
        strikeouts = stat_value(stats, ("so", "k", "ks", "strikeouts"))
        innings = stat_value(stats, ("ip", "inningspitched"))
        complete_game = stat_value(stats, ("cg",))
        earned_runs = stat_value(stats, ("er", "earnedruns"))
        runs_allowed = stat_value(stats, ("r", "runs"))
        hits_allowed = stat_value(stats, ("h", "ha", "hitsallowed"))
        walks = stat_value(stats, ("bb", "walks"))
        no_hitter = stat_value(stats, ("nh",))
        perfect_game = stat_value(stats, ("pg",))

        if strikeouts >= BASEBALL_SOFTBALL_TOP_THRESHOLDS["strikeouts"]:
            reasons.append(f"{format_num(strikeouts)} K")
        # CG is not always a column. A pitcher who went the distance with no
        # runs allowed is a shutout regardless of whether MaxPreps flags it.
        full_game = complete_game >= 1 or innings >= 7
        if full_game and earned_runs == 0 and runs_allowed == 0:
            reasons.append("complete-game shutout")
        if no_hitter >= 1 or (full_game and "h" in stats and hits_allowed == 0):
            reasons.append("no-hitter")
        if perfect_game >= 1 or (full_game and hits_allowed == 0 and walks == 0 and runs_allowed == 0 and "bb" in stats):
            reasons.append("perfect game")
        details.append(
            f"Pitching: IP {format_num(innings)}, K {format_num(strikeouts)}, "
            f"H {format_num(hits_allowed)}, ER {format_num(earned_runs)}"
        )

    unique = list(dict.fromkeys(reasons))
    if not unique:
        return None
    return " | ".join(d for d in details if d), unique


FOOTBALL_SECTION_HINTS = {
    "passing": ("passing",),
    "rushing": ("rushing",),
    "receiving": ("receiving", "receptions"),
    "tackles": ("tackles", "tackling"),
    "sacks": ("sacks",),
    "interceptions": ("interceptions",),
}

# Columns that mark a scoring/kicking summary table. These restate touchdowns
# already counted in the rushing and receiving tables, so scoring them would
# both double-report and, because the column is "TDs" rather than "TD",
# attribute a rusher's touchdowns to whichever category matched first.
FOOTBALL_SUMMARY_COLUMNS = {
    "tds", "tdpts", "totpts", "kickpts", "conv",
    "tdrush", "tdrec", "tdfr", "tdir", "tdpr", "tdkor",
}


def football_section_kind(section: str, header_set: Set[str]) -> str:
    """Classify a football stat table.

    Column names decide first: on MaxPreps each football category is its own
    table with a distinctive key column (C+Att, Car, Rec, Solo/Asst, Sacks),
    which is unambiguous. Section headings are only a fallback, because the
    nearest heading is often the enclosing <h2> ("Defense") shared by the
    tackles, sacks and interception tables.
    """
    # 'All Purpose Yards' and 'Total Yards' restate yardage already counted in
    # the rushing/receiving tables, so scoring them would double-report.
    if {"rush", "rec"} <= header_set or {"rush", "pass"} <= header_set:
        return ""
    if header_set & FOOTBALL_SUMMARY_COLUMNS:
        return ""
    if "all purpose" in section or "total yards" in section:
        return ""

    if "yds" in header_set:
        if "qbrate" in header_set or ({"c", "att"} <= header_set) or ({"comp", "att"} <= header_set):
            return "passing"
        if "car" in header_set or "carries" in header_set:
            return "rushing"
        if "rec" in header_set or "receptions" in header_set:
            return "receiving"
    if {"solo", "asst"} <= header_set or {"solo", "ast"} <= header_set or "tottckls" in header_set:
        return "tackles"
    if header_set & {"sacks", "sack", "sk"}:
        return "sacks"
    if header_set & {"int", "ints", "interceptions"} and not (header_set & {"c", "att", "comp"}):
        return "interceptions"

    # Heading text is only a fallback, and only for a table that actually
    # carries scoreable columns.
    if header_set & {"yds", "td", "solo", "asst", "tot", "tottckls", "sacks", "int"}:
        for kind, hints in FOOTBALL_SECTION_HINTS.items():
            if any(hint in section for hint in hints):
                return kind
    return ""


def eval_football(stats: Dict[str, float], norm_headers: Sequence[str], section: str) -> Evaluation:
    header_set = {h for h in norm_headers if h}
    kind = football_section_kind(section, header_set)
    if not kind:
        return None

    reasons: List[str] = []
    details: List[str] = []

    if kind == "passing":
        yds = within(stat_value(stats, ("yds", "passyds", "yards")), "passing_yards")
        tds = within(stat_value(stats, ("td", "tds", "passtd")), "touchdowns")
        comp = stat_value(stats, ("c", "comp", "cmp"))
        att = stat_value(stats, ("att", "attempts"))
        if yds >= FOOTBALL_TOP_THRESHOLDS["passing_yards"]:
            reasons.append(f"{format_num(yds)} pass yds")
        if tds >= FOOTBALL_TOP_THRESHOLDS["passing_td"]:
            reasons.append(f"{format_num(tds)} pass TD")
        details.append(
            f"Passing: {format_num(comp)}/{format_num(att)}, {format_num(yds)} yds, {format_num(tds)} TD"
        )
    elif kind == "rushing":
        yds = within(stat_value(stats, ("yds", "rushyds", "yards")), "rushing_yards")
        tds = within(stat_value(stats, ("td", "tds", "rushtd")), "touchdowns")
        car = stat_value(stats, ("car", "att", "carries"))
        if yds >= FOOTBALL_TOP_THRESHOLDS["rushing_yards"]:
            reasons.append(f"{format_num(yds)} rush yds")
        if tds >= FOOTBALL_TOP_THRESHOLDS["rushing_td"]:
            reasons.append(f"{format_num(tds)} rush TD")
        details.append(f"Rushing: {format_num(car)} car, {format_num(yds)} yds, {format_num(tds)} TD")
    elif kind == "receiving":
        yds = within(stat_value(stats, ("yds", "recyds", "yards")), "receiving_yards")
        tds = within(stat_value(stats, ("td", "tds", "rectd")), "touchdowns")
        rec = stat_value(stats, ("rec", "receptions"))
        if yds >= FOOTBALL_TOP_THRESHOLDS["receiving_yards"]:
            reasons.append(f"{format_num(yds)} rec yds")
        if tds >= FOOTBALL_TOP_THRESHOLDS["receiving_td"]:
            reasons.append(f"{format_num(tds)} rec TD")
        details.append(f"Receiving: {format_num(rec)} rec, {format_num(yds)} yds, {format_num(tds)} TD")
    elif kind == "tackles":
        total = stat_value(stats, ("tottckls", "tot", "totaltackles", "tackles", "tkl"))
        if total <= 0:
            total = stat_value(stats, ("solo", "sol")) + stat_value(stats, ("asst", "ast", "assisted"))
        total = within(total, "tackles")
        if total >= FOOTBALL_TOP_THRESHOLDS["tackles"]:
            reasons.append(f"{format_num(total)} tackles")
        details.append(f"Defense: {format_num(total)} tackles")
    elif kind == "sacks":
        sacks = within(stat_value(stats, ("sacks", "sack", "sk")), "sacks")
        if sacks >= FOOTBALL_TOP_THRESHOLDS["sacks"]:
            reasons.append(f"{format_num(sacks)} sacks")
        details.append(f"Defense: {format_num(sacks)} sacks")
    elif kind == "interceptions":
        picks = within(stat_value(stats, ("int", "ints", "interceptions")), "interceptions")
        if picks >= FOOTBALL_TOP_THRESHOLDS["interceptions"]:
            reasons.append(f"{format_num(picks)} INT")
        details.append(f"Defense: {format_num(picks)} INT")

    if not reasons:
        return None
    return " | ".join(details), reasons


VOLLEYBALL_SECTION_ORDER = ("attacking", "serving", "ball handling", "digging", "blocking")


def volleyball_section_kind(section: str, header_set: Set[str]) -> str:
    """Classify a volleyball stat table.

    MaxPreps splits volleyball into one table per skill, and the same short
    column letter means different things between them -- "A" is aces in the
    serving table but assists in ball handling. Reading columns without
    knowing the section is how a 2-ace night became "28.6 aces" (that column
    is ace percentage) and a 45-assist setter became a 45-ace server.
    """
    if "serve receiving" in section:
        return ""  # passing stats, nothing scoreable
    for kind in VOLLEYBALL_SECTION_ORDER:
        if kind in section:
            return kind
    if {"k", "att", "hit"} <= header_set:
        return "attacking"
    if {"sa", "se"} <= header_set:
        return "serving"
    if {"ast", "bha"} <= header_set:
        return "ball handling"
    if {"d", "de"} <= header_set:
        return "digging"
    if "totblks" in header_set or {"bs", "ba"} <= header_set:
        return "blocking"
    return ""


def eval_volleyball(stats: Dict[str, float], norm_headers: Sequence[str], section: str) -> Evaluation:
    header_set = {h for h in norm_headers if h}
    kind = volleyball_section_kind(section, header_set)
    if not kind:
        return None

    reasons: List[str] = []
    details: List[str] = []

    if kind == "attacking":
        kills = within(stat_value(stats, ("k", "kills")), "kills")
        if kills >= VOLLEYBALL_TOP_THRESHOLDS["kills"]:
            reasons.append(f"{format_num(kills)} kills")
        details.append(f"Kills {format_num(kills)}")
    elif kind == "serving":
        # "A" is the ace count. "Ace" is ace percentage and "SA" is serve
        # attempts -- neither is a countable ace.
        aces = stat_value(stats, ("a", "aces"))
        if aces >= VOLLEYBALL_TOP_THRESHOLDS["aces"]:
            reasons.append(f"{format_num(aces)} aces")
        details.append(f"Aces {format_num(aces)}")
    elif kind == "ball handling":
        assists = within(stat_value(stats, ("ast", "assists")), "vb_assists")
        if assists >= VOLLEYBALL_TOP_THRESHOLDS["assists"]:
            reasons.append(f"{format_num(assists)} assists")
        details.append(f"Assists {format_num(assists)}")
    elif kind == "digging":
        digs = within(stat_value(stats, ("d", "digs")), "digs")
        if digs >= VOLLEYBALL_TOP_THRESHOLDS["digs"]:
            reasons.append(f"{format_num(digs)} digs")
        details.append(f"Digs {format_num(digs)}")
    elif kind == "blocking":
        blocks = stat_value(stats, ("totblks", "totalblocks"))
        if blocks == 0:
            blocks = stat_value(stats, ("bs",)) + stat_value(stats, ("ba",))
        if blocks >= VOLLEYBALL_TOP_THRESHOLDS["blocks"]:
            reasons.append(f"{format_num(blocks)} blocks")
        details.append(f"Blocks {format_num(blocks)}")

    if not reasons:
        return None
    return " | ".join(details), reasons


def eval_soccer(stats: Dict[str, float], section: str) -> Evaluation:
    _ = section
    goals = stat_value(stats, ("g", "goals", "gls"))
    assists = stat_value(stats, ("a", "ast", "assists"))
    saves = stat_value(stats, ("sv", "saves"))
    goals_against = stat_value(stats, ("ga", "goalsagainst", "goalsallowed"))
    shutouts = stat_value(stats, ("sho", "shutout", "shutouts"))
    shutout = shutouts >= 1 or (saves >= 1 and goals_against == 0 and "ga" in stats)

    reasons: List[str] = []
    if goals >= SOCCER_TOP_THRESHOLDS["goals"]:
        reasons.append(f"{format_num(goals)} goals")
    if assists >= SOCCER_TOP_THRESHOLDS["assists"]:
        reasons.append(f"{format_num(assists)} assists")
    if shutout and saves >= SOCCER_TOP_THRESHOLDS["saves_with_shutout"]:
        reasons.append(f"shutout + {format_num(saves)} saves")
    if not reasons:
        return None
    line = (
        f"G {format_num(goals)}, A {format_num(assists)}, "
        f"SV {format_num(saves)}, GA {format_num(goals_against)}"
    )
    return line, reasons


def eval_lacrosse(stats: Dict[str, float], section: str) -> Evaluation:
    _ = section
    goals = stat_value(stats, ("g", "goals"))
    assists = stat_value(stats, ("a", "ast", "assists"))
    points = stat_value(stats, ("pts", "points"))
    if points == 0:
        points = goals + assists
    saves = stat_value(stats, ("sv", "saves"))

    reasons: List[str] = []
    if goals >= LACROSSE_TOP_THRESHOLDS["goals"]:
        reasons.append(f"{format_num(goals)} goals")
    if assists >= LACROSSE_TOP_THRESHOLDS["assists"]:
        reasons.append(f"{format_num(assists)} assists")
    if points >= LACROSSE_TOP_THRESHOLDS["points"]:
        reasons.append(f"{format_num(points)} points")
    if saves >= LACROSSE_TOP_THRESHOLDS["saves"]:
        reasons.append(f"{format_num(saves)} saves")
    if not reasons:
        return None
    line = f"G {format_num(goals)}, A {format_num(assists)}, PTS {format_num(points)}, SV {format_num(saves)}"
    return line, reasons


def evaluate_row(
    rules: str, row: Dict[str, object], norm_headers: Sequence[str], section: str
) -> Evaluation:
    stats = row.get("stats", {})
    if not isinstance(stats, dict):
        return None
    if rules == "basketball":
        return eval_basketball(stats, section)
    if rules == "baseball":
        return eval_baseball(stats, norm_headers, section)
    if rules == "football":
        return eval_football(stats, norm_headers, section)
    if rules == "volleyball":
        return eval_volleyball(stats, norm_headers, section)
    if rules == "soccer":
        return eval_soccer(stats, section)
    if rules == "lacrosse":
        return eval_lacrosse(stats, section)
    return None


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def merge_qualifier(bucket: Dict[str, Qualifier], item: Qualifier) -> None:
    """Combine a player's rows from several stat tables into one entry.

    Keyed on the contest, not on the resolved score, so a row whose team could
    not be resolved does not become a second copy of the same player.
    """
    key = item.ledger_key()
    existing = bucket.get(key)
    if existing is None:
        bucket[key] = item
        return
    reasons = list(dict.fromkeys(list(existing.reasons) + list(item.reasons)))
    details = [d for d in (existing.stat_line, item.stat_line) if d]
    merged_line = " | ".join(dict.fromkeys(" | ".join(details).split(" | ")))
    existing.reasons = tuple(reasons)
    existing.stat_line = merged_line
    if not existing.team and item.team:
        existing.team = item.team
    if not existing.opponent and item.opponent:
        existing.opponent = item.opponent
    if not existing.team_score and item.team_score:
        existing.team_score = item.team_score
    if not existing.opponent_score and item.opponent_score:
        existing.opponent_score = item.opponent_score


def display_line(item: Qualifier) -> str:
    return f"{item.stat_line} ({'; '.join(item.reasons)})"


# ---------------------------------------------------------------------------
# Playwright: game stats page
# ---------------------------------------------------------------------------


def goto_with_retry(page: Page, url: str, attempts: int = 2) -> None:
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logging.warning("Page load failed (%s/%s): %s", attempt, attempts, url)
            if attempt < attempts:
                page.wait_for_timeout(1000 * attempt)
    if last_exc:
        raise last_exc


def toggle_names_from_html(html: str) -> List[Tuple[str, bool]]:
    """Read the team switcher out of serialized HTML as (name, is_active).

    Reading the switcher from the captured HTML rather than from a live
    locator avoids a race: on some contest pages the locator resolves before
    the switcher paints and returns an empty label, which previously made the
    whole game look like a non-Mississippi team and dropped it.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: List[Tuple[str, bool]] = []
    for button in soup.find_all("button"):
        active = button.get("data-active")
        if active is None:
            continue
        label = normalize_text(button.get("aria-label") or button.get_text(" ", strip=True))
        if label and label not in {name for name, _ in out}:
            out.append((label, active == "true"))
    return out


def active_name_from_html(html: str) -> str:
    for name, active in toggle_names_from_html(html):
        if active:
            return name
    return ""


def collect_team_views(page: Page, game_url: str) -> List[Tuple[str, str]]:
    """Load the Stats tab and return (team_name, html) for each team.

    Player stats live under ?tab=Stats. There is no 'Box Score' tab on a
    MaxPreps contest page and .../box-score/ returns 404, so the old
    box-score hunt could never reach these tables.
    """
    stats_url = with_tab(game_url, "Stats")
    goto_with_retry(page, stats_url)
    try:
        page.wait_for_selector("table", timeout=12000)
    except Exception:  # noqa: BLE001
        logging.debug("No stat tables rendered (stats not entered yet?): %s", stats_url)
        return []

    html = page.content()
    names = [name for name, _ in toggle_names_from_html(html)]
    if len(names) < 2:
        return [(active_name_from_html(html) or (names[0] if names else ""), html)]

    views: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for name in names:
        try:
            if active_name_from_html(page.content()) != name:
                button = page.locator(f"button[data-active][aria-label={json.dumps(name)}]").first
                # Poll until the switch actually lands, and click again once if
                # it does not. A fixed sleep was sometimes too short, and the
                # page then yielded the previous team a second time -- which
                # silently dropped the other team, or worse, labelled that
                # team's stats with the wrong school.
                landed = False
                for attempt in range(2):
                    try:
                        button.scroll_into_view_if_needed(timeout=4000)
                    except Exception:  # noqa: BLE001
                        pass
                    button.click(timeout=8000)
                    for _ in range(24):
                        page.wait_for_timeout(250)
                        if active_name_from_html(page.content()) == name:
                            landed = True
                            break
                    if landed:
                        break
                    logging.debug("Team view %r did not switch (attempt %s)", name, attempt + 1)
                if not landed:
                    # Expected when a team has no stats: MaxPreps renders the
                    # button but leaves it inert.
                    logging.debug("Team view %r never became active on %s", name, stats_url)
                    continue
        except Exception:  # noqa: BLE001
            logging.debug("Could not switch to team view %r on %s", name, stats_url)
            continue
        current = page.content()
        resolved = active_name_from_html(current) or name
        if resolved in seen:
            continue
        seen.add(resolved)
        views.append((resolved, current))
    return views


def parse_team_view(
    config: SportConfig,
    team_name: str,
    html: str,
    meta: GameMeta,
    bucket: Dict[str, Qualifier],
    ms_players_only: bool = True,
) -> int:
    soup = BeautifulSoup(html, "html.parser")
    parsed_rows = 0
    resolved_team = match_team_name(team_name, meta.teams) or team_name
    if not resolved_team and len(meta.ms_teams) == 1 and len(meta.teams) == 1:
        resolved_team = next(iter(meta.ms_teams))
    opponent = next((t for t in meta.teams if normalize_key(t) != normalize_key(resolved_team)), "")

    # An MS-vs-out-of-state game belongs on the MS scoreboard, but the visiting
    # team's players do not belong in a Mississippi top-performers report.
    # Only filter when the team is actually known -- dropping an unidentified
    # view would silently lose real Mississippi performances.
    if ms_players_only and resolved_team and meta.ms_teams and resolved_team not in meta.ms_teams:
        logging.debug("Skipping non-MS team view %r in %s", resolved_team, meta.game_url)
        return 0

    for table in soup.find_all("table"):
        headers, norm_headers, rows = parse_table(table)
        if not headers or not rows:
            continue
        if not any(is_player_header_key(h) for h in norm_headers):
            continue
        section = table_section(table)

        for row in rows:
            player = row_player_name(norm_headers, row)
            if not player or is_non_player_row(player):
                continue
            parsed_rows += 1

            evaluation = evaluate_row(config.rules, row, norm_headers, section)
            if not evaluation:
                continue
            stat_line, reasons = evaluation

            merge_qualifier(
                bucket,
                Qualifier(
                    sport_key=config.key,
                    sport_label=config.label,
                    game_date=meta.game_date,
                    game_url=meta.game_url,
                    player_name=player,
                    team=resolved_team,
                    opponent=opponent,
                    team_score=meta.scores.get(resolved_team, ""),
                    opponent_score=meta.scores.get(opponent, ""),
                    stat_line=stat_line,
                    reasons=tuple(reasons),
                ),
            )
    return parsed_rows


def process_game(
    page: Page,
    config: SportConfig,
    game_url: str,
    metrics: Dict[str, int],
    ms_players_only: bool = True,
) -> List[Qualifier]:
    views = collect_team_views(page, game_url)
    if not views:
        metrics["games_no_stats"] += 1
        return []

    meta = parse_game_meta(views[0][1], BeautifulSoup(views[0][1], "html.parser"), game_url)
    if meta.teams and not meta.ms_teams:
        metrics["games_non_ms"] += 1
        return []

    bucket: Dict[str, Qualifier] = {}
    parsed = 0
    for team_name, html in views:
        parsed += parse_team_view(
            config, team_name, html, meta, bucket, ms_players_only=ms_players_only
        )
    metrics["players_parsed"] += parsed

    # A contest page can render a set/quarter line score with no player stats
    # behind it. That is "awaiting stats", not "has stats".
    if parsed:
        metrics["games_with_stats"] += 1
    else:
        metrics["games_no_stats"] += 1
    return list(bucket.values())


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def new_metrics() -> Dict[str, int]:
    return {
        "games_found": 0,
        "links_seen": 0,
        "wrong_date": 0,
        "non_ms_url": 0,
        "games_no_stats": 0,
        "games_non_ms": 0,
        "games_with_stats": 0,
        "players_parsed": 0,
        "players_qualified": 0,
    }


def process_sport_date(
    page: Page,
    config: SportConfig,
    day: date_cls,
    max_games: int,
    ms_players_only: bool = True,
) -> SportDateResult:
    date_param = maxpreps_date_param(day)
    result = SportDateResult(config=config, date_value=fmt_mdy(day), metrics=new_metrics())
    url = config.scoreboard_url(date_param)

    try:
        html = fetch_scoreboard(page, url)
    except Exception as exc:  # noqa: BLE001
        result.error = f"scoreboard fetch failed: {exc}"
        return result

    games, discovery = discover_games(html, day)
    result.metrics["links_seen"] = discovery["links_seen"]
    result.metrics["wrong_date"] = discovery["wrong_date"]
    result.metrics["non_ms_url"] = discovery["non_ms"]
    result.metrics["games_found"] = len(games)

    if max_games and len(games) > max_games:
        logging.warning(
            "%s %s: capping %s games at %s", config.key, result.date_value, len(games), max_games
        )
        games = games[:max_games]

    bucket: Dict[str, Qualifier] = {}
    for game_url in games:
        try:
            for item in process_game(
                page, config, game_url, result.metrics, ms_players_only=ms_players_only
            ):
                merge_qualifier(bucket, item)
        except Exception:  # noqa: BLE001
            logging.exception("Failed processing game: %s", game_url)
        random_delay()

    result.qualifiers = list(bucket.values())
    result.metrics["players_qualified"] = len(result.qualifiers)
    logging.info(
        "%s %s | games %s (skipped %s off-date, %s non-MS) | stats %s | rows %s | qualified %s",
        config.key,
        result.date_value,
        result.metrics["games_found"],
        result.metrics["wrong_date"],
        result.metrics["non_ms_url"],
        result.metrics["games_with_stats"],
        result.metrics["players_parsed"],
        result.metrics["players_qualified"],
    )
    return result


def login_if_needed(context: BrowserContext) -> None:
    email = os.getenv("MAXPREPS_EMAIL")
    password = os.getenv("MAXPREPS_PASSWORD")
    if not email or not password:
        raise RuntimeError("MAXPREPS_EMAIL and MAXPREPS_PASSWORD are required with --login.")

    page = context.new_page()
    try:
        goto_with_retry(page, f"{BASE_URL}/account/")
        if "login" not in page.url.lower() and page.locator("input[type='password']").count() == 0:
            logging.info("Using existing MaxPreps session.")
            return

        logging.info("Authenticating to MaxPreps.")
        goto_with_retry(page, LOGIN_URL)
        for selector in ("input[type='email']", "input[name='email']", "input[id*='email' i]"):
            if page.locator(selector).count() > 0:
                page.locator(selector).first.fill(email)
                break
        else:
            raise RuntimeError("Could not find MaxPreps email input.")

        for selector in ("input[type='password']", "input[name='password']", "input[id*='password' i]"):
            if page.locator(selector).count() > 0:
                page.locator(selector).first.fill(password)
                break
        else:
            raise RuntimeError("Could not find MaxPreps password input.")

        for selector in (
            "button[type='submit']",
            "button:has-text('Log In')",
            "button:has-text('Sign In')",
            "input[type='submit']",
        ):
            if page.locator(selector).count() > 0:
                page.locator(selector).first.click(timeout=10000)
                break
        else:
            raise RuntimeError("Could not submit MaxPreps login form.")

        page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        random_delay()
        if "login" in page.url.lower() and page.locator("input[type='password']").count() > 0:
            raise RuntimeError("MaxPreps login appears to have failed.")

        context.storage_state(path=str(STORAGE_STATE_PATH))
        logging.info("Saved session to %s", STORAGE_STATE_PATH)
    finally:
        page.close()


def run_scrape(
    window: Sequence[date_cls],
    configs: Sequence[SportConfig],
    headed: bool,
    use_login: bool,
    max_games: int,
    skip_offseason: bool,
    ms_players_only: bool,
) -> Tuple[List[Qualifier], List[SportDateResult]]:
    results: List[SportDateResult] = []
    qualifiers: List[Qualifier] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        context_kwargs = {"user_agent": USER_AGENT}
        if STORAGE_STATE_PATH.exists():
            context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
        context = browser.new_context(**context_kwargs)
        context.set_default_timeout(PAGE_TIMEOUT_MS)
        # Images and fonts are pure cost for a text scrape.
        context.route(
            re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4)(\?|$)", re.IGNORECASE),
            lambda route: route.abort(),
        )

        page = context.new_page()
        try:
            if use_login:
                login_if_needed(context)
            for config in configs:
                for day in window:
                    if skip_offseason and day.month not in config.season_months:
                        logging.debug("Skipping %s on %s (out of season)", config.key, day)
                        continue
                    result = process_sport_date(
                        page, config, day, max_games, ms_players_only=ms_players_only
                    )
                    results.append(result)
                    qualifiers.extend(result.qualifiers)
                    if result.error:
                        logging.error("%s %s: %s", config.key, result.date_value, result.error)
        finally:
            page.close()
            context.close()
            browser.close()

    merged: Dict[str, Qualifier] = {}
    for item in qualifiers:
        merge_qualifier(merged, item)
    return list(merged.values()), results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def build_body(
    reported: Sequence[Tuple[str, Qualifier]],
    window: Sequence[date_cls],
    results: Sequence[SportDateResult],
    suppressed: int,
    label: str = "",
) -> str:
    start, end = window[0], window[-1]
    today = today_central()
    if len(window) == 1:
        lag = (today - window[0]).days
        when = f"reported {fmt_mdy(today)}"
        if lag > 0:
            when += f", {lag} day(s) after the games"
        heading = f"TOP PERFORMERS – {window[0].strftime('%A %m/%d/%Y')}"
        if label:
            heading = f"{label} · {heading}"
        lines: List[str] = [heading, when, ""]
    else:
        lines = [
            f"DAILY TOP PERFORMERS – reported {fmt_mdy(today)}",
            f"Scan window: {fmt_mdy(start)} through {fmt_mdy(end)} ({len(window)} day(s))",
            "",
        ]

    if not reported:
        lines.append("No new players met stat thresholds in the scan window.")
        if suppressed:
            lines.append(f"({suppressed} qualifying performance(s) were already reported on an earlier run.)")
    else:
        by_date: Dict[str, List[Tuple[str, Qualifier]]] = {}
        for status, item in reported:
            by_date.setdefault(item.game_date, []).append((status, item))

        for game_date in sorted(by_date, key=lambda d: parse_mdy(d), reverse=True):
            played = parse_mdy(game_date)
            lag = (end - played).days
            heading = f"=== {played.strftime('%a %m/%d/%Y')} ==="
            if len(window) == 1:
                heading = ""
            elif lag > 0:
                heading = (
                    f"=== {played.strftime('%a %m/%d/%Y')} "
                    f"(LATE ENTRY – posted {lag} day(s) after the game) ==="
                )
            if heading:
                lines.extend([heading, ""])

            by_sport: Dict[str, List[Tuple[str, Qualifier]]] = {}
            for status, item in by_date[game_date]:
                by_sport.setdefault(item.sport_key, []).append((status, item))

            for config in SPORT_CONFIGS:
                items = by_sport.get(config.key)
                if not items:
                    continue
                lines.append(f"[{config.label}]")
                for status, item in sorted(
                    items, key=lambda pair: (pair[1].team.lower(), pair[1].player_name.lower())
                ):
                    team = item.team or "Unknown Team"
                    opponent = item.opponent or "Opponent"
                    suffix = "  (UPDATED STAT LINE)" if status == "updated" else ""
                    lines.append(f"{item.player_name} - {team}{suffix}")
                    lines.append(display_line(item))
                    if item.team_score or item.opponent_score:
                        lines.append(f"{team} {item.team_score}, {opponent} {item.opponent_score}")
                    else:
                        lines.append(f"{team} vs {opponent}")
                    lines.append(item.game_url)
                    lines.append("")
                lines.append("")

        if suppressed:
            lines.append(f"({suppressed} other qualifying performance(s) were already reported earlier.)")
            lines.append("")

    summary: List[str] = []
    for result in results:
        if result.error:
            summary.append(f"{result.config.key} {result.date_value}: ERROR – {result.error}")
            continue
        metrics = result.metrics
        if not metrics["games_found"] and not metrics["links_seen"]:
            continue
        summary.append(
            f"{result.config.key} {result.date_value}: "
            f"{metrics['games_found']} MS game(s) on date, "
            f"{metrics['games_with_stats']} with stats, "
            f"{metrics['games_no_stats']} awaiting stats, "
            f"{metrics['players_parsed']} rows, "
            f"{metrics['players_qualified']} qualified"
        )
    if summary:
        lines.extend(["", "--- RUN SUMMARY ---"])
        lines.extend(summary)

    pending = sum(r.metrics.get("games_no_stats", 0) for r in results)
    if pending:
        lines.append("")
        lines.append(
            f"{pending} game(s) in the window still have no box score entered; "
            "they will be re-checked on the next run."
        )
    return "\n".join(lines).strip()


def build_subject(
    test_mode: bool,
    reported: Sequence[Tuple[str, Qualifier]],
    window: Sequence[date_cls],
    label: str = "",
) -> str:
    end = fmt_mdy(window[-1])
    prefix = "TEST RUN – " if test_mode else ""
    if label:
        prefix += f"{label} – "
    # A single-day window is a report about that one date, not a daily digest.
    if len(window) == 1:
        headline = f"Top Performers – {window[0].strftime('%A %m/%d/%Y')}"
        empty = f"No Qualifying Performances – {window[0].strftime('%A %m/%d/%Y')}"
    else:
        headline = f"Daily Top Performers – {end}"
        empty = f"No New Qualifying Performances – {end}"
    if not reported:
        return f"{prefix}{empty}"
    fresh = sum(1 for status, _ in reported if status == "new")
    updated = len(reported) - fresh
    late = sum(1 for _, item in reported if parse_mdy(item.game_date) != window[-1])
    parts = []
    if fresh:
        parts.append(f"{fresh} new")
    if updated:
        parts.append(f"{updated} updated")
    if late:
        parts.append(f"{late} late")
    return f"{prefix}{headline} ({', '.join(parts)})"


def send_email(subject: str, body: str, attempts: int = 3) -> None:
    sender = os.getenv("GMAIL_SENDER")
    app_password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("GMAIL_RECIPIENT")
    if not sender or not app_password or not recipient:
        raise RuntimeError("GMAIL_SENDER, GMAIL_APP_PASSWORD, and GMAIL_RECIPIENT must be set.")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    ssl_context = ssl.create_default_context()
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl_context, timeout=60) as server:
                server.login(sender, app_password)
                server.send_message(msg)
            logging.info("Email sent: %s", subject)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logging.warning("Email send failed (%s/%s): %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Could not send email after {attempts} attempts") from last_exc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MaxPreps Mississippi top performer scraper")
    parser.add_argument("--date", help="MM/DD/YYYY end of the scan window (default: yesterday, America/Chicago)")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=(
            "How many days back to scan, ending at --date. Catches box scores "
            f"entered days after the game. Default {DEFAULT_LOOKBACK_DAYS}."
        ),
    )
    parser.add_argument("--sports", help="Comma-separated sport keys to run (default: all)")
    parser.add_argument(
        "--max-games",
        type=int,
        default=250,
        help="Safety cap on games per sport per date (0 disables the cap)",
    )
    parser.add_argument("--ledger", default=str(LEDGER_PATH), help="Path to the already-reported ledger")
    parser.add_argument(
        "--ignore-ledger",
        action="store_true",
        help="Report every qualifier found, even if it was emailed before (use for backfills)",
    )
    parser.add_argument("--no-ledger-write", action="store_true", help="Do not persist the ledger")
    parser.add_argument("--no-email", action="store_true", help="Print the report instead of emailing it")
    parser.add_argument(
        "--seed-ledger",
        action="store_true",
        help=(
            "Record everything currently found as already reported and send no "
            "email. Run this once on first deployment so the first real run "
            "does not email a backlog of older games."
        ),
    )
    parser.add_argument(
        "--only-when-new",
        action="store_true",
        help="Skip the email entirely when nothing new qualified",
    )
    parser.add_argument("--include-offseason", action="store_true", help="Do not skip sports out of season")
    parser.add_argument(
        "--include-out-of-state-players",
        action="store_true",
        help="Also report opposing players from other states in MS matchups",
    )
    parser.add_argument("--login", action="store_true", help="Authenticate to MaxPreps before scraping")
    parser.add_argument(
        "--label",
        default="",
        help="Text prefixed to the email subject, e.g. 'ON-DEMAND', to mark a manual run",
    )
    parser.add_argument("--test", action="store_true", help="Verbose logging and a TEST subject prefix")
    parser.add_argument("--headed", action="store_true", help="Run the browser headed for debugging")
    parser.add_argument("--list-sports", action="store_true", help="Print the configured sport keys and exit")
    return parser.parse_args(argv)


def configure_logging(test_mode: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if test_mode else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def select_configs(raw: Optional[str]) -> List[SportConfig]:
    if not raw:
        return list(SPORT_CONFIGS)
    wanted = [part.strip().lower() for part in raw.split(",") if part.strip()]
    unknown = [key for key in wanted if key not in SPORTS_BY_KEY]
    if unknown:
        raise ValueError(f"Unknown sport key(s): {', '.join(unknown)}. Known: {', '.join(SPORTS_BY_KEY)}")
    return [SPORTS_BY_KEY[key] for key in wanted]


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(args.test)

    if args.list_sports:
        for config in SPORT_CONFIGS:
            print(f"{config.key:18} {config.slug}/{config.gender:5} rules={config.rules}")
        return 0

    try:
        window = build_date_window(args.date, args.days)
        configs = select_configs(args.sports)
        logging.info(
            "Scan window (America/Chicago): %s .. %s | sports: %s",
            fmt_mdy(window[0]),
            fmt_mdy(window[-1]),
            ", ".join(c.key for c in configs),
        )

        ledger = Ledger(Path(args.ledger), enabled=not args.ignore_ledger)
        ledger.load()

        qualifiers, results = run_scrape(
            window=window,
            configs=configs,
            headed=args.headed,
            use_login=args.login,
            max_games=args.max_games,
            skip_offseason=not args.include_offseason,
            ms_players_only=not args.include_out_of_state_players,
        )

        reported: List[Tuple[str, Qualifier]] = []
        suppressed = 0
        for item in qualifiers:
            status = ledger.classify(item)
            if status is None:
                suppressed += 1
                continue
            reported.append((status, item))

        body = build_body(reported, window, results, suppressed, label=args.label)
        subject = build_subject(args.test, reported, window, label=args.label)

        if args.test or args.no_email:
            print("=" * 80)
            print(subject)
            print("=" * 80)
            print(body)
            print("=" * 80)

        emailed = False
        if args.seed_ledger:
            logging.info("--seed-ledger set; marking %s performer(s) as reported without emailing.", len(reported))
        elif args.no_email:
            logging.info("--no-email set; report not sent.")
        elif args.only_when_new and not reported:
            logging.info("Nothing new; --only-when-new suppressed the email.")
        else:
            send_email(subject, body)
            emailed = True

        # Only mark performers as reported once the email is actually out, so
        # an SMTP failure does not silently swallow a day of results.
        if (emailed or args.seed_ledger) and not args.no_ledger_write:
            for _, item in reported:
                ledger.record(item)
            pruned = ledger.prune(today_central())
            if pruned:
                logging.info("Pruned %s ledger entries older than %s days", pruned, LEDGER_RETENTION_DAYS)
            ledger.save()

        failures = [r for r in results if r.error]
        if failures:
            logging.warning("%s sport/date combination(s) failed; see the run summary.", len(failures))
        return 0
    except Exception:  # noqa: BLE001
        err = traceback.format_exc()
        logging.error("Script failure:\n%s", err)
        if not getattr(args, "no_email", False) and not getattr(args, "seed_ledger", False):
            try:
                send_email("ERROR – Top Performers Script Failed", err)
            except Exception:  # noqa: BLE001
                logging.exception("Failed to send error email.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
