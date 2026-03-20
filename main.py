from __future__ import annotations

from datetime import datetime
from html import escape
from random import Random
import re
from urllib.parse import quote

import feedparser
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
}

BLOG_FEEDS = [
    ("A Closer Listen", "https://acloserlisten.com/feed/"),
    ("Headphone Commute", "https://headphonecommute.com/feed/"),
    ("Fluid Radio", "https://fluid-radio.co.uk/feed/"),
    ("Boomkat", "https://boomkat.com/feed"),
]

REDDIT_SOURCES = [
    ("r/ambientmusic", "https://www.reddit.com/r/ambientmusic/new.json?limit=16"),
    ("r/experimentalmusic", "https://www.reddit.com/r/experimentalmusic/new.json?limit=16"),
    ("r/bandcamp", "https://www.reddit.com/r/BandCamp/new.json?limit=16"),
]

BANDCAMP_RE = re.compile(r"https?://[^\s\"'>]+bandcamp\.com[^\s\"'>]*", re.IGNORECASE)


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
        url = f"https://bandcamp.com/search?q={quote(query)}"
        response = requests.get(url, headers=HEADERS, timeout=12)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        for link in soup.find_all("a", href=True):
            href = normalize_url(link["href"])
            if href and ("bandcamp.com/album/" in href or "bandcamp.com/track/" in href):
                return href
    except Exception as exc:
        print(f"Bandcamp search failed for {query}: {exc}")
    return None


def build_item(
    source: str,
    title: str,
    link: str,
    summary: str = "",
    bandcamp_url: str | None = None,
) -> dict[str, str]:
    bandcamp_url = normalize_url(bandcamp_url)
    if not bandcamp_url:
        bandcamp_url = search_bandcamp(title)

    embed_url = get_bandcamp_embed(bandcamp_url) if bandcamp_url else None

    return {
        "source": source,
        "title": title.strip(),
        "link": normalize_url(link) or "",
        "summary": clean_text(summary),
        "bandcamp_url": bandcamp_url or "",
        "embed_url": embed_url or "",
    }


def fetch_blog_items() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for source_name, feed_url in BLOG_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:8]:
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
                        )
                    )
        except Exception as exc:
            print(f"Feed failed for {source_name}: {exc}")
    return items


def fetch_reddit_items() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for source_name, url in REDDIT_SOURCES:
        try:
            response = requests.get(url, headers=HEADERS, timeout=12)
            response.raise_for_status()
            payload = response.json()
            children = payload.get("data", {}).get("children", [])

            for child in children:
                data = child.get("data", {})
                title = data.get("title", "").strip()
                post_url = normalize_url(data.get("url"))
                permalink = normalize_url(f"https://www.reddit.com{data.get('permalink', '')}")
                summary = data.get("selftext", "")
                bandcamp_url = extract_bandcamp_url(post_url, summary)
                link = post_url or permalink or ""

                if title and link:
                    items.append(
                        build_item(
                            source=source_name,
                            title=title,
                            link=link,
                            summary=summary,
                            bandcamp_url=bandcamp_url,
                        )
                    )
        except Exception as exc:
            print(f"Reddit fetch failed for {source_name}: {exc}")
    return items


def pick_rotating_items() -> list[dict[str, str]]:
    pool = fetch_blog_items() + fetch_reddit_items()

    unique: dict[tuple[str, str], dict[str, str]] = {}
    for item in pool:
        key = (item["title"].lower(), item["source"].lower())
        if key not in unique:
            unique[key] = item

    items = list(unique.values())
    seed = datetime.utcnow().strftime("%Y-%m-%d-%H")
    Random(seed).shuffle(items)
    return items[:12]


def render_card(item: dict[str, str]) -> str:
    title = escape(item["title"])
    source = escape(item["source"])
    link = escape(item["link"])
    summary = escape(item["summary"])
    bandcamp_url = escape(item["bandcamp_url"])
    embed_url = escape(item["embed_url"])

    links = [f'<a href="{link}" target="_blank" rel="noreferrer">source</a>']
    if bandcamp_url:
        links.append(f'<a href="{bandcamp_url}" target="_blank" rel="noreferrer">bandcamp</a>')

    embed_html = ""
    if embed_url:
        embed_html = (
            f'<div class="embed-wrap"><iframe loading="lazy" src="{embed_url}"></iframe></div>'
        )

    summary_html = f"<p>{summary}</p>" if summary else ""

    return f"""
    <article class="card">
        <div class="meta">{source}</div>
        <h2>{title}</h2>
        {summary_html}
        <div class="links">{' '.join(links)}</div>
        {embed_html}
    </article>
    """


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    picks = pick_rotating_items()
    cards = "".join(render_card(item) for item in picks)

    return f"""
    <html>
      <head>
        <title>Underground Issue</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
          :root {{
            --bg: #efe8dc;
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
          .links {{
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
            <p class="kicker">Rotating feed</p>
            <h1>Underground Issue</h1>
            <p class="intro">
              A changing stack of ambient, experimental and hard-to-find releases
              pulled from blogs and Reddit, then pushed toward Bandcamp when a trail exists.
              The mix reshuffles every hour so it does not freeze around the same names.
            </p>
          </section>
          <section class="grid">
            {cards}
          </section>
          <p class="footer">
            Sources: A Closer Listen, Headphone Commute, Fluid Radio, Boomkat,
            r/ambientmusic, r/experimentalmusic, r/BandCamp.
          </p>
        </main>
      </body>
    </html>
    """
