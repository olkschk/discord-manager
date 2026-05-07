"""Voice audio player using discord.py-self.

Connects an account to a Discord voice channel and plays a local audio file.
Uses FFmpegOpusAudio — requires FFmpeg installed and on PATH.

One play session per account (tracked by _sessions dict).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Track active discord.py-self clients: account_id → client
_sessions: dict[str, object] = {}


async def stop_playing(account_id: str) -> None:
    """Stop current playback and disconnect the discord.py-self client."""
    client = _sessions.pop(account_id, None)
    if client is None:
        return
    try:
        if hasattr(client, "voice_clients"):
            for vc in list(client.voice_clients):
                try:
                    vc.stop()
                    await vc.disconnect(force=True)
                except Exception:
                    pass
        await client.close()
    except Exception as exc:
        logger.warning("voice_player: stop error: %s", exc)


async def play_sound(
    account_id: str,
    token: str,
    guild_id: str,
    channel_id: str,
    sound_path: str,
    *,
    proxy_url: str | None = None,
) -> dict:
    """Connect to voice channel and play sound_path.

    Returns {"ok": True} immediately after starting; playback runs in background.
    Stops any existing playback for this account first.
    """
    import discord

    # Stop existing session for this account
    await stop_playing(account_id)

    sound_file = Path(sound_path)
    if not sound_file.exists():
        return {"ok": False, "error": f"File not found: {sound_file.name}"}

    ready_event = asyncio.Event()
    error_holder: list[str] = []

    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(proxy=proxy_url, intents=intents)
    _sessions[account_id] = client

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(int(guild_id))
            if guild is None:
                error_holder.append(f"Guild {guild_id} not found (account not in server?)")
                ready_event.set()
                await client.close()
                return

            channel = guild.get_channel(int(channel_id))
            if channel is None:
                error_holder.append(f"Voice channel {channel_id} not found")
                ready_event.set()
                await client.close()
                return

            # Disconnect from any existing voice in this guild
            existing_vc = guild.voice_client
            if existing_vc:
                await existing_vc.disconnect(force=True)

            vc = await channel.connect()
            logger.info("voice_player: connected to %s, playing %s", channel.name, sound_file.name)
            ready_event.set()

            def after_play(error):
                if error:
                    logger.warning("voice_player: playback error: %s", error)
                logger.info("voice_player: finished playing %s for %s", sound_file.name, account_id)
                asyncio.run_coroutine_threadsafe(
                    _disconnect_and_close(account_id, vc, client), client.loop
                )

            vc.play(
                discord.FFmpegOpusAudio(str(sound_file)),
                after=after_play,
            )

        except Exception as exc:
            logger.exception("voice_player: on_ready error")
            error_holder.append(str(exc))
            ready_event.set()
            try:
                await client.close()
            except Exception:
                pass

    # Start the client in a background task
    async def _run():
        try:
            await client.start(token)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("voice_player: client error: %s", exc)
        finally:
            _sessions.pop(account_id, None)

    asyncio.create_task(_run(), name=f"voice-play-{account_id}")

    # Wait up to 15s for on_ready
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=15)
    except asyncio.TimeoutError:
        await stop_playing(account_id)
        return {"ok": False, "error": "Timeout connecting to voice (15s)"}

    if error_holder:
        _sessions.pop(account_id, None)
        return {"ok": False, "error": error_holder[0]}

    return {"ok": True, "playing": sound_file.name}


async def _disconnect_and_close(account_id: str, vc, client) -> None:
    try:
        if vc.is_connected():
            await vc.disconnect(force=True)
    except Exception:
        pass
    try:
        await client.close()
    except Exception:
        pass
    _sessions.pop(account_id, None)
    logger.info("voice_player: session closed for %s", account_id)


def list_sounds(sounds_dir: str) -> list[str]:
    """Return sorted list of playable audio filenames in the sounds directory."""
    exts = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".webm"}
    d = Path(sounds_dir)
    if not d.exists():
        return []
    return sorted(
        f.name for f in d.iterdir()
        if f.is_file() and f.suffix.lower() in exts
    )
