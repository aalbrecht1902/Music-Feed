from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
from bs4 import BeautifulSoup
import feedparser
import random
from urllib.parse import quote

app = FastAPI()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
}

YOUR_ARTISTS = [
    "GAS",
    "Deepchord",
    "Loscil",
    "Tim Hecker",
    "KMRU",
]

LABEL_RSS_FEEDS = [
    "https://raster-media.net/feed/",
    "https://hospitalproductions.bandcamp.com/releases?format=RSS",
    "https://boomkateditions.bandcamp.com/releases?format=RSS",
    "https://cronica.bandcamp.com/releases?format=RSS",
    "https://www.12k.com/feed/",
]


def normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    return url


def get_bandcamp_embed(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        iframe = soup.find("iframe")
        if iframe and iframe.get("src"):
            return normalize_url(iframe["src"])

        meta = soup.find("meta", attrs={"property": "og:video"})
        if meta and meta.get("content"):
            return normalize_url(meta["content"])

        return None
    except Exception as e:
        print(f"get_bandcamp_embed failed for {url}: {e}")
        return None


def search_bandcamp(query: str) -> list[str]:
    url = f"https://bandcamp.com/search?q={quote(query)}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        album_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/album/" in href or "/track/" in href:
                href = normalize_url(href)
                if href not in album_links:
                    album_links.append(href)

        embeds = []
        for link in album_links[:5]:
            embed = get_bandcamp_embed(link)
            if embed:
                embeds.append(embed)
            if len(embeds) == 3:
                break

        return embeds
    except Exception as e:
        print(f"search_bandcamp failed for {query}: {e}")
        return []


def parse_label_feed(feed_url: str) -> list[str]:
    try:
        feed = feedparser.parse(feed_url)
        embeds = []

        for entry in feed.entries[:5]:
            link = entry.get("link")
            if not link:
                continue

            embed = get_bandcamp_embed(link)
            if embed:
                embeds.append(embed)

            if len(embeds) == 3:
                break

        return embeds
    except Exception as e:
        print(f"parse_label_feed failed for {feed_url}: {e}")
        return []


@app.get("/", response_class=HTMLResponse)
def home():
    html = """
    <html>
    <head>
        <title>Underground Issue</title>
        <style>
            body {
                font-family: sans-serif;
                max-width: 1200px;
                margin: 40px auto;
                padding: 0 20px;
                background: #111;
                color: #eee;
            }
            h1, h2 {
                color: #fff;
            }
            .grid {
                display: flex;
                flex-wrap: wrap;
                gap: 20px;
                margin-bottom: 40px;
            }
            iframe {
                border: 0;
                width: 350px;
                height: 470px;
            }
            p {
                color: #bbb;
            }
        </style>
    </head>
    <body>
        <h1>Underground Issue</h1>
    """

    artists = YOUR_ARTISTS[:]
    random.shuffle(artists)

    for artist in artists:
        embeds = search_bandcamp(artist)
        html += f"<h2>Related to: {artist}</h2>"
        if embeds:
            html += '<div class="grid">'
            for e in embeds:
                html += f'<iframe src="{e}" loading="lazy"></iframe>'
            html += "</div>"
        else:
            html += "<p>No embeds found</p>"

    for feed_url in LABEL_RSS_FEEDS:
        embeds = parse_label_feed(feed_url)
        label_name = feed_url.split("//")[-1].split("/")[0]
        html += f"<h2>Label Spotlight: {label_name}</h2>"
        if embeds:
            html += '<div class="grid">'
            for e in embeds:
                html += f'<iframe src="{e}" loading="lazy"></iframe>'
            html += "</div>"
        else:
            html += "<p>No recent embeds</p>"

    html += "</body></html>"
    return html
