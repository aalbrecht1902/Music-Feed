from fastapi import FastAPI
import requests
from bs4 import BeautifulSoup

app = FastAPI()

YOUR_ARTISTS = [
    "GAS",
    "Deepchord",
    "Loscil",
    "Tim Hecker",
    "KMRU"
]

def search_bandcamp(artist):
    url = f"https://bandcamp.com/search?q={artist}"
    r = requests.get(url)
    soup = BeautifulSoup(r.text, "html.parser")

    embeds = []

    for iframe in soup.find_all("iframe"):
        src = iframe.get("src")
        if src and "bandcamp" in src:
            embeds.append(src)

    return embeds[:2]


@app.get("/")
def home():
    html = "<h1>Underground Picks</h1>"

    for artist in YOUR_ARTISTS:
        embeds = search_bandcamp(artist)

        html += f"<h2>{artist}</h2>"

        for e in embeds:
            html += f'<iframe style="border:0;width:350px;height:470px;" src="{e}"></iframe>'

    return html
