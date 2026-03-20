from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from html import escape
import hashlib
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

BLOG_FEEDS = [
    ("A Closer Listen", "https://acloserlisten.com/feed/"),
    ("Headphone Commute", "https://headphonecommute.com/feed/"),
    ("Fluid Radio", "https://fluid-radio.co.uk/feed/"),
    ("Boomkat", "https://boomkat.com/feed"),
]

REDDIT_SOURCES = [
    ("r/ambientmusic", "https://www.reddit.com/r/ambientmusic/new.json?limit=20"),
    ("r/experimentalmusic", "https://www.reddit.com/r/experimentalmusic/new.json?limit=20"),
    ("r/bandcamp", "https://www.reddit.com/r/BandCamp/new.json?limit=20"),
]

BANDCAMP_RE = re.compile(r"https?://[^\s\"'>]+bandcamp\.com[^\s\"'>]*", re.IGNORECASE)
TITLE_SPLIT_RE = re.compile(r"\s(?:[-|:]\s|by\s)", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

SOURCE_WEIGHTS = {
    "A Closer Listen": 3.5,
    "Headphone Commute": 3.3,
    "Fluid Radio": 3.2,
    "Boomkat": 2.7,
    "r/ambientmusic": 2.2,
    "r/experimentalmusic": 2.1,
    "r/bandcamp": 2.0,
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


def get_bandcamp_embed(url: str | None) -> str | None:
    if not url:
        return None
    try:
        response = requests.get(url, headers=HEADERS, timeout=12)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        iframe = soup.find("iframe")
        if iframe and iframe.get("src"):
            return normalize_url(iframe["src"])

        meta = soup.find("meta", attrs={"property": "og:video"})
        if meta and meta.get("content"):
            return normalize_url(meta["content"])
    except Exception as exc:
        print(f"Bandcamp embed lookup failed for {url}: {exc}")
    return None


def search_bandcamp(query: str) -> str | None:
    try:
        response = requests.get(
            f"https://bandcamp.com/search?q={quote(query)}",
            headers=HEADERS,
            timeout=12,
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
        score += 2.0
    if "drone" in tags:
        score += 1.4
    if "glacial" in tags:
        score += 1.1
    if "field" in tags:
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
    source_score: float = 0.0,
) -> dict[str, Any]:
    clean_summary = clean_text(summary)
    artist_guess, album_guess = infer_artist_and_album(title, clean_summary)
    bandcamp_url = normalize_url(bandcamp_url)

    if not bandcamp_url:
        search_query = " ".join(
            part for part in [artist_guess, album_guess, title] if part
        )
        bandcamp_url = search_bandcamp(search_query)

    embed_url = get_bandcamp_embed(bandcamp_url) if bandcamp_url else None
    tags = classify_item(title, clean_summary)
    item = {
        "item_id": make_item_id(source, title.strip(), normalize_url(link) or ""),
        "source": source,
        "title": title.strip(),
        "link": normalize_url(link) or "",
        "summary": clean_summary,
        "bandcamp_url": bandcamp_url or "",
        "embed_url": embed_url or "",
        "artist_guess": artist_guess,
        "album_guess": album_guess,
        "tags": tags,
        "owned": is_owned(artist_guess, album_guess, title.strip()),
        "source_score": source_score,
        "current_score": source_score,
    }
    stats = record_candidate(item)
    item["feedback"] = stats
    item["current_score"] = score_item(item, stats)
    with get_db() as connection:
        connection.execute(
            "UPDATE candidates SET current_score = ? WHERE item_id = ?",
            (float(item["current_score"]), item["item_id"]),
        )
    return item


def fetch_blog_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source_name, feed_url in BLOG_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = entry.get("summary", "") or entry.get("description", "")
                bandcamp_url = extract_bandcamp_url(summary, link)
                if title and link:
                    items.append(
                        build_item(
                            source=source_name,
                            title=title,
                            link=link,
                            summary=summary,
                            bandcamp_url=bandcamp_url,
                            source_score=SOURCE_WEIGHTS.get(source_name, 1.0),
                        )
                    )
        except Exception as exc:
            print(f"Feed failed for {source_name}: {exc}")
    return items


def fetch_reddit_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source_name, url in REDDIT_SOURCES:
        try:
            response = requests.get(url, headers=HEADERS, timeout=12)
            response.raise_for_status()
            payload = response.json()
            children = payload.get("data", {}).get("children", [])

            for child in children:
                data = child.get("data", {})
                title = data.get("title", "").strip()
                if not title or is_generic_post(title):
                    continue
                post_url = normalize_url(data.get("url"))
                permalink = normalize_url(f"https://www.reddit.com{data.get('permalink', '')}")
                summary = data.get("selftext", "")
                bandcamp_url = extract_bandcamp_url(post_url, summary)
                link = post_url or permalink or ""
                reddit_score = float(data.get("score") or 0)

                if title and link:
                    items.append(
                        build_item(
                            source=source_name,
                            title=title,
                            link=link,
                            summary=summary,
                            bandcamp_url=bandcamp_url,
                            source_score=SOURCE_WEIGHTS.get(source_name, 1.0)
                            + min(reddit_score / 50.0, 1.4),
                        )
                    )
        except Exception as exc:
            print(f"Reddit fetch failed for {source_name}: {exc}")
    return items


def pick_rotating_items(seed: str | None = None) -> list[dict[str, Any]]:
    pool = fetch_blog_items() + fetch_reddit_items()
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


def render_feedback_link(item_id: str, value: str, label: str) -> str:
    return f'<a href="/feedback?item_id={item_id}&value={value}" class="pill">{label}</a>'


def render_card(item: dict[str, Any]) -> str:
    title = escape(item["title"])
    source = escape(item["source"])
    link = escape(item["link"])
    summary = escape(item["summary"])
    bandcamp_url = escape(item["bandcamp_url"])
    embed_url = escape(item["embed_url"])
    artist_guess = escape(item["artist_guess"])
    album_guess = escape(item["album_guess"])
    owned = bool(item["owned"])
    tags = " / ".join(str(tag) for tag in item["tags"])
    item_id = escape(item["item_id"])
    score = escape(str(item["current_score"]))

    links = [f'<a href="/out/{item_id}" target="_blank" rel="noreferrer">source</a>']
    if bandcamp_url:
        links.append(f'<a href="{bandcamp_url}" target="_blank" rel="noreferrer">bandcamp</a>')

    embed_html = ""
    if embed_url:
        embed_html = (
            f'<div class="embed-wrap"><iframe loading="lazy" src="{embed_url}"></iframe></div>'
        )

    summary_html = f"<p>{summary}</p>" if summary else ""
    guess_html = ""
    if artist_guess or album_guess:
        guess_html = (
            f'<div class="guess">{artist_guess or "Unknown artist"}'
            f'{(" · " + album_guess) if album_guess else ""}</div>'
        )
    owned_html = '<div class="owned">Already in library</div>' if owned else ""
    tags_html = f'<div class="tags">{escape(tags)}</div>' if tags else ""
    feedback_html = " ".join(
        [
            render_feedback_link(item["item_id"], "like", "Like"),
            render_feedback_link(item["item_id"], "weirder", "Weirder"),
            render_feedback_link(item["item_id"], "hide", "Too obvious"),
            render_feedback_link(item["item_id"], "owned", "Already have it"),
        ]
    )

    return f"""
    <article class="card">
        <div class="meta">{source} · score {score}</div>
        <h2>{title}</h2>
        {guess_html}
        {owned_html}
        {tags_html}
        {summary_html}
        <div class="links">{' '.join(links)}</div>
        <div class="feedback">{feedback_html}</div>
        {embed_html}
    </article>
    """


def render_section(section: dict[str, Any]) -> str:
    cards = "".join(render_card(item) for item in section["items"])
    return f"""
    <section class="section">
      <div class="section-head">
        <p class="section-kicker">Live section</p>
        <h2 class="section-title">{escape(section["title"])}</h2>
        <p class="section-copy">{escape(section["description"])}</p>
      </div>
      <div class="grid">
        {cards}
      </div>
    </section>
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
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    picks = pick_rotating_items(seed=seed)
    sections = build_sections(picks)
    shown_items = [item for section in sections for item in section["items"]]
    track_impression(shown_items)

    library_path = find_library_path()
    filtered_count = sum(1 for item in picks if bool(item["owned"]))
    db_note = os.path.basename(DB_PATH)
    section_html = "".join(render_section(section) for section in sections)
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
            --paper: rgba(255, 250, 242, 0.76);
            --ink: #201712;
            --muted: #68584a;
            --edge: rgba(32, 23, 18, 0.12);
            --accent: #b24c2b;
            --accent-soft: #d28f41;
          }}
          * {{
            box-sizing: border-box;
          }}
          body {{
            margin: 0;
            font-family: Georgia, "Times New Roman", serif;
            color: var(--ink);
            background:
              radial-gradient(circle at top left, rgba(210, 143, 65, 0.28), transparent 34%),
              radial-gradient(circle at top right, rgba(178, 76, 43, 0.14), transparent 28%),
              linear-gradient(180deg, #f4eee3 0%, #eadfcf 100%);
            min-height: 100vh;
          }}
          .shell {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 36px 18px 72px;
          }}
          .hero {{
            background: linear-gradient(135deg, rgba(255,255,255,0.55), rgba(255,255,255,0.2));
            border: 1px solid var(--edge);
            border-radius: 28px;
            padding: 28px;
            backdrop-filter: blur(10px);
            box-shadow: 0 22px 60px rgba(58, 36, 24, 0.12);
          }}
          .kicker {{
            text-transform: uppercase;
            letter-spacing: 0.18em;
            font-size: 0.72rem;
            color: var(--accent);
            margin: 0 0 12px;
          }}
          h1 {{
            margin: 0;
            font-size: clamp(2.8rem, 8vw, 5.6rem);
            line-height: 0.92;
            max-width: 8ch;
          }}
          .intro {{
            max-width: 48rem;
            font-size: 1.05rem;
            line-height: 1.6;
            color: var(--muted);
            margin-top: 18px;
          }}
          .toolbar {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 12px;
            margin-top: 22px;
          }}
          .button {{
            appearance: none;
            border: 0;
            border-radius: 999px;
            background: var(--ink);
            color: #f8f0e5;
            padding: 12px 18px;
            font: inherit;
            cursor: pointer;
          }}
          .button:hover {{
            background: var(--accent);
          }}
          .stamp {{
            color: var(--muted);
            font-size: 0.92rem;
          }}
          .section {{
            margin-top: 34px;
          }}
          .section-head {{
            margin-bottom: 10px;
          }}
          .section-kicker {{
            text-transform: uppercase;
            letter-spacing: 0.16em;
            font-size: 0.7rem;
            color: var(--accent);
            margin: 0 0 6px;
          }}
          .section-title {{
            margin: 0;
            font-size: clamp(1.6rem, 3vw, 2.4rem);
          }}
          .section-copy {{
            margin: 8px 0 0;
            max-width: 42rem;
            color: var(--muted);
          }}
          .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 18px;
            margin-top: 22px;
          }}
          .card {{
            background: var(--paper);
            border: 1px solid var(--edge);
            border-radius: 22px;
            padding: 18px;
            box-shadow: 0 18px 40px rgba(58, 36, 24, 0.08);
          }}
          .guess {{
            font-size: 0.96rem;
            margin-bottom: 10px;
            color: var(--ink);
          }}
          .owned {{
            display: inline-block;
            border-radius: 999px;
            padding: 5px 10px;
            font-size: 0.74rem;
            margin-bottom: 10px;
            background: rgba(32, 23, 18, 0.09);
            color: var(--muted);
          }}
          .tags {{
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--accent);
            margin-bottom: 12px;
          }}
          .meta {{
            color: var(--accent);
            font-size: 0.76rem;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            margin-bottom: 10px;
          }}
          h2 {{
            margin: 0 0 10px;
            font-size: 1.3rem;
            line-height: 1.15;
          }}
          p {{
            color: var(--muted);
            line-height: 1.55;
            margin: 0 0 14px;
          }}
          .links, .feedback {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-bottom: 14px;
          }}
          a {{
            color: var(--ink);
            text-decoration: none;
            border-bottom: 1px solid rgba(32, 23, 18, 0.35);
            padding-bottom: 2px;
          }}
          a:hover {{
            color: var(--accent);
            border-color: var(--accent);
          }}
          .pill {{
            border: 1px solid rgba(32, 23, 18, 0.16);
            border-radius: 999px;
            padding: 7px 11px;
            font-size: 0.84rem;
            background: rgba(255,255,255,0.3);
          }}
          .pill:hover {{
            background: rgba(178, 76, 43, 0.08);
          }}
          .embed-wrap {{
            overflow: hidden;
            border-radius: 16px;
            background: rgba(32, 23, 18, 0.05);
          }}
          iframe {{
            width: 100%;
            min-height: 440px;
            border: 0;
          }}
          .footer {{
            margin-top: 18px;
            color: var(--muted);
            font-size: 0.94rem;
          }}
          @media (max-width: 640px) {{
            .shell {{
              padding: 20px 14px 48px;
            }}
            .hero {{
              padding: 20px;
              border-radius: 22px;
            }}
            iframe {{
              min-height: 380px;
            }}
          }}
        </style>
      </head>
      <body>
        <main class="shell">
          <section class="hero">
            <p class="kicker">Adaptive feed</p>
            <h1>Underground Issue</h1>
            <p class="intro">
              A live recommendation engine for ambient, drone and submerged electronics.
              It crawls blogs and Reddit, tries to find the Bandcamp trail, filters against
              your library when available, remembers what it showed you, and learns from your feedback.
            </p>
            <div class="toolbar">
              <button class="button" onclick="window.location='/?seed=' + Date.now()">Refresh finds</button>
              <span class="stamp">Generated {generated_at}</span>
              <span class="stamp">{filtered_count} owned matches pushed down</span>
              <span class="stamp">Memory: {escape(db_note)}</span>
            </div>
          </section>
          {section_html}
          <p class="footer">
            Sources: A Closer Listen, Headphone Commute, Fluid Radio, Boomkat,
            r/ambientmusic, r/experimentalmusic, r/BandCamp. {library_note}
          </p>
        </main>
      </body>
    </html>
    """
