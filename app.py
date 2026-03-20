from fastapi import FastAPI
import requests
from bs4 import BeautifulSoup
import feedparser
import random

app = FastAPI()

# ====== YOUR TASTE ======
YOUR_ARTISTS = [
    "GAS",
    "Deepchord",
    "Loscil",
    "Tim Hecker",
    "KMRU"
]

YOUR_LABELS = [
    "Raster-Noton",
    "Hospital Productions",
    "Boomkat Editions"
]

# ====== UNDERGROUND LABEL RSS FEEDS ======
LABEL_RSS_FEEDS = [
    "https://raster-noton.com/feed/",             # Raster-Noton
    "https://hospitalproductions.bandcamp.com/releases?format=RSS",  # Hospital Productions
    "https://boomkateditions.com/feed/",         # Boomkat Editions
    "https://crónica.bandcamp.com/releases?format=RSS", # Replace with real feed
    "https://www.12k.com/feed/"                  # 12k
]

# ====== BANDCAMP SEARCH ======
def search_bandcamp(query):
    """
    Search Bandcamp for an artist or label
    Return list of embeddable iframe URLs (up to 3)
    """
    url = f"https://bandcamp.com/search?q={query}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        embeds = []
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src")
            if src and "bandcamp" in src:
                embeds.append(src)
        return embeds[:3]
    except:
        return []

# ====== PARSE LABEL RSS FEED ======
def parse_label_feed(feed_url):
    try:
        feed = feedparser.parse(feed_url)
        embeds = []
        for entry in feed.entries[:3]:  # take latest 3 tracks per feed
            # look for bandcamp iframe or link
            if 'link' in entry:
                embed_src = get_bandcamp_embed(entry.link)
                if embed_src:
                    embeds.append(embed_src)
        return embeds
    except:
        return []

# ====== GET BANDCAMP EMBED ======
def get_bandcamp_embed(url):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        iframe = soup.find("iframe")
        if iframe:
            return iframe["src"]
        # fallback: if no iframe, return link itself
        return url
    except:
        return url

# ====== MAIN PAGE ======
@app.get("/")
def home():
    html = "<h1>Underground Issue 🎧</h1>"

    # Randomize artists to keep it fresh
    random.shuffle(YOUR_ARTISTS)

    # Artists first
    for artist in YOUR_ARTISTS:
        embeds = search_bandcamp(artist)
        if embeds:
            html += f"<h2>Related to: {artist}</h2>"
            for e in embeds:
                if e.startswith("http"):
                    html += f'<iframe style="border:0;width:350px;height:470px;" src="{e}"></iframe>'
        else:
            html += f"<h2>{artist}</h2><p>No embeds found</p>"

    # Label RSS feeds
    for feed_url in LABEL_RSS_FEEDS:
        embeds = parse_label_feed(feed_url)
        if embeds:
            html += f"<h2>Label Spotlight: {feed_url.split('//')[-1]}</h2>"
            for e in embeds:
                if e.startswith("http"):
                    html += f'<iframe style="border:0;width:350px;height:470px;" src="{e}"></iframe>'
        else:
            html += f"<h2>Label Spotlight: {feed_url.split('//')[-1]}</h2><p>No recent embeds</p>"

    return html
