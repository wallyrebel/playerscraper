#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import pytz
from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, Page, sync_playwright

BASE_URL = "https://www.maxpreps.com"
LOGIN_URL = f"{BASE_URL}/login/"
STORAGE_STATE_PATH = Path("storage_state.json")
TZ = pytz.timezone("America/Chicago")
PAGE_TIMEOUT_MS = 45000

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

SPORT_LABELS = {
    "baseball": "BASEBALL",
    "softball": "SOFTBALL",
    "basketball": "BASKETBALL",
    "soccer": "SOCCER",
    "football": "FOOTBALL",
    "volleyball": "VOLLEYBALL",
    "lacrosse": "LACROSSE",
    "golf": "GOLF",
    "track-field": "TRACK & FIELD",
}

PLAYER_HEADERS = {"player", "name", "athlete", "competitor", "golfer", "runner"}

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

VOLLEYBALL_TOP_THRESHOLDS = {
    "kills": 25,
    "assists": 45,
    "digs": 35,
    "aces": 10,
    "blocks": 8,
}


def is_player_header_key(norm_key: str) -> bool:
    if not norm_key:
        return False
    if norm_key in PLAYER_HEADERS:
        return True
    if norm_key.endswith("name"):
        return True
    return "athlete" in norm_key or "player" in norm_key


@dataclass
class GameMeta:
    teams: List[str] = field(default_factory=list)
    scores: Dict[str, str] = field(default_factory=dict)


@dataclass
class Qualifier:
    sport: str
    player_name: str
    team: str
    opponent: str
    team_score: str
    opponent_score: str
    stat_line: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MaxPreps Mississippi top performer scraper")
    parser.add_argument("--date", help="MM/DD/YYYY date override (defaults to yesterday in America/Chicago)")
    parser.add_argument("--test", action="store_true", help="Test mode with verbose logging and TEST subject")
    parser.add_argument("--headed", action="store_true", help="Run browser headed for debugging")
    return parser.parse_args()


def configure_logging(test_mode: bool) -> None:
    level = logging.DEBUG if test_mode else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(message)s")


def target_date_str(date_override: Optional[str]) -> str:
    if date_override:
        try:
            dt = datetime.strptime(date_override, "%m/%d/%Y")
        except ValueError as exc:
            raise ValueError("--date must use MM/DD/YYYY") from exc
        return dt.strftime("%m/%d/%Y")
    yesterday = datetime.now(TZ) - timedelta(days=1)
    return yesterday.strftime("%m/%d/%Y")


def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def parse_number(value: str) -> float:
    text = normalize_text(value).replace(",", "")
    if not text or text in {"-", "--", "N/A", "n/a"}:
        return 0.0
    match = re.search(r"-?\d+(?:\.\d+)?", text)
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


def reason_count_from_stat_line(stat_line: str) -> int:
    match = re.search(r"\(([^)]*)\)", stat_line or "")
    if not match:
        return 0
    parts = [p.strip() for p in match.group(1).split(";") if p.strip()]
    return len(parts)


def canonical_url(url: str) -> str:
    parsed = urlparse(urljoin(BASE_URL, url))
    if "maxpreps.com" not in parsed.netloc.lower():
        return ""
    path = parsed.path.rstrip("/") or "/"
    clean = parsed._replace(path=path)
    return urlunparse(clean)


def random_delay() -> None:
    time.sleep(random.uniform(1.5, 3.0))


def goto_with_retry(page: Page, url: str, attempts: int = 2) -> None:
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            random_delay()
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logging.warning("Page load failed (%s/%s): %s", attempt, attempts, url)
            if attempt < attempts:
                page.wait_for_timeout(1000 * attempt)
    if last_exc:
        raise last_exc


def click_any(page: Page, selectors: Sequence[str]) -> bool:
    for selector in selectors:
        loc = page.locator(selector)
        if loc.count() == 0:
            continue
        try:
            loc.first.click(timeout=10000)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def dismiss_cookies(page: Page) -> None:
    click_any(
        page,
        (
            "button:has-text('Accept All')",
            "button:has-text('Accept all')",
            "button:has-text('I Accept')",
            "button:has-text('Accept')",
        ),
    )


def login_if_needed(context: BrowserContext) -> None:
    page = context.new_page()
    try:
        goto_with_retry(page, f"{BASE_URL}/account/")
        dismiss_cookies(page)
        logged_out = "login" in page.url.lower() or page.locator("input[type='password']").count() > 0
        if not logged_out:
            logging.info("Using existing MaxPreps session.")
            return

        email = os.getenv("MAXPREPS_EMAIL")
        password = os.getenv("MAXPREPS_PASSWORD")
        if not email or not password:
            raise RuntimeError("MAXPREPS_EMAIL and MAXPREPS_PASSWORD are required for login.")

        logging.info("Authenticating to MaxPreps.")
        goto_with_retry(page, LOGIN_URL)
        dismiss_cookies(page)

        email_ok = False
        for selector in ("input[type='email']", "input[name='email']", "input[id*='email' i]"):
            loc = page.locator(selector)
            if loc.count() > 0:
                loc.first.fill(email)
                email_ok = True
                break
        if not email_ok:
            raise RuntimeError("Could not find MaxPreps email input.")

        pass_ok = False
        for selector in ("input[type='password']", "input[name='password']", "input[id*='password' i]"):
            loc = page.locator(selector)
            if loc.count() > 0:
                loc.first.fill(password)
                pass_ok = True
                break
        if not pass_ok:
            raise RuntimeError("Could not find MaxPreps password input.")

        if not click_any(
            page,
            (
                "button[type='submit']",
                "button:has-text('Log In')",
                "button:has-text('Sign In')",
                "input[type='submit']",
            ),
        ):
            raise RuntimeError("Could not submit MaxPreps login form.")

        page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        random_delay()
        if "login" in page.url.lower() and page.locator("input[type='password']").count() > 0:
            raise RuntimeError("MaxPreps login appears to have failed.")

        context.storage_state(path=str(STORAGE_STATE_PATH))
        logging.info("Saved session to %s", STORAGE_STATE_PATH)
    finally:
        page.close()


def extract_game_links(scoreboard_html: str) -> List[str]:
    soup = BeautifulSoup(scoreboard_html, "html.parser")
    links: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        url = canonical_url(anchor.get("href", ""))
        if not url:
            continue
        path = urlparse(url).path.lower()
        if "/games/" in path or "/game/" in path or "box-score" in path:
            links.add(url)

    script_url_pattern = re.compile(r"https?://www\.maxpreps\.com[^\s\"'<>]+", re.IGNORECASE)
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text:
            continue
        for found in script_url_pattern.findall(text):
            url = canonical_url(found)
            if not url:
                continue
            path = urlparse(url).path.lower()
            if "/games/" in path or "/game/" in path or "box-score" in path:
                links.add(url)

    return sorted(links)


def open_box_score(page: Page, game_url: str) -> bool:
    def is_not_found_page(current_html: Optional[str] = None) -> bool:
        title = page.title().lower()
        if "out of bounds" in title or "page not found" in title or "404" in title:
            return True
        html = (current_html if current_html is not None else page.content()).lower()
        return "pageerror\": 404" in html or "out of bounds" in html

    goto_with_retry(page, game_url, attempts=2)
    current_html = page.content()
    if not is_not_found_page(current_html) and ("box-score" in page.url.lower() or "box score" in current_html.lower()):
        return True

    for attempt in range(1, 3):
        if click_any(
            page,
            (
                "a:has-text('Box Score')",
                "button:has-text('Box Score')",
                "[role='tab']:has-text('Box Score')",
                "[role='link']:has-text('Box Score')",
            ),
        ):
            page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            random_delay()
            current_html = page.content()
            if not is_not_found_page(current_html):
                return True
        if attempt < 2:
            goto_with_retry(page, game_url, attempts=2)

    base = game_url.rstrip("/")
    for candidate in (
        f"{base}/box-score",
        f"{base}/box-score/",
        f"{base}#tab=box-score&schoolid=",
        f"{base}#tab=box-score",
    ):
        try:
            goto_with_retry(page, candidate, attempts=2)
            current_html = page.content()
            if not is_not_found_page(current_html) and ("box-score" in page.url.lower() or "box score" in current_html.lower()):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def game_has_mississippi_context(game_html: str) -> bool:
    # MaxPreps contest payload includes a state code for the contest context.
    state_matches = re.findall(r'"state"\s*:\s*"([A-Z]{2})"', game_html)
    if state_matches:
        return "MS" in {value.upper() for value in state_matches}
    return False


def parse_game_meta(soup: BeautifulSoup) -> GameMeta:
    teams: List[str] = []
    scores: Dict[str, str] = {}

    def add_team(name: Optional[str], score: object = None) -> None:
        clean = normalize_text(name)
        if not clean:
            return
        if clean not in teams:
            teams.append(clean)
        if score is not None:
            score_text = normalize_text(str(score))
            if score_text:
                scores[clean] = score_text

    def flatten(obj: object) -> Iterable[dict]:
        if isinstance(obj, dict):
            yield obj
            graph = obj.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    yield from flatten(item)
        elif isinstance(obj, list):
            for item in obj:
                yield from flatten(item)

    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue

        for item in flatten(payload):
            item_type = str(item.get("@type", "")).lower()
            if "sportsevent" not in item_type and "event" not in item_type:
                continue

            home = item.get("homeTeam")
            away = item.get("awayTeam")
            if isinstance(home, dict):
                add_team(home.get("name"), home.get("score"))
            if isinstance(away, dict):
                add_team(away.get("name"), away.get("score"))

            for key in ("competitor", "competitors"):
                competitors = item.get(key)
                if isinstance(competitors, list):
                    for comp in competitors:
                        if isinstance(comp, dict):
                            add_team(comp.get("name"), comp.get("score"))

            match = re.search(r"(.+?)\s+vs\.?\s+(.+)", normalize_text(str(item.get("name", ""))), re.IGNORECASE)
            if match:
                add_team(match.group(1))
                add_team(match.group(2))

    if len(teams) < 2 and soup.title:
        title = normalize_text(soup.title.get_text(" ", strip=True))
        match = re.search(r"(.+?)\s+vs\.?\s+(.+?)(?:\s+-|\s+\||$)", title, re.IGNORECASE)
        if match:
            add_team(match.group(1))
            add_team(match.group(2))

    return GameMeta(teams=teams[:2], scores=scores)


def table_context(table) -> str:
    parts: List[str] = []
    caption = table.find("caption")
    if caption:
        parts.append(normalize_text(caption.get_text(" ", strip=True)))

    node = table
    for _ in range(6):
        node = node.find_previous(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "p", "div", "span"])
        if not node:
            break
        text = normalize_text(node.get_text(" ", strip=True))
        if text and len(text) <= 120:
            parts.append(text)
        if len(parts) >= 4:
            break

    seen: set[str] = set()
    final: List[str] = []
    for text in reversed(parts):
        key = text.lower()
        if key not in seen:
            seen.add(key)
            final.append(text)
    return " | ".join(final)


def parse_table(table) -> Tuple[List[str], List[Dict[str, object]]]:
    header_row = table.find("thead")
    if header_row:
        header_row = header_row.find("tr")
    else:
        header_row = table.find("tr")
    if not header_row:
        return [], []

    headers = [normalize_text(c.get_text(" ", strip=True)) for c in header_row.find_all(["th", "td"])]
    if len(headers) < 2:
        return [], []

    norm_headers = [normalize_header(h) for h in headers]
    header_sig = "|".join(norm_headers)
    out_rows: List[Dict[str, object]] = []

    for tr in table.find_all("tr"):
        cells = [normalize_text(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        if "|".join(normalize_header(v) for v in cells) == header_sig:
            continue
        if len(cells) < 2:
            continue
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        if len(cells) > len(headers):
            cells = cells[: len(headers)]

        raw: Dict[str, str] = {}
        numeric: Dict[str, float] = {}
        for h_norm, value in zip(norm_headers, cells):
            if not h_norm:
                continue
            raw[h_norm] = value
            numeric[h_norm] = parse_number(value)
        out_rows.append({"cells": cells, "raw": raw, "stats": numeric})

    return headers, out_rows


def structured_player_table(sport: str, headers: Sequence[str], context: str) -> bool:
    has_player = any(is_player_header_key(normalize_header(h)) for h in headers)
    if not has_player:
        return False

    norm_headers = {normalize_header(h) for h in headers if normalize_header(h)}
    if sport in {"baseball", "softball"}:
        batting_schema = (
            bool({"ab", "pa"} & norm_headers)
            and ("rbi" in norm_headers)
            and ("ip" not in norm_headers)
            and ("era" not in norm_headers)
        )
        pitching_schema = (
            bool({"ip", "era"} & norm_headers)
            and bool({"so", "k", "cg", "er", "nh", "pg"} & norm_headers)
            and ("rbi" not in norm_headers)
        )
        return batting_schema or pitching_schema
    if sport == "football":
        passing_schema = bool({"comp", "att", "yds", "td"} & norm_headers) and ("yds" in norm_headers)
        rushing_schema = bool({"car", "yds", "td"} & norm_headers) and ("yds" in norm_headers)
        receiving_schema = bool({"rec", "yds", "td"} & norm_headers)
        defensive_schema = bool({"tot", "solo", "ast"} & norm_headers) and bool(
            {"sack", "sacks", "int", "ints", "interceptions"} & norm_headers
        )
        return passing_schema or rushing_schema or receiving_schema or defensive_schema
    if sport == "volleyball":
        volleyball_schema = bool(
            {
                "k",
                "kills",
                "ast",
                "assists",
                "a",
                "aces",
                "d",
                "digs",
                "totblks",
                "totalblocks",
                "blocks",
                "ba",
                "bs",
            }
            & norm_headers
        )
        return volleyball_schema

    text = (context + " " + " ".join(headers)).lower()
    if "scoring summary" in text or "inning by inning" in text or "by quarter" in text:
        return False

    keywords = {
        "basketball": ("pts", "ast", "reb", "stl", "blk"),
        "baseball": ("batting", "pitching", "rbi", "hr", "hits"),
        "softball": ("batting", "pitching", "rbi", "hr", "hits"),
        "soccer": ("goals", "assists", "saves"),
        "football": ("passing", "rushing", "receiving", "tackles", "sacks", "interceptions"),
        "volleyball": ("kills", "assists", "digs", "aces", "blocks"),
        "lacrosse": ("goals", "assists", "saves"),
        "golf": ("score", "par", "medalist"),
        "track-field": ("place", "mark", "record"),
    }
    return any(k in text for k in keywords.get(sport, ()))


def stat_value(stats: Dict[str, float], aliases: Sequence[str], fuzzy: bool = True) -> float:
    norm_aliases = [normalize_header(alias) for alias in aliases]
    for alias in norm_aliases:
        if alias in stats:
            return stats[alias]
    if fuzzy:
        for alias in norm_aliases:
            for key, value in stats.items():
                if alias and (alias in key or key in alias):
                    return value
    return 0.0


def row_player_name(headers: Sequence[str], row: Dict[str, object]) -> str:
    raw = row.get("raw", {})
    if not isinstance(raw, dict):
        return ""
    for header in headers:
        key = normalize_header(header)
        if is_player_header_key(key):
            name = normalize_text(str(raw.get(key, "")))
            if name:
                return name
    cells = row.get("cells", [])
    if isinstance(cells, list) and cells:
        return normalize_text(str(cells[0]))
    return ""


def is_total_row(name: str) -> bool:
    low = name.strip().lower()
    if not low:
        return True
    return low in {"totals", "team totals", "total", "team"} or "totals" in low


def row_team(row: Dict[str, object], context: str, meta: GameMeta) -> str:
    raw = row.get("raw", {})
    if isinstance(raw, dict):
        for key in ("team", "school"):
            value = normalize_text(str(raw.get(key, "")))
            if value:
                return value
    context_low = context.lower()
    for team in meta.teams:
        if team and team.lower() in context_low:
            return team
    if len(meta.teams) == 1:
        return meta.teams[0]
    return ""


def row_opponent(team: str, meta: GameMeta) -> str:
    if len(meta.teams) != 2:
        return ""
    t1, t2 = meta.teams
    if team.lower() == t1.lower():
        return t2
    if team.lower() == t2.lower():
        return t1
    return ""


def score_pair(team: str, opponent: str, meta: GameMeta) -> Tuple[str, str]:
    return meta.scores.get(team, ""), meta.scores.get(opponent, "")


def eval_basketball(stats: Dict[str, float]) -> Optional[str]:
    pts = stat_value(stats, ("pts", "points"), fuzzy=False)
    reb = stat_value(stats, ("reb", "trb", "totalrebounds", "totreb"), fuzzy=False)
    ast = stat_value(stats, ("ast", "assists"), fuzzy=False)
    threes = stat_value(stats, ("3ptm", "3pm", "3fgm", "fg3m"), fuzzy=False)
    stl = stat_value(stats, ("stl", "steals"), fuzzy=False)
    blk = stat_value(stats, ("blk", "blocks"), fuzzy=False)

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
    return (
        f"PTS {format_num(pts)}, REB {format_num(reb)}, AST {format_num(ast)}, "
        f"3PM {format_num(threes)}, STL {format_num(stl)}, BLK {format_num(blk)} "
        f"({'; '.join(reasons)})"
    )


def eval_baseball_softball(stats: Dict[str, float], headers: Sequence[str]) -> Optional[str]:
    norm_headers = {normalize_header(h) for h in headers if normalize_header(h)}
    keys = set(stats.keys())
    is_batting = (
        bool({"ab", "pa"} & norm_headers)
        and ("rbi" in norm_headers)
        and ("ip" not in norm_headers)
        and ("era" not in norm_headers)
    )
    is_pitching = (
        bool({"ip", "era"} & norm_headers)
        and bool({"so", "k", "cg", "er", "nh", "pg"} & norm_headers)
        and ("rbi" not in norm_headers)
    )

    reasons: List[str] = []
    details: List[str] = []

    if is_batting:
        hits = stats.get("h", 0.0)
        rbi = stats.get("rbi", 0.0)
        hr = stats.get("hr", 0.0)
        doubles = stats.get("2b", 0.0)
        triples = stats.get("3b", 0.0)
        xbh = doubles + triples + hr
        ab = stats.get("ab", 0.0)

        # Sanity guard against malformed rows.
        if hits <= max(ab, hits + 0.01):
            if hits >= BASEBALL_SOFTBALL_TOP_THRESHOLDS["hits"]:
                reasons.append(f"{format_num(hits)} hits")
            if rbi >= BASEBALL_SOFTBALL_TOP_THRESHOLDS["rbi"]:
                reasons.append(f"{format_num(rbi)} RBI")
            if hr >= BASEBALL_SOFTBALL_TOP_THRESHOLDS["hr"]:
                reasons.append(f"{format_num(hr)} HR")
            if xbh >= BASEBALL_SOFTBALL_TOP_THRESHOLDS["xbh"]:
                reasons.append(f"{format_num(xbh)} XBH")

            details.append(f"Batting: H {format_num(hits)}, RBI {format_num(rbi)}, HR {format_num(hr)}, XBH {format_num(xbh)}")

    if is_pitching:
        strikeouts = stats.get("so", 0.0) or stats.get("k", 0.0)
        complete_game = stats.get("cg", 0.0)
        earned_runs = stats.get("er", 0.0)
        no_hitter = stats.get("nh", 0.0)
        perfect_game = stats.get("pg", 0.0)

        if strikeouts >= BASEBALL_SOFTBALL_TOP_THRESHOLDS["strikeouts"]:
            reasons.append(f"{format_num(strikeouts)} K")
        if complete_game >= 1 and earned_runs == 0:
            reasons.append("complete-game shutout")
        if no_hitter >= 1:
            reasons.append("no-hitter")
        if perfect_game >= 1:
            reasons.append("perfect game")

        details.append(f"Pitching: K {format_num(strikeouts)}, CG {format_num(complete_game)}, ER {format_num(earned_runs)}")

    unique_reasons = []
    for reason in reasons:
        if reason not in unique_reasons:
            unique_reasons.append(reason)

    if not unique_reasons:
        return None
    details_text = " | ".join(d for d in details if d)
    return f"{details_text} ({'; '.join(unique_reasons)})"


def eval_soccer(stats: Dict[str, float]) -> Optional[str]:
    goals = stat_value(stats, ("g", "goals"))
    assists = stat_value(stats, ("a", "assists"))
    saves = stat_value(stats, ("sv", "saves"))
    shutouts = stat_value(stats, ("sho", "shutout", "shutouts", "so"))
    goals_against = stat_value(stats, ("ga", "goalsagainst", "goalsallowed"))
    shutout = shutouts >= 1 or (saves >= 1 and goals_against == 0)

    reasons: List[str] = []
    if goals >= 2:
        reasons.append(f"{format_num(goals)} goals")
    if assists >= 3:
        reasons.append(f"{format_num(assists)} assists")
    if shutout and saves >= 8:
        reasons.append(f"shutout + {format_num(saves)} saves")
    if not reasons:
        return None
    return (
        f"G {format_num(goals)}, A {format_num(assists)}, SV {format_num(saves)}, "
        f"GA {format_num(goals_against)} ({'; '.join(reasons)})"
    )


def eval_football(stats: Dict[str, float], headers: Sequence[str], context: str) -> Optional[str]:
    _ = context
    norm_headers = {normalize_header(h) for h in headers if normalize_header(h)}

    def cap(value: float, max_allowed: float) -> float:
        if value < 0 or value > max_allowed:
            return 0.0
        return value

    reasons: List[str] = []
    details: List[str] = []

    is_passing = "comp" in norm_headers and "att" in norm_headers and "yds" in norm_headers
    is_rushing = "car" in norm_headers and "yds" in norm_headers and "td" in norm_headers
    is_receiving = ("rec" in norm_headers or "receptions" in norm_headers) and "yds" in norm_headers and "td" in norm_headers
    is_defense = bool({"tot", "solo", "ast"} & norm_headers) and bool(
        {"sack", "sacks", "int", "ints", "interceptions"} & norm_headers
    )

    if is_passing:
        pass_yds = cap(stats.get("yds", 0.0), 700.0)
        pass_td = cap(stats.get("td", 0.0), 10.0)
        if pass_yds >= FOOTBALL_TOP_THRESHOLDS["passing_yards"]:
            reasons.append(f"{format_num(pass_yds)} pass yds")
        if pass_td >= FOOTBALL_TOP_THRESHOLDS["passing_td"]:
            reasons.append(f"{format_num(pass_td)} pass TD")
        details.append(f"PassYds {format_num(pass_yds)}, PassTD {format_num(pass_td)}")

    if is_rushing:
        rush_yds = cap(stats.get("yds", 0.0), 500.0)
        rush_td = cap(stats.get("td", 0.0), 10.0)
        if rush_yds >= FOOTBALL_TOP_THRESHOLDS["rushing_yards"]:
            reasons.append(f"{format_num(rush_yds)} rush yds")
        if rush_td >= FOOTBALL_TOP_THRESHOLDS["rushing_td"]:
            reasons.append(f"{format_num(rush_td)} rush TD")
        details.append(f"RushYds {format_num(rush_yds)}, RushTD {format_num(rush_td)}")

    if is_receiving:
        rec_yds = cap(stats.get("yds", 0.0), 500.0)
        rec_td = cap(stats.get("td", 0.0), 10.0)
        if rec_yds >= FOOTBALL_TOP_THRESHOLDS["receiving_yards"]:
            reasons.append(f"{format_num(rec_yds)} rec yds")
        if rec_td >= FOOTBALL_TOP_THRESHOLDS["receiving_td"]:
            reasons.append(f"{format_num(rec_td)} rec TD")
        details.append(f"RecYds {format_num(rec_yds)}, RecTD {format_num(rec_td)}")

    if is_defense:
        tackles = cap(stats.get("tot", 0.0) or stats.get("totaltackles", 0.0) or (stats.get("solo", 0.0) + stats.get("ast", 0.0)), 40.0)
        sacks = cap(stats.get("sack", 0.0) or stats.get("sacks", 0.0), 10.0)
        picks = cap(stats.get("int", 0.0) or stats.get("ints", 0.0) or stats.get("interceptions", 0.0), 6.0)
        if tackles >= FOOTBALL_TOP_THRESHOLDS["tackles"]:
            reasons.append(f"{format_num(tackles)} tackles")
        if sacks >= FOOTBALL_TOP_THRESHOLDS["sacks"]:
            reasons.append(f"{format_num(sacks)} sacks")
        if picks >= FOOTBALL_TOP_THRESHOLDS["interceptions"]:
            reasons.append(f"{format_num(picks)} INT")
        details.append(f"Tackles {format_num(tackles)}, Sacks {format_num(sacks)}, INT {format_num(picks)}")

    unique_reasons: List[str] = []
    for reason in reasons:
        if reason not in unique_reasons:
            unique_reasons.append(reason)

    if not unique_reasons:
        return None
    return f"{' | '.join(details)} ({'; '.join(unique_reasons)})"


def eval_volleyball(stats: Dict[str, float]) -> Optional[str]:
    kills = stat_value(stats, ("kills", "k"), fuzzy=False)
    assists = stat_value(stats, ("assists", "ast"), fuzzy=False)
    digs = stat_value(stats, ("digs", "dig", "d"), fuzzy=False)
    aces = stat_value(stats, ("aces", "a"), fuzzy=False)
    if aces == 0:
        aces = stat_value(stats, ("sa",), fuzzy=False)
    blocks = stat_value(stats, ("blocks", "blk", "totalblocks", "totblks"), fuzzy=False)
    if blocks == 0:
        blocks = (stat_value(stats, ("bs",), fuzzy=False) + stat_value(stats, ("ba",), fuzzy=False))

    reasons: List[str] = []
    if kills >= VOLLEYBALL_TOP_THRESHOLDS["kills"]:
        reasons.append(f"{format_num(kills)} kills")
    if assists >= VOLLEYBALL_TOP_THRESHOLDS["assists"]:
        reasons.append(f"{format_num(assists)} assists")
    if digs >= VOLLEYBALL_TOP_THRESHOLDS["digs"]:
        reasons.append(f"{format_num(digs)} digs")
    if aces >= VOLLEYBALL_TOP_THRESHOLDS["aces"]:
        reasons.append(f"{format_num(aces)} aces")
    if blocks >= VOLLEYBALL_TOP_THRESHOLDS["blocks"]:
        reasons.append(f"{format_num(blocks)} blocks")
    if not reasons:
        return None
    return (
        f"Kills {format_num(kills)}, Assists {format_num(assists)}, Digs {format_num(digs)}, "
        f"Aces {format_num(aces)}, Blocks {format_num(blocks)} ({'; '.join(reasons)})"
    )


def eval_lacrosse(stats: Dict[str, float]) -> Optional[str]:
    _ = stats
    return None


def eval_track_field(row: Dict[str, object], headers: Sequence[str]) -> Optional[str]:
    raw = row.get("raw", {})
    cells = row.get("cells", [])
    if not isinstance(raw, dict) or not isinstance(cells, list):
        return None

    place_text = ""
    for header in headers:
        key = normalize_header(header)
        if key in {"place", "rank", "position", "pl"}:
            place_text = normalize_text(str(raw.get(key, "")))
            break

    row_text = " ".join(str(c) for c in cells).lower()
    first_place = bool(re.search(r"\b1(st)?\b", place_text.lower()))
    meet_record = bool(re.search(r"\bmeet record\b|\bmr\b", row_text))
    state_mark = bool(re.search(r"\bstate qualifying\b|\bstate qualifier\b|\bstate qualify\b|\bsq\b", row_text))

    reasons: List[str] = []
    if first_place:
        reasons.append("1st place")
    if meet_record:
        reasons.append("meet record")
    if state_mark:
        reasons.append("state qualifying mark")
    if not reasons:
        return None
    return f"Track result: {'; '.join(reasons)}"


def eval_golf(row: Dict[str, object], stats: Dict[str, float]) -> Optional[str]:
    cells = row.get("cells", [])
    row_text = " ".join(str(c) for c in cells).lower() if isinstance(cells, list) else ""
    score = stat_value(stats, ("score", "strokes", "total"))
    par = stat_value(stats, ("par",))
    to_par = stat_value(stats, ("topar", "relpar", "plusminus", "vspar"))
    under_par = (to_par < 0) or (score > 0 and par > 0 and score < par)
    medalist = "medalist" in row_text

    reasons: List[str] = []
    if under_par:
        reasons.append("under par round")
    if medalist:
        reasons.append("match medalist")
    if not reasons:
        return None
    return f"Score {format_num(score)}, Par {format_num(par)}, ToPar {format_num(to_par)} ({'; '.join(reasons)})"


def evaluate_row(sport: str, row: Dict[str, object], headers: Sequence[str], context: str) -> Optional[str]:
    stats = row.get("stats", {})
    if not isinstance(stats, dict):
        return None

    if sport == "basketball":
        return eval_basketball(stats)
    if sport in {"baseball", "softball"}:
        return eval_baseball_softball(stats, headers)
    if sport == "soccer":
        return eval_soccer(stats)
    if sport == "football":
        return eval_football(stats, headers, context)
    if sport == "volleyball":
        return eval_volleyball(stats)
    if sport == "lacrosse":
        return eval_lacrosse(stats)
    if sport == "track-field":
        return eval_track_field(row, headers)
    if sport == "golf":
        return eval_golf(row, stats)
    return None


def merge_qualifier(
    bucket: Dict[Tuple[str, str, str, str, str, str], Qualifier],
    item: Qualifier,
) -> None:
    key = (
        item.sport.lower(),
        item.player_name.lower(),
        item.team.lower(),
        item.opponent.lower(),
        item.team_score,
        item.opponent_score,
    )
    if key not in bucket:
        bucket[key] = item
        return

    existing = bucket[key]
    existing_score = reason_count_from_stat_line(existing.stat_line)
    item_score = reason_count_from_stat_line(item.stat_line)

    if item_score > existing_score:
        existing.stat_line = item.stat_line
        return
    if item_score == existing_score and len(item.stat_line) > len(existing.stat_line):
        existing.stat_line = item.stat_line


def dedupe_qualifiers(items: Sequence[Qualifier]) -> List[Qualifier]:
    merged: Dict[Tuple[str, str, str, str, str, str], Qualifier] = {}
    for qualifier in items:
        merge_qualifier(merged, qualifier)
    return list(merged.values())


def parse_qualifiers(sport: str, html: str, meta: GameMeta) -> Tuple[List[Qualifier], int]:
    soup = BeautifulSoup(html, "html.parser")
    parsed_rows = 0
    found: Dict[Tuple[str, str, str, str, str, str], Qualifier] = {}

    for table in soup.find_all("table"):
        context = table_context(table)
        headers, rows = parse_table(table)
        if not headers or not rows:
            continue
        if not structured_player_table(sport, headers, context):
            continue

        for row in rows:
            player = row_player_name(headers, row)
            if not player or is_total_row(player):
                continue
            parsed_rows += 1

            stat_line = evaluate_row(sport, row, headers, context)
            if not stat_line:
                continue

            team = row_team(row, context, meta)
            opponent = row_opponent(team, meta)
            team_score, opponent_score = score_pair(team, opponent, meta)

            qualifier = Qualifier(
                sport=sport,
                player_name=player,
                team=team,
                opponent=opponent,
                team_score=team_score,
                opponent_score=opponent_score,
                stat_line=stat_line,
            )
            merge_qualifier(found, qualifier)

    return list(found.values()), parsed_rows


def process_sport(context: BrowserContext, sport: str, date_value: str) -> Tuple[List[Qualifier], Dict[str, int]]:
    url = f"{BASE_URL}/ms/{sport}/scores/?date={date_value}"
    page = context.new_page()
    metrics = {
        "games_found": 0,
        "games_skipped_no_box": 0,
        "games_skipped_non_ms": 0,
        "players_parsed": 0,
        "players_qualified": 0,
    }
    sport_qualifiers: List[Qualifier] = []

    try:
        goto_with_retry(page, url, attempts=2)
        game_links = list(dict.fromkeys(extract_game_links(page.content())))
        metrics["games_found"] = len(game_links)

        logging.info("Sport processed: %s", sport)
        logging.info("Games found [%s]: %s", sport, metrics["games_found"])

        for game_url in game_links:
            try:
                if not open_box_score(page, game_url):
                    metrics["games_skipped_no_box"] += 1
                    continue

                html = page.content()
                if not game_has_mississippi_context(html):
                    metrics["games_skipped_non_ms"] += 1
                    continue
                meta = parse_game_meta(BeautifulSoup(html, "html.parser"))
                qualifiers, parsed_rows = parse_qualifiers(sport, html, meta)
                metrics["players_parsed"] += parsed_rows
                metrics["players_qualified"] += len(qualifiers)
                sport_qualifiers.extend(qualifiers)
            except Exception:  # noqa: BLE001
                logging.exception("Failed processing game: %s", game_url)

        logging.info("Games skipped (no box score) [%s]: %s", sport, metrics["games_skipped_no_box"])
        logging.info("Games skipped (non-MS context) [%s]: %s", sport, metrics["games_skipped_non_ms"])
        logging.info("Players parsed [%s]: %s", sport, metrics["players_parsed"])
        logging.info("Players qualified [%s]: %s", sport, metrics["players_qualified"])
        return dedupe_qualifiers(sport_qualifiers), metrics
    finally:
        page.close()


def build_body(qualifiers: Sequence[Qualifier], date_value: str) -> str:
    if not qualifiers:
        return "No players met stat thresholds."

    grouped: Dict[str, List[Qualifier]] = {}
    for item in qualifiers:
        grouped.setdefault(item.sport, []).append(item)

    lines: List[str] = [f"DAILY TOP PERFORMERS – {date_value}", ""]
    for sport in SPORTS:
        items = grouped.get(sport, [])
        if not items:
            continue
        lines.append(f"[{SPORT_LABELS.get(sport, sport.upper())}]")
        for item in sorted(items, key=lambda q: (q.team.lower(), q.player_name.lower())):
            team = item.team or "Unknown Team"
            opponent = item.opponent or "Opponent"
            lines.append(f"{item.player_name} - {team}")
            lines.append(item.stat_line)
            if item.team_score or item.opponent_score:
                lines.append(f"{team} {item.team_score}, {opponent} {item.opponent_score}")
            else:
                lines.append(f"{team} vs {opponent}")
            lines.append("")
        lines.append("")
    return "\n".join(lines).strip()


def build_subject(test_mode: bool, date_value: str, has_qualifiers: bool) -> str:
    if test_mode:
        if has_qualifiers:
            return f"TEST RUN – Top Performers – {date_value}"
        return f"TEST RUN – Top Performers – {date_value}"
    if has_qualifiers:
        return f"Daily Top Performers – {date_value}"
    return f"No Qualifying Performances – {date_value}"


def send_email(subject: str, body: str) -> None:
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

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)

    logging.info("Email sent: %s", subject)


def run_scrape(date_value: str, headed: bool) -> List[Qualifier]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        if STORAGE_STATE_PATH.exists():
            context = browser.new_context(storage_state=str(STORAGE_STATE_PATH))
        else:
            context = browser.new_context()

        try:
            login_if_needed(context)
            qualifiers: List[Qualifier] = []
            for sport in SPORTS:
                sport_qualifiers, _ = process_sport(context, sport, date_value)
                qualifiers.extend(sport_qualifiers)
            return dedupe_qualifiers(qualifiers)
        finally:
            context.close()
            browser.close()


def main() -> int:
    args = parse_args()
    configure_logging(args.test)

    try:
        date_value = target_date_str(args.date)
        logging.info("Target date (America/Chicago): %s", date_value)
        qualifiers = run_scrape(date_value=date_value, headed=args.headed)

        body = build_body(qualifiers, date_value)
        subject = build_subject(args.test, date_value, bool(qualifiers))

        if args.test:
            print("=" * 80)
            print(subject)
            print("=" * 80)
            print(body)
            print("=" * 80)

        send_email(subject, body)
        return 0
    except Exception:  # noqa: BLE001
        err = traceback.format_exc()
        logging.error("Script failure:\n%s", err)
        try:
            send_email("ERROR – Top Performers Script Failed", err)
        except Exception:  # noqa: BLE001
            logging.exception("Failed to send error email.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
