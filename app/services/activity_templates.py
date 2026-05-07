"""Activity templates for Spotify and games — copied from discord-farm/disc/activity.py.

Icons show up in Discord because:
- Spotify: large_image must be the Spotify ALBUM IMAGE ID (not track_id).
  Format: "spotify:ab67616d0000b273{hex}" — fetched from Spotify's public oembed API.
- Games: application_id + large_image URL → shows game icon.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

import aiohttp

logger = logging.getLogger(__name__)

# Cache: track_id → spotify image id (e.g. "ab67616d0000b273...")
_image_id_cache: dict[str, str] = {}


async def _fetch_spotify_image_id(track_id: str) -> str | None:
    """Fetch album cover image ID from Spotify's public oembed endpoint (no auth)."""
    if track_id in _image_id_cache:
        return _image_id_cache[track_id]
    try:
        url = f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{track_id}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    img_url = data.get("thumbnail_url", "")
                    if "i.scdn.co/image/" in img_url:
                        image_id = img_url.split("i.scdn.co/image/")[-1]
                        _image_id_cache[track_id] = image_id
                        return image_id
    except Exception as exc:
        logger.debug("Spotify oembed fetch failed for %s: %s", track_id, exc)
    return None


SPOTIFY_TRACKS = [
    {"title": "God's Plan",          "artist": "Drake",              "album": "Scorpion",                  "duration": 198000, "track_id": "4oMpZxPhFpuuvmqr5QWGCM"},
    {"title": "HUMBLE.",             "artist": "Kendrick Lamar",     "album": "DAMN.",                     "duration": 177000, "track_id": "7KXjTSCq5nL1LoYtL7XAwS"},
    {"title": "SICKO MODE",          "artist": "Travis Scott",       "album": "ASTROWORLD",                "duration": 312000, "track_id": "2xLMifQCjDGFmkHkpNLD9h"},
    {"title": "No Role Modelz",      "artist": "J. Cole",            "album": "2014 Forest Hills Drive",   "duration": 293000, "track_id": "5cGnv2RMPGSuQFBOf3U7KJ"},
    {"title": "Self Care",           "artist": "Mac Miller",         "album": "Swimming",                  "duration": 346000, "track_id": "4Ly1bGEJfLVU5WmhNz5KL1"},
    {"title": "POWER",               "artist": "Kanye West",         "album": "My Beautiful Dark Twisted Fantasy", "duration": 292000, "track_id": "2gZUPNdnz5Y45eiGxpHGSc"},
    {"title": "Lose Yourself",       "artist": "Eminem",             "album": "8 Mile",                    "duration": 326000, "track_id": "5Z01UMMf7V1o0MzF86s6WJ"},
    {"title": "EARFQUAKE",           "artist": "Tyler, the Creator", "album": "IGOR",                      "duration": 192000, "track_id": "3iVcZ5G6tvkXZkZKlMpIUs"},
    {"title": "Rich Flex",           "artist": "Drake & 21 Savage",  "album": "Her Loss",                  "duration": 211000, "track_id": "1bDbXMyjaUIooNwFE9wn0N"},
    {"title": "Kill Bill",           "artist": "SZA",                "album": "SOS",                       "duration": 153000, "track_id": "1Qrg8KqiBpW07V7PNxwwwL"},
    {"title": "Blinding Lights",     "artist": "The Weeknd",         "album": "After Hours",               "duration": 200000, "track_id": "0VjIjW4GlUZAMYd2vXMi3b"},
    {"title": "Sunflower",           "artist": "Post Malone",        "album": "Spider-Man: Into the Spider-Verse", "duration": 158000, "track_id": "3KkXRkHbMCARz0aVfEt68P"},
    {"title": "Levitating",          "artist": "Dua Lipa",           "album": "Future Nostalgia",          "duration": 203000, "track_id": "463CkQjx2Zk1yXoBuierM9"},
    {"title": "Starboy",             "artist": "The Weeknd",         "album": "Starboy",                   "duration": 230000, "track_id": "5aAx2yezTd8zXrkmtKl66Z"},
    {"title": "The Less I Know The Better", "artist": "Tame Impala", "album": "Currents",                 "duration": 217000, "track_id": "6K4t31amVTZDgR3sKmwUJJ"},
    {"title": "R U Mine?",           "artist": "Arctic Monkeys",     "album": "AM",                        "duration": 202000, "track_id": "6yt9lYAJI0cBbHGeoV2wNG"},
    {"title": "Do I Wanna Know?",    "artist": "Arctic Monkeys",     "album": "AM",                        "duration": 272000, "track_id": "5FVd6KXrgO9B3JPmC8OPst"},
    {"title": "505",                 "artist": "Arctic Monkeys",     "album": "Favourite Worst Nightmare", "duration": 253000, "track_id": "0BxE4FqsDD1Ot4YuBXwAPp"},
    {"title": "Lucid Dreams",        "artist": "Juice WRLD",         "album": "Goodbye & Good Riddance",   "duration": 239000, "track_id": "285pBltuF7vW8TeIKgscAR"},
    {"title": "XO Tour Llif3",       "artist": "Lil Uzi Vert",       "album": "Luv Is Rage 2",             "duration": 200000, "track_id": "7GX5flRQZVHvD11qtapldc"},
    {"title": "Mask Off",            "artist": "Future",             "album": "Future",                    "duration": 203000, "track_id": "0VgkVdmE4gld66l8iyGjgx"},
    {"title": "Rockstar",            "artist": "Post Malone",        "album": "beerbongs & bentleys",      "duration": 218000, "track_id": "0e7ipj03S05BNilyu5bRzt"},
    {"title": "DNA.",                "artist": "Kendrick Lamar",     "album": "DAMN.",                     "duration": 185000, "track_id": "6HZILIRieu8S0iqY8kIKhj"},
    {"title": "goosebumps",          "artist": "Travis Scott",       "album": "Birds in the Trap Sing McKnight", "duration": 243000, "track_id": "6wIi9iiQ2oRxDhPmRBpQ3A"},
    {"title": "One More Time",       "artist": "Daft Punk",          "album": "Discovery",                 "duration": 320000, "track_id": "0DiWol3AO6WpXZgdNdQXES"},
    {"title": "Get Lucky",           "artist": "Daft Punk",          "album": "Random Access Memories",    "duration": 369000, "track_id": "2Foc5Q5nqNiosCNqttzHof"},
    {"title": "Midnight City",       "artist": "M83",                "album": "Hurry Up, We're Dreaming",  "duration": 243000, "track_id": "1eyzqe2QqGZUmfcPZtrIyt"},
    {"title": "Mr. Brightside",      "artist": "The Killers",        "album": "Hot Fuss",                  "duration": 222000, "track_id": "003vvx7Niy0yvhvHt4a14Y"},
    {"title": "Smells Like Teen Spirit", "artist": "Nirvana",        "album": "Nevermind",                 "duration": 301000, "track_id": "5ghIJDpPoe3CfHMGu71E6T"},
    {"title": "Bohemian Rhapsody",   "artist": "Queen",              "album": "A Night at the Opera",      "duration": 355000, "track_id": "7tFiyTwD0nx5a1eklYtX2J"},
]

GAMES = [
    {
        "name": "Counter-Strike 2",
        "app_id": "730828853285830746",
        "large_image": "https://shared.fastly.steamstatic.com/community_assets/images/apps/730/8dbc71957312bbd3baea65848b545be9eae2a355.jpg",
        "large_text": "Counter-Strike 2",
        "details": ["Competitive • Mirage", "Competitive • Dust 2", "Competitive • Inferno", "Premier • Mirage", "Deathmatch • Dust 2"],
        "state": ["CT Side • 12 : 5", "T Side • 8 : 10", "CT Side • 14 : 9", "T Side • 3 : 7", "CT Side • 6 : 6"],
    },
    {
        "name": "Valorant",
        "app_id": "700136079562375199",
        "large_image": "https://cdn.discordapp.com/app-icons/700136079562375258/e55fc8259df1548328f977d302779ab7.png?size=160",
        "large_text": "Valorant",
        "details": ["Competitive • Ascent", "Competitive • Haven", "Unrated • Sunset", "Competitive • Lotus", "Deathmatch"],
        "state": ["Attack • 5 : 4", "Defense • 8 : 7", "Attack • 11 : 10", "Defense • 3 : 6"],
    },
    {
        "name": "Dota 2",
        "app_id": "356875570662621184",
        "large_image": "https://cdn.discordapp.com/app-icons/356875988589740042/6b4b3fa4c83555d3008de69d33a60588.png?size=160",
        "large_text": "Dota 2",
        "details": ["All Pick • Radiant", "All Pick • Dire", "Ranked All Pick • Radiant", "Turbo Mode"],
        "state": ["Midlane • Carry", "Support • Safe Lane", "Offlane", "Hard Support"],
    },
    {
        "name": "Apex Legends",
        "app_id": "431276913082490890",
        "large_image": "https://shared.fastly.steamstatic.com/community_assets/images/apps/1172470/1b94d48e50e6df48a47dd75c7a3adf76a57f0de2.jpg",
        "large_text": "Apex Legends",
        "details": ["Battle Royale • Kings Canyon", "Battle Royale • World's Edge", "Ranked • Storm Point"],
        "state": ["Squad • 2 Alive", "Solo • Top 10", "1 Kill • Looting"],
    },
    {
        "name": "Minecraft",
        "app_id": "356875570662621184",
        "large_image": "https://static.wikia.nocookie.net/minecraft_gamepedia/images/8/8e/Grass_Block_JE7_BE6.png",
        "large_text": "Minecraft Java Edition",
        "details": ["Survival Mode", "Building • Survival", "Hypixel SkyBlock", "Creative Mode"],
        "state": ["In a cave", "Mining diamonds", "Building a base", "Y: 11 • Mining"],
    },
    {
        "name": "Rust",
        "app_id": "356876112570064917",
        "large_image": "https://shared.fastly.steamstatic.com/community_assets/images/apps/252490/820be4782639f9c4b64fa3ca7e6c26a95ae4fd1c.jpg",
        "large_text": "Rust",
        "details": ["Vanilla • Facepunch #1", "Modded • 2x Server", "Solo • Official Server"],
        "state": ["Farming stone", "Raiding", "Geared up", "Roaming"],
    },
    {
        "name": "GTA V",
        "app_id": "356876342777470977",
        "large_image": "https://cdn.discordapp.com/app-icons/1402418714716143646/b77111108195cd5e4dd2011dd39bf67d.png?size=160",
        "large_text": "Grand Theft Auto V",
        "details": ["GTA Online • Freemode", "GTA Online • Heist", "GTA Online • Cayo Perico"],
        "state": ["CEO • Making money", "In a lobby", "MC President"],
    },
    {
        "name": "League of Legends",
        "app_id": "401518684763586560",
        "large_image": "https://cdn.discordapp.com/app-icons/1402418696126992445/5a15a24a3931880801709a32accd0a1d.png?size=160",
        "large_text": "League of Legends",
        "details": ["Ranked Solo • Summoner's Rift", "Normal • Summoner's Rift", "ARAM", "Flex Queue"],
        "state": ["Midlane • Assassin", "ADC • Bot Lane", "Jungle", "Support • Bot Lane"],
    },
    {
        "name": "Cyberpunk 2077",
        "app_id": "418238566651404288",
        "large_image": "https://shared.fastly.steamstatic.com/community_assets/images/apps/1091500/9da7ff37cfdb9c5a86b2bd52476af49ea20a15b0.jpg",
        "large_text": "Cyberpunk 2077",
        "details": ["Main Story", "Side Quests", "Phantom Liberty DLC", "Night City"],
        "state": ["Watson District", "Corpo Plaza", "Pacifica", "Dogtown"],
    },
    {
        "name": "Elden Ring",
        "app_id": "1161235054286135376",
        "large_image": "https://shared.fastly.steamstatic.com/community_assets/images/apps/1245620/01b4a5a6b8c5c1fe9a5a8c1e7c6d3d6e0a0c3b4b.jpg",
        "large_text": "Elden Ring",
        "details": ["Exploring the Lands Between", "Boss Fight", "Shadow of the Erdtree"],
        "state": ["Limgrave", "Caelid", "Liurnia", "Altus Plateau"],
    },
]


async def build_spotify_activity(track: dict | None = None) -> dict:
    """Build a full Spotify activity payload with real album art.

    Fetches the album image ID from Spotify's public oembed API (no auth needed).
    large_image format: "spotify:{album_image_id}" — Discord fetches art from Spotify CDN.
    Falls back to track_id if oembed fails (shows ? placeholder).
    """
    t = track or random.choice(SPOTIFY_TRACKS)
    now_ms = int(time.time() * 1000)
    offset = random.randint(15_000, max(15_001, t["duration"] - 30_000))

    # Fetch real album image ID — the key for album art in Discord presence
    image_id = await _fetch_spotify_image_id(t["track_id"])
    large_image = f"spotify:{image_id}" if image_id else f"spotify:{t['track_id']}"

    return {
        "type": 2,
        "name": "Spotify",
        "id": "spotify:1",
        "flags": 48,
        "details": t["title"],
        "state": t["artist"],
        "assets": {
            "large_image": large_image,
            "large_text": t["album"],
        },
        "timestamps": {
            "start": now_ms - offset,
            "end": now_ms - offset + t["duration"],
        },
        "sync_id": t["track_id"],
        "party": {"id": f"spotify:{random.randint(10**15, 10**16)}"},
    }


def build_game_activity(game: dict | None = None) -> dict:
    """Build a game activity payload. application_id makes Discord show the game icon."""
    g = game or random.choice(GAMES)
    return {
        "type": 0,
        "name": g["name"],
        "application_id": g["app_id"],
        "details": random.choice(g["details"]),
        "state": random.choice(g["state"]),
        "assets": {
            "large_image": g["large_image"],
            "large_text": g["large_text"],
        },
        "timestamps": {
            "start": int(time.time() * 1000) - random.randint(3_600_000, 28_800_000)
        },
    }


async def build_random_activity() -> dict:
    """50/50 Spotify or game."""
    if random.random() < 0.5:
        return await build_spotify_activity()
    return build_game_activity()


# Human-readable labels for the UI
GAME_NAMES = [g["name"] for g in GAMES]
