from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
from bs4 import BeautifulSoup
import feedparser
import random
from urllib.parse import quote

app = FastAPI()

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

YOUR_ARTISTS = ["GAS", "Deepchord", "Loscil", "Tim Hecker", "KMRU"]

LABEL_RSS_FEEDS = [
    "https://hospitalproductions.bandcamp.com/releases?format=RSS",
    "https://boomkateditions.bandcamp.com/releases?format=RSS",
    "https://cronica.bandcamp.com/releases?format=RSS",
    "https://www.12k.com/feed/",
]

def normalize_url(url: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    return url

def get_bandcamp_embed(url: str):
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
        print(f"embed error {url}: {e}")
        return None

def search_bandcamp(query: str):
    try:
        r = requests.get(f"https://bandcamp.com/search?q={quote(query)}", headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        links = []
        for a in soup.find_all("a", href=True):
            href = normalize_url(a["href"])
            if "/album/" in href or "/track/" in href:
                if href not in links:
                    links.append(href)

        embeds = []
        for link in links[:5]:
            embed = get_bandcamp_embed(link)
            if embed:
                embeds.append(embed)
            if len(embeds) == 3:
                break
        return embeds
    except Exception as e:
        print(f"search error {query}: {e}")
        return []

def parse_label_feed(feed_url: str):
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
        print(f"feed error {feed_url}: {e}")
        return []

@app.get("/", response_class=HTMLResponse)
def home():
    artists = YOUR_ARTISTS[:]
    random.shuffle(artists)

    html = "<html><body style='background:#111;color:#eee;font-family:sans-serif'><h1>Underground Issue</h1>"

    for artist in artists:
        html += f"<h2>{artist}</h2>"
        embeds = search_bandcamp(artist)
        if embeds:
            for e in embeds:
                html += f'<iframe style="border:0;width:350px;height:470px;" src="{e}"></iframe>'
        else:
            html += "<p>No embeds found</p>"

    for feed_url in LABEL_RSS_FEEDS:
        html += f"<h2>{feed_url}</h2>"
        embeds = parse_label_feed(feed_url)
        if embeds:
            for e in embeds:
                html += f'<iframe style="border:0;width:350px;height:470px;" src="{e}"></iframe>'
        else:
            html += "<p>No recent embeds</p>"

    html += "</body></html>"
    return html
