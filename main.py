from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from html import escape
import os
import plistlib
from random import Random
import re
from typing import Any

import feedparser
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

app = FastAPI()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
}

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")

BANDCAMP_FEEDS = [
    ("Astral Industries", "https://astralindustries.bandcamp.com/releases?format=rss"),
    ("Mysteries of the Deep", "https://mysteriesofthedeep.bandcamp.com/releases?format=rss"),
    ("West Mineral", "https://westmineral.bandcamp.com/releases?format=rss"),
    ("Peak Oil", "https://peakoil.bandcamp.com/releases?format=rss"),
    ("Efficient Space", "https://efficientspace.bandcamp.com/releases?format=rss"),
    ("Latency", "https://latency.bandcamp.com/releases?format=rss"),
    ("Further Records", "https://furtherrecords.bandcamp.com/releases?format=rss"),
    ("A Strangely Isolated Place", "https://astrangelyisolatedplace.bandcamp.com/releases?format=rss"),
    ("Mule Musiq", "https://mulemusiq.bandcamp.com/releases?format=rss"),
    ("Touch", "https://touch33.bandcamp.com/releases?format=rss"),
    ("Room40", "https://room40.bandcamp.com/releases?format=rss"),
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
    "fourth-world": [
        "fourth world",
        "hassell",
        "organic",
        "tropical",
        "trumpet",
        "ritual",
    ],
    "ambient": [
        "ambient",
        "drift",
        "atmospheric",
        "mist",
        "haze",
    ],
}

SOURCE_WEIGHTS = {
    "Astral Industries": 4.4,
    "Mysteries of the Deep": 3.9,
    "West Mineral": 3.0,
    "Peak Oil": 3.4,
    "Efficient Space": 3.8,
    "Latency": 3.3,
    "Further Records": 2.9,
    "A Strangely Isolated Place": 2.7,
    "Mule Musiq": 3.7,
    "Touch": 1.8,
    "Room40": 1.7,
}

DEFAULT_LIBRARY_PATHS = [
    os.environ.get("LIBRARY_XML_PATH", ""),
    os.path.join(DATA_DIR, "itunes_library.xml"),
    os.path.join(DATA_DIR, "iTunes Library.xml"),
]


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return f"https:{url}"
    return url


def clean_text(value: str | None, limit: int = 180) -> str:
    if not value:
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}..."


def is_blocked_title(title: str) -> bool:
    lowered = title.lower()
    return any(pattern in lowered for pattern in BLOCKED_TITLE_PATTERNS)


def find_library_path() -> str | None:
    for path in DEFAULT_LIBRARY_PATHS:
        if path and os.path.exists(path):
            return path
    return None


@lru_cache(maxsize=1)
def load_library() -> dict[str, set[str]]:
    path = find_library_path()
    if not path:
        return {"artists": set(), "albums": set(), "artist_albums": set()}

    try:
        with open(path, "rb") as handle:
            payload = plistlib.load(handle)
        tracks = payload.get("Tracks", {})
    except Exception:
        return {"artists": set(), "albums": set(), "artist_albums": set()}

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

    return {"artists": artists, "albums": albums, "artist_albums": artist_albums}


def infer_artist_and_album(title: str) -> tuple[str, str]:
    parts = re.split(r"\s[-:|]\s", title, maxsplit=1)
    if len(parts) == 2:
        first, second = parts[0].strip(), parts[1].strip()
        if len(first.split()) <= 4:
            return first[:120], second[:160]
    return "", title[:160]


def classify_item(title: str, summary: str) -> list[str]:
    haystack = f"{title} {summary}".lower()
    tags = [tag for tag, keywords in CATEGORY_KEYWORDS.items() if any(word in haystack for word in keywords)]
    return tags or ["ambient"]


def is_owned(artist: str, album: str, title: str) -> bool:
    library = load_library()
    artist_key = artist.lower().strip()
    album_key = album.lower().strip()
    title_key = title.lower().strip()

    if artist_key and artist_key in library["artists"]:
        if not album_key:
            return True
        if f"{artist_key} -- {album_key}" in library["artist_albums"]:
            return True
    if album_key and album_key in library["albums"]:
        return True
    if title_key and title_key in library["albums"]:
        return True
    return False


def make_blurb(item: dict[str, Any]) -> str:
    if "dub-techno" in item["tags"]:
        return "Dubby, spacious and more pulse-driven than straight ambient."
    if "minimal-groove" in item["tags"]:
        return "Hypnotic and rhythmic, closer to the slow-burn Dozzy side."
    if "warm-electronic" in item["tags"]:
        return "Warmer and more melodic, with haze instead of austerity."
    if "fourth-world" in item["tags"]:
        return "Organic and open, with more fourth-world feel than sealed-off drone."
    return "In your zone, but tilted toward the more listenable end of it."


def score_item(item: dict[str, Any]) -> float:
    score = SOURCE_WEIGHTS.get(item["source"], 1.0)
    title = item["title"].lower()
    tags = item["tags"]

    if "dub-techno" in tags:
        score += 2.8
    if "minimal-groove" in tags:
        score += 2.2
    if "warm-electronic" in tags:
        score += 1.8
    if "fourth-world" in tags:
        score += 1.7
    if "ambient" in tags:
        score -= 0.4

    if any(word in title for word in ("ambient", "drone", "meditation", "sleep")):
        score -= 1.2
    if any(word in title for word in ("dub", "groove", "rhythm", "pulse", "percussion")):
        score += 0.9
    if item["owned"]:
        score -= 6.0

    return score


def fetch_release_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source_name, feed_url in BANDCAMP_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:14]:
                title = (entry.get("title") or "").strip()
                link = normalize_url((entry.get("link") or "").strip())
                summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
                if not title or not link or is_blocked_title(title):
                    continue

                artist, album = infer_artist_and_album(title)
                item = {
                    "source": source_name,
                    "title": title,
                    "link": link,
                    "artist": artist,
                    "album": album or title,
                    "summary": summary,
                    "tags": classify_item(title, summary),
                }
                item["owned"] = is_owned(item["artist"], item["album"], item["title"])
                item["score"] = score_item(item)
                item["blurb"] = make_blurb(item)
                items.append(item)
        except Exception:
            continue
    return items


def pick_items(seed: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    items = fetch_release_items()
    unique: dict[tuple[str, str], dict[str, Any]] = {}

    for item in items:
        key = (item["artist"].lower(), item["album"].lower())
        if key not in unique or unique[key]["score"] < item["score"]:
            unique[key] = item

    picks = list(unique.values())
    rng = Random(seed or datetime.utcnow().strftime("%Y-%m-%d-%H-%M-%S"))
    rng.shuffle(picks)
    picks.sort(key=lambda item: (item["owned"], -item["score"]))
    return picks[:limit]


def render_card(item: dict[str, Any]) -> str:
    source = escape(item["source"])
    artist = escape(item["artist"] or item["title"])
    album = escape(item["album"])
    link = escape(item["link"])
    blurb = escape(item["blurb"])

    return f"""
    <article class="card">
      <div class="eyebrow">{source}</div>
      <h2>{artist}</h2>
      <div class="subhead">{album}</div>
      <p class="blurb">{blurb}</p>
      <a class="release-link" href="{link}" target="_blank" rel="noreferrer">Open release</a>
    </article>
    """


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> str:
    seed = request.query_params.get("seed")
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    picks = pick_items(seed=seed, limit=10)
    cards = "".join(render_card(item) for item in picks)
    library_active = "active" if find_library_path() else "inactive"

    return f"""
    <html>
      <head>
        <title>Release Finds</title>
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
          .stamp {{
            color: var(--muted);
            font-size: 0.85rem;
          }}
          .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
          }}
          .card {{
            background: linear-gradient(180deg, var(--panel) 0%, var(--panel-strong) 100%);
            border: 1px solid var(--edge);
            border-radius: 24px;
            padding: 20px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.22);
            min-height: 220px;
          }}
          .eyebrow {{
            color: var(--accent);
            font-size: 0.72rem;
            text-transform: uppercase;
            letter-spacing: 0.16em;
            margin-bottom: 10px;
          }}
          h2 {{
            margin: 0;
            font-size: 1.28rem;
            line-height: 1.08;
            letter-spacing: -0.03em;
          }}
          .subhead {{
            margin-top: 6px;
            color: var(--muted);
            font-size: 0.96rem;
          }}
          .blurb {{
            color: var(--muted);
            line-height: 1.45;
            font-size: 0.96rem;
            margin: 16px 0 18px;
          }}
          .release-link {{
            display: inline-block;
            color: var(--ink);
            text-decoration: none;
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 999px;
            padding: 10px 14px;
            font-size: 0.86rem;
          }}
          .release-link:hover {{
            color: var(--accent);
            border-color: rgba(131, 224, 193, 0.38);
            background: rgba(255, 255, 255, 0.03);
          }}
          .empty {{
            border: 1px solid var(--edge);
            border-radius: 24px;
            padding: 22px;
            color: var(--muted);
            background: linear-gradient(180deg, var(--panel) 0%, var(--panel-strong) 100%);
          }}
        </style>
      </head>
      <body>
        <main class="shell">
          <div class="toolbar">
            <button class="button" onclick="window.location='/?seed=' + Date.now()">Refresh 10 picks</button>
            <span class="stamp">Generated {generated_at}</span>
            <span class="stamp">Library filter {library_active}</span>
          </div>
          {f'<section class="grid">{cards}</section>' if picks else '<section class="empty">No release links found right now. Hit refresh to try again.</section>'}
        </main>
      </body>
    </html>
    """
