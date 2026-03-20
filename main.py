from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from html import escape
import hashlib
import json
import os
import plistlib
from random import Random
import re
import sqlite3
from typing import Any
from urllib.parse import quote

import feedparser
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
}

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "underground_issue.db")
REFRESH_TTL_SECONDS = 15 * 60

BLOG_FEEDS = [
    ("A Closer Listen", "https://acloserlisten.com/feed/"),
    ("Headphone Commute", "https://headphonecommute.com/feed/"),
]

REDDIT_SOURCES = [
    ("r/ambientmusic", "https://www.reddit.com/r/ambientmusic/new.json?limit=20"),
    ("r/experimentalmusic", "https://www.reddit.com/r/experimentalmusic/new.json?limit=20"),
    ("r/bandcamp", "https://www.reddit.com/r/BandCamp/new.json?limit=20"),
]

BANDCAMP_FEEDS = [
    ("Astral Industries", "https://astralindustries.bandcamp.com/music?format=rss"),
    ("Mysteries of the Deep", "https://mysteriesofthedeep.bandcamp.com/music?format=rss"),
    ("Motion Ward", "https://motionward.bandcamp.com/music?format=rss"),
    ("West Mineral", "https://westmineral.bandcamp.com/music?format=rss"),
    ("Peak Oil", "https://peakoil.bandcamp.com/music?format=rss"),
    ("Efficient Space", "https://efficientspace.bandcamp.com/music?format=rss"),
    ("Latency", "https://latency.bandcamp.com/music?format=rss"),
    ("Further Records", "https://furtherrecords.bandcamp.com/music?format=rss"),
    ("A Strangely Isolated Place", "https://astrangelyisolatedplace.bandcamp.com/music?format=rss"),
    ("Mule Musiq", "https://mulemusiq.bandcamp.com/music?format=rss"),
    ("Touch", "https://touch33.bandcamp.com/music?format=rss"),
    ("Room40", "https://room40.bandcamp.com/music?format=rss"),
]

BANDCAMP_RE = re.compile(r"https?://[^\s\"'>]+bandcamp\.com[^\s\"'>]*", re.IGNORECASE)
EMBED_RE = re.compile(r"https?://bandcamp\.com/EmbeddedPlayer/[^\s\"'>]+", re.IGNORECASE)
TITLE_SPLIT_RE = re.compile(r"\s(?:[-|:]\s|by\s)", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

SOURCE_WEIGHTS = {
    "A Closer Listen": 1.4,
    "Headphone Commute": 2.0,
    "Astral Industries": 4.3,
    "Mysteries of the Deep": 3.8,
    "Motion Ward": 3.0,
    "West Mineral": 2.8,
    "Peak Oil": 3.3,
    "Efficient Space": 3.7,
    "Latency": 3.2,
    "Further Records": 2.9,
    "A Strangely Isolated Place": 2.6,
    "Mule Musiq": 3.6,
    "Touch": 1.8,
    "Room40": 1.6,
    "r/ambientmusic": 1.2,
    "r/experimentalmusic": 0.6,
    "r/bandcamp": 2.6,
}

GENERIC_POST_PATTERNS = [
    "recommendation",
    "recommendations",
    "what are you listening",
    "weekly thread",
    "monthly thread",
    "new music friday",
    "playlist",
    "top ambient",
]

BLOCKED_TITLE_PATTERNS = [
    "acl 2026 spring music preview",
    "spring music preview",
    "apuesta",
    "casino",
    "bet",
    "juеgos",
]

CATEGORY_KEYWORDS = {
    "dub-techno": [
        "dub techno",
        "echospace",
        "deepchord",
        "rod modell",
        "cv313",
        "basic channel",
        "chain reaction",
        "dub",
        "submerged",
    ],
    "drone": [
        "drone",
        "electroacoustic",
        "minimal",
        "lowercase",
        "longform",
        "sustained",
        "tone",
    ],
    "fourth-world": [
        "fourth world",
        "hassell",
        "organic",
        "tropical",
        "trumpet",
        "ritual",
    ],
    "minimal-groove": [
        "dozzy",
        "hypnotic",
        "pulse",
        "groove",
        "percussion",
        "polyrhythm",
        "dub house",
        "slow techno",
    ],
    "warm-electronic": [
        "carrier",
        "warm",
        "hazy",
        "lush",
        "melodic",
        "dubbed-out",
        "downtempo",
    ],
    "glacial": [
        "ambient",
        "glacial",
        "arctic",
        "ice",
        "frozen",
        "fog",
        "haze",
        "slow",
    ],
    "field": [
        "field recording",
        "field recordings",
        "environmental",
        "location recording",
        "tape",
        "outdoor",
    ],
}

DEFAULT_LIBRARY_PATHS = [
    os.environ.get("LIBRARY_XML_PATH", ""),
    os.path.join(DATA_DIR, "itunes_library.xml"),
    os.path.join(DATA_DIR, "iTunes Library.xml"),
]


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def get_db() -> sqlite3.Connection:
    ensure_data_dir()
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS candidates (
            item_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            summary TEXT NOT NULL,
            bandcamp_url TEXT NOT NULL,
            embed_url TEXT NOT NULL,
            artist_guess TEXT NOT NULL,
            album_guess TEXT NOT NULL,
            tags TEXT NOT NULL,
            owned INTEGER NOT NULL DEFAULT 0,
            source_score REAL NOT NULL DEFAULT 0,
            current_score REAL NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            show_count INTEGER NOT NULL DEFAULT 0,
            click_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            feedback TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feedback_item_id
        ON feedback(item_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    return connection


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return f"https:{url}"
    return url


def clean_text(value: str | None, limit: int = 240) -> str:
    if not value:
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}..."


def extract_bandcamp_url(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = BANDCAMP_RE.search(value)
        if match:
            return normalize_url(match.group(0))
    return None


def extract_bandcamp_embed(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = EMBED_RE.search(value)
        if match:
            return normalize_url(match.group(0))
    return None


def find_library_path() -> str | None:
    for path in DEFAULT_LIBRARY_PATHS:
        if path and os.path.exists(path):
            return path
    return None


@lru_cache(maxsize=1)
def load_library() -> dict[str, Any]:
    path = find_library_path()
    if not path:
        return {
            "path": "",
            "artists": set(),
            "albums": set(),
            "artist_albums": set(),
        }

    try:
        with open(path, "rb") as handle:
            payload = plistlib.load(handle)
        tracks = payload.get("Tracks", {})
    except Exception as exc:
        print(f"Library load failed for {path}: {exc}")
        return {
            "path": path,
            "artists": set(),
            "albums": set(),
            "artist_albums": set(),
        }

    artists: set[str] = set()
    albums: set[str] = set()
    artist_albums: set[str] = set()

    for track in tracks.values():
        artist = (track.get("Artist") or "").strip()
        album = (track.get("Album") or "").strip()
        if artist:
            artists.add(artist.lower())
        if album:
            albums.add(album.lower())
        if artist and album:
            artist_albums.add(f"{artist.lower()} -- {album.lower()}")

    return {
        "path": path,
        "artists": artists,
        "albums": albums,
        "artist_albums": artist_albums,
    }


def infer_artist_and_album(title: str, summary: str = "") -> tuple[str, str]:
    plain_title = clean_text(title, limit=180)
    summary = clean_text(summary, limit=180)

    artist = ""
    album = ""

    if " by " in plain_title.lower():
        left, right = re.split(r"\s+by\s+", plain_title, maxsplit=1, flags=re.IGNORECASE)
        album = left.strip(" -:|")
        artist = right.strip(" -:|")
    else:
        parts = TITLE_SPLIT_RE.split(plain_title, maxsplit=1)
        if len(parts) == 2:
            first, second = parts[0].strip(), parts[1].strip()
            if len(first.split()) <= 4:
                artist = first
                album = second
            else:
                album = first
                artist = second

    if not artist and summary:
        match = re.search(r"\bby\s+([A-Z][^.,;]{2,80})", summary)
        if match:
            artist = match.group(1).strip(" -:|")

    album = YEAR_RE.sub("", album).strip(" -:|")
    artist = YEAR_RE.sub("", artist).strip(" -:|")
    return artist[:120], album[:160]


def classify_item(title: str, summary: str) -> list[str]:
    haystack = f"{title} {summary}".lower()
    tags = []
    for tag, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            tags.append(tag)
    if not tags:
        tags.append("ambient")
    return tags


def is_generic_post(title: str) -> bool:
    lowered = title.lower()
    return any(pattern in lowered for pattern in GENERIC_POST_PATTERNS)


def is_blocked_title(title: str) -> bool:
    lowered = title.lower()
    return any(pattern in lowered for pattern in BLOCKED_TITLE_PATTERNS)


def get_bandcamp_embed(url: str | None) -> str | None:
    if not url:
        return None
    if "bandcamp.com/EmbeddedPlayer/" in url:
        return url
    try:
        response = requests.get(url, headers=HEADERS, timeout=4)
        response.raise_for_status()
        html = response.text
        soup = BeautifulSoup(response.text, "html.parser")

        iframe = soup.find("iframe")
        if iframe and iframe.get("src"):
            return normalize_url(iframe["src"])

        meta = soup.find("meta", attrs={"property": "og:video"})
        if meta and meta.get("content"):
            return normalize_url(meta["content"])

        props = soup.find("meta", attrs={"name": "bc-page-properties"})
        if props and props.get("content"):
            data = json.loads(props["content"])
            item_id = data.get("item_id")
            item_type = data.get("item_type")
            if item_id and item_type:
                player_key = "album" if item_type == "a" else "track"
                return (
                    f"https://bandcamp.com/EmbeddedPlayer/{player_key}={item_id}/"
                    "size=large/bgcol=0f1621/linkcol=83e0c1/tracklist=false/artwork=small/transparent=true/"
                )

        match = re.search(r'"item_type"\s*:\s*"([at])".*?"item_id"\s*:\s*(\d+)', html, re.DOTALL)
        if match:
            player_key = "album" if match.group(1) == "a" else "track"
            item_id = match.group(2)
            return (
                f"https://bandcamp.com/EmbeddedPlayer/{player_key}={item_id}/"
                "size=large/bgcol=0f1621/linkcol=83e0c1/tracklist=false/artwork=small/transparent=true/"
            )

        album_match = re.search(r'"album_id"\s*:\s*(\d+)', html)
        if album_match:
            return (
                f"https://bandcamp.com/EmbeddedPlayer/album={album_match.group(1)}/"
                "size=large/bgcol=0f1621/linkcol=83e0c1/tracklist=false/artwork=small/transparent=true/"
            )

        track_match = re.search(r'"track_id"\s*:\s*(\d+)', html)
        if track_match:
            return (
                f"https://bandcamp.com/EmbeddedPlayer/track={track_match.group(1)}/"
                "size=large/bgcol=0f1621/linkcol=83e0c1/tracklist=false/artwork=small/transparent=true/"
            )
    except Exception as exc:
        print(f"Bandcamp embed lookup failed for {url}: {exc}")
    return None


def search_bandcamp(query: str) -> str | None:
    try:
        response = requests.get(
            f"https://bandcamp.com/search?q={quote(query)}",
            headers=HEADERS,
            timeout=4,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        for link in soup.find_all("a", href=True):
            href = normalize_url(link["href"])
            if href and ("bandcamp.com/album/" in href or "bandcamp.com/track/" in href):
                return href
    except Exception as exc:
        print(f"Bandcamp search failed for {query}: {exc}")
    return None


def make_item_id(source: str, title: str, link: str) -> str:
    digest = hashlib.sha1(f"{source}|{title}|{link}".encode("utf-8")).hexdigest()
    return digest


def is_owned(artist_guess: str, album_guess: str, title: str) -> bool:
    library = load_library()
    artists = library["artists"]
    albums = library["albums"]
    artist_albums = library["artist_albums"]

    artist = artist_guess.strip().lower()
    album = album_guess.strip().lower()
    plain_title = title.strip().lower()

    if artist and artist in artists:
        if not album:
            return True
        if f"{artist} -- {album}" in artist_albums:
            return True
    if album and album in albums:
        return True
    if plain_title and plain_title in albums:
        return True
    return False


def record_candidate(item: dict[str, Any]) -> dict[str, int]:
    now = datetime.utcnow().isoformat()
    with get_db() as connection:
        connection.execute(
            """
            INSERT INTO candidates (
                item_id, source, title, link, summary, bandcamp_url, embed_url,
                artist_guess, album_guess, tags, owned, source_score, current_score,
                first_seen_at, last_seen_at, show_count, click_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            ON CONFLICT(item_id) DO UPDATE SET
                summary=excluded.summary,
                bandcamp_url=excluded.bandcamp_url,
                embed_url=excluded.embed_url,
                artist_guess=excluded.artist_guess,
                album_guess=excluded.album_guess,
                tags=excluded.tags,
                owned=excluded.owned,
                source_score=excluded.source_score,
                current_score=excluded.current_score,
                last_seen_at=excluded.last_seen_at
            """,
            (
                item["item_id"],
                item["source"],
                item["title"],
                item["link"],
                item["summary"],
                item["bandcamp_url"],
                item["embed_url"],
                item["artist_guess"],
                item["album_guess"],
                ",".join(item["tags"]),
                int(item["owned"]),
                float(item["source_score"]),
                float(item["current_score"]),
                now,
                now,
            ),
        )

        feedback_rows = connection.execute(
            """
            SELECT feedback, COUNT(*) AS count
            FROM feedback
            WHERE item_id = ?
            GROUP BY feedback
            """,
            (item["item_id"],),
        ).fetchall()

        stats = {"like": 0, "hide": 0, "weirder": 0, "owned": 0, "show_count": 0, "click_count": 0}
        for row in feedback_rows:
            stats[str(row["feedback"])] = int(row["count"])

        candidate_row = connection.execute(
            """
            SELECT show_count, click_count
            FROM candidates
            WHERE item_id = ?
            """,
            (item["item_id"],),
        ).fetchone()
        if candidate_row:
            stats["show_count"] = int(candidate_row["show_count"])
            stats["click_count"] = int(candidate_row["click_count"])

        return stats


def get_cached_candidate(item_id: str) -> sqlite3.Row | None:
    with get_db() as connection:
        return connection.execute(
            """
            SELECT bandcamp_url, embed_url, current_score, show_count, click_count
            FROM candidates
            WHERE item_id = ?
            """,
            (item_id,),
        ).fetchone()


def get_state(key: str) -> str:
    with get_db() as connection:
        row = connection.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row else ""


def set_state(key: str, value: str) -> None:
    with get_db() as connection:
        connection.execute(
            """
            INSERT INTO app_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def load_cached_items(limit: int = 60) -> list[dict[str, Any]]:
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT item_id, source, title, link, summary, bandcamp_url, embed_url,
                   artist_guess, album_guess, tags, owned, source_score,
                   current_score, show_count, click_count
            FROM candidates
            ORDER BY owned ASC, embed_url = '' ASC, current_score DESC, last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "item_id": str(row["item_id"]),
                "source": str(row["source"]),
                "title": str(row["title"]),
                "link": str(row["link"]),
                "summary": str(row["summary"]),
                "bandcamp_url": str(row["bandcamp_url"] or ""),
                "embed_url": str(row["embed_url"] or ""),
                "artist_guess": str(row["artist_guess"] or ""),
                "album_guess": str(row["album_guess"] or ""),
                "tags": [tag for tag in str(row["tags"] or "").split(",") if tag],
                "owned": bool(row["owned"]),
                "source_score": float(row["source_score"] or 0),
                "current_score": float(row["current_score"] or 0),
                "feedback": {
                    "show_count": int(row["show_count"] or 0),
                    "click_count": int(row["click_count"] or 0),
                    "like": 0,
                    "hide": 0,
                    "weirder": 0,
                    "owned": 0,
                },
            }
        )
    return items


def count_playable(items: list[dict[str, Any]]) -> int:
    return sum(1 for item in items if item.get("embed_url"))


def should_refresh_sources(force: bool = False) -> bool:
    if force:
        return True

    cached_items = load_cached_items(limit=12)
    cached_count = len(cached_items)
    if cached_count < 10:
        return True
    if count_playable(cached_items) < 6:
        return True

    last_refresh = get_state("last_refresh_at")
    if not last_refresh:
        return True

    try:
        refreshed_at = datetime.fromisoformat(last_refresh)
    except ValueError:
        return True

    age = (datetime.utcnow() - refreshed_at).total_seconds()
    return age >= REFRESH_TTL_SECONDS


def score_item(item: dict[str, Any], stats: dict[str, int]) -> float:
    score = float(item["source_score"])
    tags = item["tags"]
    title = item["title"].lower()
    summary = item["summary"].lower()

    if item["bandcamp_url"]:
        score += 1.1
    if item["embed_url"]:
        score += 0.8
    if item["owned"]:
        score -= 8.0

    if "dub-techno" in tags:
        score += 2.8
    if "minimal-groove" in tags:
        score += 2.2
    if "warm-electronic" in tags:
        score += 1.8
    if "fourth-world" in tags:
        score += 1.7
    if "drone" in tags:
        score += 0.2
    if "glacial" in tags:
        score -= 0.4
    if "field" in tags:
        score += 0.2

    if any(word in title for word in ("ambient", "drone", "meditation", "sleep")):
        score -= 1.2
    if any(word in title for word in ("dub", "groove", "rhythm", "pulse", "percussion")):
        score += 0.9

    if any(word in title for word in ("premiere", "debut", "new release", "recent release")):
        score += 0.8
    if "review" in title or "review" in summary:
        score += 0.5
    if len(item["summary"]) > 110:
        score += 0.4

    score += stats.get("like", 0) * 2.5
    score += stats.get("weirder", 0) * 1.4
    score -= stats.get("hide", 0) * 3.0
    score -= stats.get("owned", 0) * 10.0
    score -= min(stats.get("show_count", 0) * 0.3, 2.1)
    score += min(stats.get("click_count", 0) * 0.35, 1.4)

    if not item["artist_guess"]:
        score -= 0.6
    if is_generic_post(item["title"]):
        score -= 4.0

    return round(score, 2)


def build_item(
    source: str,
    title: str,
    link: str,
    summary: str = "",
    bandcamp_url: str | None = None,
    embed_url: str | None = None,
    source_score: float = 0.0,
) -> dict[str, Any]:
    clean_summary = clean_text(summary)
    artist_guess, album_guess = infer_artist_and_album(title, clean_summary)
    bandcamp_url = normalize_url(bandcamp_url)
    link = normalize_url(link) or ""
    if not bandcamp_url and "bandcamp.com/" in link:
        bandcamp_url = link

    tags = classify_item(title, clean_summary)
    item = {
        "item_id": make_item_id(source, title.strip(), link),
        "source": source,
        "title": title.strip(),
        "link": link,
        "summary": clean_summary,
        "bandcamp_url": bandcamp_url or "",
        "embed_url": normalize_url(embed_url) or "",
        "artist_guess": artist_guess,
        "album_guess": album_guess,
        "tags": tags,
        "owned": is_owned(artist_guess, album_guess, title.strip()),
        "source_score": source_score,
        "current_score": source_score,
    }
    cached = get_cached_candidate(item["item_id"])
    if cached:
        item["bandcamp_url"] = item["bandcamp_url"] or str(cached["bandcamp_url"] or "")
        item["embed_url"] = str(cached["embed_url"] or "")
        if cached["show_count"] or cached["click_count"]:
            item["current_score"] = float(cached["current_score"] or source_score)
    stats = record_candidate(item)
    item["feedback"] = stats
    item["current_score"] = score_item(item, stats)
    with get_db() as connection:
        connection.execute(
            "UPDATE candidates SET current_score = ? WHERE item_id = ?",
            (float(item["current_score"]), item["item_id"]),
        )
    return item


def hydrate_item_media(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("embed_url"):
        return item

    bandcamp_url = str(item.get("bandcamp_url") or "")
    if not bandcamp_url:
        search_query = " ".join(
            part for part in [item["artist_guess"], item["album_guess"], item["title"]] if part
        )
        bandcamp_url = search_bandcamp(search_query) or ""

    embed_url = get_bandcamp_embed(bandcamp_url) if bandcamp_url else ""
    item["bandcamp_url"] = bandcamp_url
    item["embed_url"] = embed_url or ""
    item["current_score"] = score_item(item, item["feedback"])

    with get_db() as connection:
        connection.execute(
            """
            UPDATE candidates
            SET bandcamp_url = ?, embed_url = ?, current_score = ?
            WHERE item_id = ?
            """,
            (item["bandcamp_url"], item["embed_url"], float(item["current_score"]), item["item_id"]),
        )
    return item


def fetch_blog_items() -> list[dict[str, Any]]:
    return []


def fetch_bandcamp_feed_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source_name, feed_url in BANDCAMP_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:12]:
                title = entry.get("title", "").strip()
                link = normalize_url(entry.get("link", "").strip())
                summary = entry.get("summary", "") or entry.get("description", "")
                embed_url = extract_bandcamp_embed(summary, link)
                if not title or not link or is_blocked_title(title):
                    continue
                items.append(
                    build_item(
                        source=source_name,
                        title=title,
                        link=link,
                        summary=summary,
                        bandcamp_url=link,
                        embed_url=embed_url,
                        source_score=4.2,
                    )
                )
        except Exception as exc:
            print(f"Bandcamp feed failed for {source_name}: {exc}")
    return items


def fetch_reddit_items() -> list[dict[str, Any]]:
    return []


def pick_rotating_items(seed: str | None = None, force_refresh: bool = False) -> list[dict[str, Any]]:
    if not should_refresh_sources(force=force_refresh):
        items = load_cached_items()
        rng = Random(seed or datetime.utcnow().strftime("%Y-%m-%d-%H-%M-%S"))
        rng.shuffle(items)
        items.sort(key=lambda item: (bool(item["owned"]), not bool(item["embed_url"]), -float(item["current_score"])))
        return items

    cached_before_refresh = load_cached_items()
    pool = fetch_bandcamp_feed_items()
    unique: dict[tuple[str, str], dict[str, Any]] = {}

    for item in pool:
        key = (
            str(item.get("artist_guess", item["title"])).lower(),
            str(item.get("album_guess", item["title"])).lower(),
        )
        existing = unique.get(key)
        if not existing or float(item["current_score"]) > float(existing["current_score"]):
            unique[key] = item

    items = list(unique.values())
    rng = Random(seed or datetime.utcnow().strftime("%Y-%m-%d-%H-%M-%S"))
    rng.shuffle(items)
    items.sort(key=lambda item: (bool(item["owned"]), -float(item["current_score"])))
    hydration_limit = min(len(items), 30)
    hydrated = [hydrate_item_media(item) for item in items[:hydration_limit]]
    if hydration_limit:
        items[:hydration_limit] = hydrated
        items.sort(key=lambda item: (bool(item["owned"]), not bool(item["embed_url"]), -float(item["current_score"])))
    if count_playable(items) < 6 and count_playable(cached_before_refresh) > count_playable(items):
        items = cached_before_refresh
    set_state("last_refresh_at", datetime.utcnow().isoformat())
    return items


def track_impression(items: list[dict[str, Any]]) -> None:
    if not items:
        return
    with get_db() as connection:
        connection.executemany(
            "UPDATE candidates SET show_count = show_count + 1 WHERE item_id = ?",
            [(item["item_id"],) for item in items],
        )


def pick_section_items(
    items: list[dict[str, Any]],
    predicate,
    limit: int,
    seen: set[str],
) -> list[dict[str, Any]]:
    picked = []
    for item in items:
        item_key = str(item["item_id"])
        if item_key in seen:
            continue
        if not predicate(item):
            continue
        picked.append(item)
        seen.add(item_key)
        if len(picked) == limit:
            break
    return picked


def build_sections(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    sections = []

    fresh = pick_section_items(
        items,
        lambda item: not item["owned"] and float(item["current_score"]) >= 3.1,
        6,
        seen,
    )
    dub = pick_section_items(
        items,
        lambda item: not item["owned"] and "dub-techno" in item["tags"],
        4,
        seen,
    )
    drone = pick_section_items(
        items,
        lambda item: not item["owned"]
        and ("drone" in item["tags"] or "glacial" in item["tags"] or "field" in item["tags"]),
        4,
        seen,
    )
    deep_cuts = pick_section_items(
        items,
        lambda item: not item["owned"] and float(item["current_score"]) >= 3.4,
        6,
        seen,
    )
    near_matches = pick_section_items(
        items,
        lambda item: not item["owned"] and item["artist_guess"] and float(item["current_score"]) >= 2.8,
        4,
        seen,
    )

    if fresh:
        sections.append(
            {
                "title": "Fresh Finds",
                "description": "Recent pulls with the strongest overall signal after feedback and library filtering.",
                "items": fresh,
            }
        )
    if dub:
        sections.append(
            {
                "title": "Dub Techno Fog",
                "description": "Mistier records leaning toward echospace, chain reaction and submerged pulse.",
                "items": dub,
            }
        )
    if drone:
        sections.append(
            {
                "title": "Drone and Drift",
                "description": "Longform, glacial and field-tinged records from the current crawl.",
                "items": drone,
            }
        )
    if deep_cuts:
        sections.append(
            {
                "title": "Deep Cuts",
                "description": "The stranger fringe, pushed up when you mark things as weirder.",
                "items": deep_cuts,
            }
        )
    if near_matches:
        sections.append(
            {
                "title": "Closest To Your Library",
                "description": "Things the engine thinks sit near your collection without matching owned records.",
                "items": near_matches,
            }
        )

    return sections


def make_blurb(item: dict[str, Any]) -> str:
    tags = item["tags"]
    artist = item["artist_guess"] or "Unknown artist"
    album = item["album_guess"] or item["title"]
    source = item["source"]

    if "dub-techno" in tags:
        return f"{artist} sits in a dubby, spacious lane with more pulse than pure ambient drift."
    if "minimal-groove" in tags:
        return f"{album} leans hypnotic and rhythmic, closer to slow-burn Dozzy territory."
    if "warm-electronic" in tags:
        return f"{artist} lands warmer and more listenable, with haze and melody over austerity."
    if "fourth-world" in tags:
        return f"{album} has that open, organic fourth-world feel rather than sealed-off drone."
    if "field" in tags:
        return f"{album} folds field texture into the ambient frame without losing focus."
    return f"Surfaced via {source}, this stays in your zone but aims for something more musical and lived-in."


def select_showcase_items(items: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    playable = [item for item in items if item["embed_url"] and not item["owned"]]
    owned_playable = [item for item in items if item["embed_url"] and item["owned"]]

    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()

    for pool in (playable, owned_playable):
        for item in pool:
            if item["item_id"] in seen:
                continue
            chosen.append(item)
            seen.add(item["item_id"])
            if len(chosen) == limit:
                return chosen
    return chosen


def render_feedback_link(item_id: str, value: str, label: str) -> str:
    return f'<a href="/feedback?item_id={item_id}&value={value}" class="pill">{label}</a>'


def render_card(item: dict[str, Any]) -> str:
    title = escape(item["title"])
    source = escape(item["source"])
    bandcamp_url = escape(item["bandcamp_url"])
    embed_url = escape(item["embed_url"])
    artist_guess = escape(item["artist_guess"])
    album_guess = escape(item["album_guess"])
    item_id = escape(item["item_id"])
    blurb = escape(make_blurb(item))

    links = [f'<a href="/out/{item_id}" target="_blank" rel="noreferrer">open source</a>']
    if bandcamp_url:
        links.append(f'<a href="{bandcamp_url}" target="_blank" rel="noreferrer">bandcamp</a>')

    embed_html = ""
    if embed_url:
        embed_html = (
            f'<div class="embed-wrap"><iframe loading="lazy" src="{embed_url}"></iframe></div>'
        )

    feedback_html = " ".join(
        [
            render_feedback_link(item["item_id"], "like", "save"),
            render_feedback_link(item["item_id"], "weirder", "weirder"),
            render_feedback_link(item["item_id"], "hide", "skip"),
            render_feedback_link(item["item_id"], "owned", "owned"),
        ]
    )

    return f"""
    <article class="card">
        <div class="card-head">
          <div>
            <div class="eyebrow">{source}</div>
            <h2>{artist_guess or title}</h2>
            <div class="subhead">{album_guess or title}</div>
          </div>
          <div class="card-links">{' '.join(links)}</div>
        </div>
        {embed_html}
        <p class="blurb">{blurb}</p>
        <div class="feedback">{feedback_html}</div>
    </article>
    """


@app.get("/feedback")
def feedback(item_id: str, value: str) -> RedirectResponse:
    valid_values = {"like", "hide", "weirder", "owned"}
    if value not in valid_values:
        return RedirectResponse(url="/", status_code=303)

    with get_db() as connection:
        connection.execute(
            "INSERT INTO feedback (item_id, feedback, created_at) VALUES (?, ?, ?)",
            (item_id, value, datetime.utcnow().isoformat()),
        )
        if value == "owned":
            connection.execute(
                "UPDATE candidates SET owned = 1, current_score = current_score - 10 WHERE item_id = ?",
                (item_id,),
            )
    return RedirectResponse(url=f"/?seed={int(datetime.utcnow().timestamp())}", status_code=303)


@app.get("/out/{item_id}")
def outgoing(item_id: str) -> RedirectResponse:
    with get_db() as connection:
        row = connection.execute(
            "SELECT link FROM candidates WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row:
            connection.execute(
                "UPDATE candidates SET click_count = click_count + 1 WHERE item_id = ?",
                (item_id,),
            )
            return RedirectResponse(url=row["link"], status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> str:
    seed = request.query_params.get("seed")
    force_refresh = request.query_params.get("refresh") == "1"
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    picks = pick_rotating_items(seed=seed, force_refresh=force_refresh)
    showcase_items = select_showcase_items(picks, limit=10)
    if count_playable(showcase_items) < 8 and not force_refresh:
        picks = pick_rotating_items(seed=seed, force_refresh=True)
        showcase_items = select_showcase_items(picks, limit=10)
    track_impression(showcase_items)

    library_path = find_library_path()
    filtered_count = sum(1 for item in picks if bool(item["owned"]))
    card_html = "".join(render_card(item) for item in showcase_items)
    empty_html = """
      <section class="empty">
        <p>No playable Bandcamp finds yet. Hit refresh to force a new crawl.</p>
      </section>
    """ if not showcase_items else ""
    library_note = (
        f"Library filter active from {escape(os.path.basename(library_path))}."
        if library_path
        else "Library filter inactive. Add your exported iTunes XML at data/itunes_library.xml or set LIBRARY_XML_PATH on Render."
    )

    return f"""
    <html>
      <head>
        <title>Underground Issue</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
          :root {{
            --bg: #0b0f14;
            --panel: rgba(18, 24, 32, 0.78);
            --panel-strong: rgba(24, 31, 42, 0.94);
            --ink: #eef3f8;
            --muted: #9eacbc;
            --edge: rgba(255, 255, 255, 0.08);
            --accent: #83e0c1;
            --accent-2: #7ba7ff;
          }}
          * {{
            box-sizing: border-box;
          }}
          body {{
            margin: 0;
            font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
            color: var(--ink);
            background:
              radial-gradient(circle at top left, rgba(123, 167, 255, 0.26), transparent 28%),
              radial-gradient(circle at 85% 10%, rgba(131, 224, 193, 0.18), transparent 22%),
              linear-gradient(180deg, #091018 0%, #0f1621 52%, #0b0f14 100%);
            min-height: 100vh;
          }}
          .shell {{
            max-width: 1380px;
            margin: 0 auto;
            padding: 28px 18px 56px;
          }}
          .toolbar {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 12px;
            margin-bottom: 18px;
          }}
          .button {{
            appearance: none;
            border: 0;
            border-radius: 999px;
            background: linear-gradient(135deg, var(--accent), var(--accent-2));
            color: #081018;
            padding: 11px 18px;
            font: inherit;
            font-weight: 600;
            cursor: pointer;
          }}
          .button:hover {{
            filter: brightness(1.06);
          }}
          .stamp {{
            color: var(--muted);
            font-size: 0.85rem;
          }}
          .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 20px;
            margin-top: 22px;
          }}
          .card {{
            background: linear-gradient(180deg, var(--panel) 0%, var(--panel-strong) 100%);
            border: 1px solid var(--edge);
            border-radius: 24px;
            padding: 18px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.22);
          }}
          .card-head {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 14px;
            margin-bottom: 14px;
          }}
          .eyebrow {{
            color: var(--accent);
            font-size: 0.72rem;
            text-transform: uppercase;
            letter-spacing: 0.16em;
            margin-bottom: 8px;
          }}
          h2 {{
            margin: 0;
            font-size: 1.22rem;
            line-height: 1.08;
            letter-spacing: -0.03em;
            color: var(--ink);
          }}
          .subhead {{
            margin-top: 6px;
            color: var(--muted);
            font-size: 0.92rem;
          }}
          .blurb {{
            color: var(--muted);
            line-height: 1.45;
            font-size: 0.94rem;
            margin: 14px 0 0;
          }}
          .card-links, .feedback {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
          }}
          a {{
            color: var(--ink);
            text-decoration: none;
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 999px;
            padding: 8px 12px;
            font-size: 0.83rem;
          }}
          a:hover {{
            color: var(--accent);
            border-color: rgba(131, 224, 193, 0.38);
            background: rgba(255, 255, 255, 0.03);
          }}
          .embed-wrap {{
            overflow: hidden;
            border-radius: 18px;
            background: rgba(255, 255, 255, 0.04);
          }}
          iframe {{
            width: 100%;
            height: 122px;
            border: 0;
            display: block;
          }}
          .feedback {{
            margin-top: 14px;
          }}
          .footer {{
            margin-top: 20px;
            color: var(--muted);
            font-size: 0.85rem;
          }}
          .empty {{
            margin-top: 24px;
            border: 1px solid var(--edge);
            border-radius: 24px;
            padding: 22px;
            color: var(--muted);
            background: linear-gradient(180deg, var(--panel) 0%, var(--panel-strong) 100%);
          }}
          @media (max-width: 640px) {{
            .shell {{
              padding: 20px 14px 48px;
            }}
            .card-head {{
              flex-direction: column;
            }}
            iframe {{
              height: 122px;
            }}
          }}
        </style>
      </head>
      <body>
        <main class="shell">
          <div class="toolbar">
            <button class="button" onclick="window.location='/?refresh=1&seed=' + Date.now()">Refresh 10 picks</button>
            <span class="stamp">Generated {generated_at}</span>
            <span class="stamp">{filtered_count} owned matches pushed down</span>
          </div>
          <section class="grid">
            {card_html}
          </section>
          {empty_html}
          <p class="footer">
            Sources: A Closer Listen, Headphone Commute, Fluid Radio, Boomkat,
            r/ambientmusic, r/experimentalmusic, r/BandCamp. {library_note}
          </p>
        </main>
      </body>
    </html>
    """
