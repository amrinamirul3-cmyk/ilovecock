import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import yt_dlp
import os
from collections import deque

# ─── Configuration ───────────────────────────────────────────────
# Paste your Discord Bot Token here or set env variable DISCORD_TOKEN
TOKEN = os.getenv("DISCORD_TOKEN", "MTQ3NTMwOTMwOTE0MjI0MTM3MA.G7_Dft.w2nRPRI8RyNrHFFameaD-RsA7l0pQhhLFknA2U")

# ─── yt-dlp options ──────────────────────────────────────────────
YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": False,          # allow playlist URLs
    "extract_flat": False,
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "opus",
    }],
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# ─── Bot setup ───────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# guild_id -> { "queue": deque, "current": dict|None, "voice_client": vc }
guilds: dict[int, dict] = {}


def get_guild_state(guild_id: int) -> dict:
    if guild_id not in guilds:
        guilds[guild_id] = {"queue": deque(), "current": None, "voice_client": None}
    return guilds[guild_id]


# ─── Helpers ─────────────────────────────────────────────────────

async def fetch_info(query: str) -> list[dict]:
    """Search YouTube or resolve a URL; returns list of track dicts."""
    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            # If it's not a URL treat it as a search query
            if not query.startswith("http"):
                info = ydl.extract_info(f"ytsearch:{query}", download=False)
                entries = info.get("entries", [info])
            else:
                info = ydl.extract_info(query, download=False)
                entries = info.get("entries", [info])
            return [
                {
                    "title": e.get("title", "Unknown"),
                    "url": e.get("url") or e.get("webpage_url"),
                    "webpage_url": e.get("webpage_url", ""),
                    "duration": e.get("duration", 0),
                    "thumbnail": e.get("thumbnail", ""),
                    "requester": None,
                }
                for e in entries
                if e
            ]

    return await loop.run_in_executor(None, _extract)


def format_duration(seconds: int) -> str:
    if not seconds:
        return "Live"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def play_next(guild_id: int):
    state = get_guild_state(guild_id)
    vc: discord.VoiceClient = state["voice_client"]

    if not vc or not vc.is_connected():
        return

    if not state["queue"]:
        state["current"] = None
        await asyncio.sleep(180)          # idle 3 min then disconnect
        if vc.is_connected() and not vc.is_playing():
            await vc.disconnect()
        return

    track = state["queue"].popleft()
    state["current"] = track

    source = discord.FFmpegPCMAudio(track["url"], **FFMPEG_OPTS)

    def after_play(error):
        if error:
            print(f"Player error: {error}")
        asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)

    vc.play(source, after=after_play)


# ─── Slash Commands ──────────────────────────────────────────────

@bot.tree.command(name="play", description="Play a YouTube video/song or add it to the queue.")
@app_commands.describe(query="YouTube URL or search term")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    # Must be in a voice channel
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ You need to be in a voice channel first!")

    vc_channel = interaction.user.voice.channel
    state = get_guild_state(interaction.guild_id)

    # Connect / move voice client
    vc = state["voice_client"]
    if vc and vc.is_connected():
        if vc.channel != vc_channel:
            await vc.move_to(vc_channel)
    else:
        vc = await vc_channel.connect()
        state["voice_client"] = vc

    await interaction.followup.send(f"🔍 Searching for: **{query}**…")

    try:
        tracks = await fetch_info(query)
    except Exception as e:
        return await interaction.followup.send(f"❌ Could not fetch: {e}")

    if not tracks:
        return await interaction.followup.send("❌ No results found.")

    for t in tracks:
        t["requester"] = interaction.user.display_name
        state["queue"].append(t)

    added = len(tracks)
    if added == 1:
        embed = discord.Embed(
            title="✅ Added to Queue",
            description=f"[{tracks[0]['title']}]({tracks[0]['webpage_url']})",
            color=discord.Color.green(),
        )
        embed.add_field(name="Duration", value=format_duration(tracks[0]["duration"]))
        embed.add_field(name="Position", value=str(len(state["queue"])))
        embed.set_thumbnail(url=tracks[0]["thumbnail"])
    else:
        embed = discord.Embed(
            title=f"✅ Added {added} tracks to Queue",
            color=discord.Color.green(),
        )

    await interaction.followup.send(embed=embed)

    if not vc.is_playing() and not vc.is_paused():
        await play_next(interaction.guild_id)


@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop(interaction: discord.Interaction):
    state = get_guild_state(interaction.guild_id)
    vc = state["voice_client"]

    if not vc or not vc.is_connected():
        return await interaction.response.send_message("❌ Not connected to any voice channel.")

    state["queue"].clear()
    state["current"] = None
    vc.stop()
    await vc.disconnect()
    await interaction.response.send_message("⏹️ Stopped and disconnected. Queue cleared.")


@bot.tree.command(name="skip", description="Skip the current track.")
async def skip(interaction: discord.Interaction):
    state = get_guild_state(interaction.guild_id)
    vc = state["voice_client"]

    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Nothing is playing right now.")

    vc.stop()  # triggers after_play → play_next
    await interaction.response.send_message("⏭️ Skipped!")


@bot.tree.command(name="pause", description="Pause the current track.")
async def pause(interaction: discord.Interaction):
    state = get_guild_state(interaction.guild_id)
    vc = state["voice_client"]

    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Paused.")
    else:
        await interaction.response.send_message("❌ Nothing is playing.")


@bot.tree.command(name="resume", description="Resume a paused track.")
async def resume(interaction: discord.Interaction):
    state = get_guild_state(interaction.guild_id)
    vc = state["voice_client"]

    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Resumed.")
    else:
        await interaction.response.send_message("❌ Nothing is paused.")


@bot.tree.command(name="queue", description="Show the current queue.")
async def queue_cmd(interaction: discord.Interaction):
    state = get_guild_state(interaction.guild_id)
    q = list(state["queue"])

    embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blurple())

    current = state["current"]
    if current:
        embed.add_field(
            name="Now Playing 🎶",
            value=f"[{current['title']}]({current['webpage_url']}) `{format_duration(current['duration'])}` — {current['requester']}",
            inline=False,
        )

    if not q:
        embed.description = "Queue is empty."
    else:
        lines = []
        for i, t in enumerate(q[:15], 1):
            lines.append(f"`{i}.` [{t['title']}]({t['webpage_url']}) `{format_duration(t['duration'])}` — {t['requester']}")
        if len(q) > 15:
            lines.append(f"… and {len(q) - 15} more tracks")
        embed.add_field(name=f"Up Next ({len(q)} tracks)", value="\n".join(lines), inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="remove", description="Remove a track from the queue by its position.")
@app_commands.describe(position="Position number shown in /queue (1 = first)")
async def remove(interaction: discord.Interaction, position: int):
    state = get_guild_state(interaction.guild_id)
    q = list(state["queue"])

    if position < 1 or position > len(q):
        return await interaction.response.send_message(f"❌ Invalid position. Queue has {len(q)} items.")

    removed = q.pop(position - 1)
    state["queue"] = deque(q)
    await interaction.response.send_message(f"🗑️ Removed **{removed['title']}** from position {position}.")


@bot.tree.command(name="nowplaying", description="Show what's currently playing.")
async def nowplaying(interaction: discord.Interaction):
    state = get_guild_state(interaction.guild_id)
    current = state["current"]

    if not current:
        return await interaction.response.send_message("❌ Nothing is playing right now.")

    embed = discord.Embed(
        title="🎶 Now Playing",
        description=f"[{current['title']}]({current['webpage_url']})",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Duration", value=format_duration(current["duration"]))
    embed.add_field(name="Requested by", value=current["requester"])
    embed.set_thumbnail(url=current["thumbnail"])
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="clearqueue", description="Clear all tracks from the queue without stopping playback.")
async def clearqueue(interaction: discord.Interaction):
    state = get_guild_state(interaction.guild_id)
    state["queue"].clear()
    await interaction.response.send_message("🗑️ Queue cleared!")


# ─── Bot events ──────────────────────────────────────────────────

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print("Slash commands synced. Bot is ready!")


bot.run(TOKEN)
