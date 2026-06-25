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
from app.services.discord_api import _get_session

logger = logging.getLogger(__name__)

# Cache: track_id → spotify image id (e.g. "ab67616d0000b273...")
_image_id_cache: dict[str, str] = {}


async def _fetch_spotify_image_id(track_id: str) -> str | None:
    """Fetch album cover image ID from Spotify's public oembed endpoint (no auth)."""
    if track_id in _image_id_cache:
        return _image_id_cache[track_id]
    try:
        url = f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{track_id}"
        s = _get_session()
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


# Cache: application_id → app icon hash (e.g. "5b86f62727932b...")
_app_icon_cache: dict[str, str | None] = {}


async def _fetch_app_icon_hash(app_id: str) -> str | None:
    """Fetch an application's icon hash from Discord's public RPC endpoint (no auth).

    `assets.large_image` for a fake rich-presence activity must reference this
    icon hash (served from cdn.discordapp.com/app-icons/{app_id}/{hash}.png) —
    not the app's Public Key or Client ID, which don't resolve to any image.
    """
    if app_id in _app_icon_cache:
        return _app_icon_cache[app_id]
    try:
        url = f"https://discord.com/api/v9/applications/{app_id}/rpc"
        s = _get_session()
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                data = await r.json()
                icon = data.get("icon")
                _app_icon_cache[app_id] = icon
                return icon
    except Exception as exc:
        logger.debug("App icon fetch failed for %s: %s", app_id, exc)
    _app_icon_cache[app_id] = None
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
        "app_id": "1510661633859522781",
        "large_text": "Counter-Strike 2",
        "details": ["Competitive • Mirage", "Competitive • Dust 2", "Competitive • Inferno", "Premier • Mirage", "Deathmatch • Dust 2"],
        "state": ["CT Side", "T Side"],
    },
    {
        "name": "VALORANT",
        "app_id": "1510663126847062256",
        "large_text": "VALORANT",
        "details": ["Competitive • Ascent", "Competitive • Haven", "Unrated • Sunset", "Competitive • Lotus", "Deathmatch"],
        "state": ["Attack", "Defense"],
    },
    {
        "name": "Spotify",
        "app_id": "1510666707746689034",
        "large_text": "Spotify",
        "details": ["Listening to music", "Discovering Weekly", "Daily Mix 1", "Liked Songs"],
        "state": ["Shuffle mode on", "Playing playlist", "Radio"],
    },
    {
        "name": "Red Dead Redemption 2",
        "app_id": "1510668195671838840",
        "large_text": "Red Dead Redemption 2",
        "details": ["Story Mode", "Red Dead Online", "Exploring the map"],
        "state": ["Heartlands", "Saint Denis", "West Elizabeth", "New Hanover", "Ambarino"],
    },
    {
        "name": "Apex Legends",
        "app_id": "1510671868871311482",
        "large_text": "Apex Legends",
        "details": ["Battle Royale • Kings Canyon", "Battle Royale • World's Edge", "Ranked • Storm Point"],
        "state": ["Squad • Alive", "Solo • Top 10", "Looting"],
    },
    {
        "name": "YouTube Music",
        "app_id": "1510672565440614590",
        "large_text": "YouTube Music",
        "details": ["Listening to music", "Watching a music video", "Auto-play radio"],
        "state": ["Shuffle on", "Playing playlist", "Liked Music"],
    },
    {
        "name": "League of Legends",
        "app_id": "1517242227540365574",
        "large_text": "League of Legends",
        "details": ["Ranked Solo • Summoner's Rift", "Normal • Summoner's Rift", "ARAM", "Flex Queue"],
        "state": ["Midlane", "ADC • Bot Lane", "Jungle", "Support"],
    },
    {
        "name": "Grand Theft Auto V",
        "app_id": "1517243079491588268",
        "large_text": "Grand Theft Auto V",
        "details": ["GTA Online", "Story Mode", "Heist in progress"],
        "state": ["Free Roam", "Racing", "In Mission"],
    },
    {
        "name": "ARC Raiders",
        "app_id": "1517243201314885672",
        "large_text": "ARC Raiders",
        "details": ["In a raid", "Preparing loadout", "Scavenging resources"],
        "state": ["Solo", "Squad", "Heading to extraction"],
    },
    {
        "name": "Among Us",
        "app_id": "1517250576512057455",
        "large_text": "Among Us",
        "details": ["In a lobby", "Playing • The Skeld", "Playing • Polus", "Playing • MIRA HQ"],
        "state": ["Crewmate", "Impostor", "In Meeting"],
    },
    {
        "name": "Roblox",
        "app_id": "1517257264153497660",
        "large_text": "Roblox",
        "details": ["Playing a game", "In Studio", "Browsing experiences"],
        "state": ["In Game", "Idle", "With friends"],
    },
    {
        "name": "World of Warcraft",
        "app_id": "1517257504885834030",
        "large_text": "World of Warcraft",
        "details": ["Mythic+ Dungeon", "Raiding", "Questing", "Battleground"],
        "state": ["Stormwind", "Orgrimmar", "The Forbidden Reach"],
    },
    {
        "name": "Escape From Tarkov",
        "app_id": "1517257673760833716",
        "large_text": "Escape From Tarkov",
        "details": ["Raid • Customs", "Raid • Reserve", "Raid • Shoreline", "Raid • Woods"],
        "state": ["PMC", "Scav Run", "Looting"],
    },
    {
        "name": "Overwatch",
        "app_id": "1517258011834449981",
        "large_text": "Overwatch",
        "details": ["Competitive • Push", "Quick Play", "Competitive • Escort", "Arcade"],
        "state": ["Tank", "DPS", "Support"],
    },
    {
        "name": "PUBG: BATTLEGROUNDS",
        "app_id": "1517260887126442014",
        "large_text": "PUBG: BATTLEGROUNDS",
        "details": ["Normal • Erangel", "Ranked • Miramar", "Normal • Sanhok", "Normal • Vikendi"],
        "state": ["Squad • Alive", "Looting", "In a vehicle", "Parachuting"],
    },
    {
        "name": "Poker Night",
        "app_id": "1517264147576914060",
        "large_text": "Poker Night",
        "details": ["Texas Hold'em", "Tournament", "Cash Game"],
        "state": ["At the table", "All In", "Waiting for hand"],
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


_THREE_HOURS_MS = 3 * 3600 * 1000
_MAX_OFFSET_MS  = 180 * 60_000


async def build_game_activity(
    game: dict | None = None,
    *,
    start_offset_ms: int | None = None,
) -> dict:
    """Build a game activity payload.

    start_offset_ms — milliseconds ago the session "started".
    None → random 0-180 min.  0 → timer starts at 00:00 right now.
    Capped at 3 h so the displayed timer never exceeds that value.
    """
    g = game or random.choice(GAMES)
    if start_offset_ms is None:
        offset = random.randint(0, _MAX_OFFSET_MS)
    else:
        offset = max(0, min(int(start_offset_ms), _THREE_HOURS_MS))
    activity: dict = {
        "type": 0,
        "name": g["name"],
        "application_id": g["app_id"],
        "timestamps": {"start": int(time.time() * 1000) - offset},
    }
    if g["details"]:
        activity["details"] = random.choice(g["details"])
    if g["state"]:
        activity["state"] = random.choice(g["state"])
    icon_hash = await _fetch_app_icon_hash(g["app_id"])
    if icon_hash:
        activity["assets"] = {
            "large_image": f"mp:app-icons/{g['app_id']}/{icon_hash}.png",
            "large_text": g["large_text"],
        }
    return activity


async def build_random_activity(*, start_offset_ms: int | None = None) -> dict:
    """Pick a random game activity."""
    return await build_game_activity(start_offset_ms=start_offset_ms)


SPECIAL_ACTIVITIES = {
    "Visual Studio Code": {
        "name": "Visual Studio Code",
        "app_id": "1510667611937968340",
        "large_text": "Visual Studio Code",
        "details": ["Editing main.py", "Working on project", "Editing index.ts", "Debugging"],
        "state": ["In workspace", "Git: main", "No folder open"],
    },
    "Azora": {
        "name": "Azora",
        "app_id": "1517238868942913599",
        "large_text": "Azora",
        "details": ["Translating voice in real-time", "Setting up languages"],
        "state": [],
    },
}

# Human-readable labels for the UI
GAME_NAMES = [g["name"] for g in GAMES]
SPECIAL_NAMES = list(SPECIAL_ACTIVITIES.keys())
