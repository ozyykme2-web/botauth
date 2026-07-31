"""
Discord Roblox Whitelist Bot
-----------------------------
/whitelist <roblox_user_id> -> verifies the ID is a real Roblox account,
then grants the user a role that gives access to a specific channel.
Setup instructions are in README.md.
"""
import io
import os
import time
import logging
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageSequence
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("whitelist-bot")
# ---------- CONFIG (set these as environment variables, see README) ----------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
WHITELIST_ROLE_ID = int(os.environ["WHITELIST_ROLE_ID"])  # role that unlocks the channel
GUILD_ID = int(os.environ["GUILD_ID"])  # your server's ID
# Role allowed to use /forceunwhitelist and /say. Falls back to "Manage Roles"
# permission if this variable isn't set, so the bot still runs without it.
ADMIN_ROLE_ID = int(os.environ["ADMIN_ROLE_ID"]) if os.environ.get("ADMIN_ROLE_ID") else None
BOT_START_TIME = time.time()
# Channel to log bot usage into
LOG_CHANNEL_ID = 1532740214320267425
# -------------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True  # needed to add roles to members
bot = commands.Bot(command_prefix="!", intents=intents)

async def log_usage(guild: discord.Guild | None, content: str):
    """Send a short usage log to the configured log channel. Never raises."""
    if guild is None:
        return
    try:
        channel = guild.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        if channel is not None:
            await channel.send(content)
    except Exception as e:
        log.warning("Failed to send usage log: %s", e)

async def roblox_user_exists(user_id: str) -> tuple[bool, str | None]:
    """Check Roblox's public API to see if a user ID corresponds to a real account.
    Returns (exists, username_or_none)."""
    url = f"https://users.roblox.com/v1/users/{user_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                # Roblox returns isBanned field too; still counts as "real" account
                return True, data.get("name")
            return False, None

def is_staff(member: discord.Member) -> bool:
    """True if member has the configured admin role, or Manage Roles / Administrator perms."""
    if member.guild_permissions.administrator or member.guild_permissions.manage_roles:
        return True
    if ADMIN_ROLE_ID is not None:
        return any(r.id == ADMIN_ROLE_ID for r in member.roles)
    return False

@bot.event
async def on_ready():
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        log.info(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")
    except Exception as e:
        log.exception("Failed to sync commands: %s", e)
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.tree.command(
    name="whitelist",
    description="Whitelist yourself using your Roblox User ID",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(roblox_user_id="Your numeric Roblox User ID")
async def whitelist(interaction: discord.Interaction, roblox_user_id: str):
    await interaction.response.defer(ephemeral=True)
    if not roblox_user_id.isdigit():
        await interaction.followup.send(
            "❌ That doesn't look like a valid Roblox User ID (must be numbers only).",
            ephemeral=True,
        )
        return
    try:
        exists, username = await roblox_user_exists(roblox_user_id)
    except Exception as e:
        log.exception("Error contacting Roblox API: %s", e)
        await interaction.followup.send(
            "⚠️ Couldn't reach Roblox's servers right now. Try again in a moment.",
            ephemeral=True,
        )
        return
    if not exists:
        await interaction.followup.send(
            f"❌ No Roblox account found with ID `{roblox_user_id}`. Double-check the ID and try again.",
            ephemeral=True,
        )
        return
    # Grant the whitelist role
    guild = interaction.guild
    role = guild.get_role(WHITELIST_ROLE_ID)
    if role is None:
        await interaction.followup.send(
            "⚠️ Whitelist role isn't configured correctly. Contact an admin.",
            ephemeral=True,
        )
        return
    member = interaction.user
    try:
        await member.add_roles(role, reason=f"Whitelisted via Roblox ID {roblox_user_id}")
    except discord.Forbidden:
        await interaction.followup.send(
            "⚠️ I don't have permission to give you that role. Contact an admin (my role must be above the whitelist role).",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"✅ Verified! Roblox account **{username}** (`{roblox_user_id}`) is real. "
        f"You've been given access to the channel.",
        ephemeral=True,
    )
    await log_usage(
        guild,
        f"✅ **whitelist** — {member.mention} (`{member.id}`) verified Roblox **{username}** (`{roblox_user_id}`)",
    )

@bot.tree.command(
    name="info",
    description="Show info about this bot",
    guild=discord.Object(id=GUILD_ID),
)
async def info(interaction: discord.Interaction):
    uptime_seconds = int(time.time() - BOT_START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"
    embed = discord.Embed(title=f"{bot.user.name} — Info", color=discord.Color.blurple())
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Ping", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(
        name="Commands",
        value="/whitelist, /unwhitelist, /forceunwhitelist, /info, /say, /imagetogif",
        inline=False,
    )
    embed.set_footer(text=f"Bot ID: {bot.user.id}")
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await log_usage(
        interaction.guild,
        f"ℹ️ **info** — {interaction.user.mention} (`{interaction.user.id}`)",
    )

@bot.tree.command(
    name="unwhitelist",
    description="Remove your own whitelist access",
    guild=discord.Object(id=GUILD_ID),
)
async def unwhitelist(interaction: discord.Interaction):
    guild = interaction.guild
    role = guild.get_role(WHITELIST_ROLE_ID)
    if role is None:
        await interaction.response.send_message(
            "⚠️ Whitelist role isn't configured correctly. Contact an admin.",
            ephemeral=True,
        )
        return
    member = interaction.user
    if role not in member.roles:
        await interaction.response.send_message(
            "ℹ️ You don't currently have whitelist access.", ephemeral=True
        )
        return
    try:
        await member.remove_roles(role, reason="Self-unwhitelisted")
    except discord.Forbidden:
        await interaction.response.send_message(
            "⚠️ I don't have permission to remove that role. Contact an admin.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        "✅ You've been unwhitelisted and no longer have access to the channel.",
        ephemeral=True,
    )
    await log_usage(
        guild,
        f"🔓 **unwhitelist** — {member.mention} (`{member.id}`) removed their own whitelist",
    )

@bot.tree.command(
    name="forceunwhitelist",
    description="[Staff only] Remove whitelist access from another user",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(member="The member to unwhitelist")
async def forceunwhitelist(interaction: discord.Interaction, member: discord.Member):
    if not is_staff(interaction.user):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return
    guild = interaction.guild
    role = guild.get_role(WHITELIST_ROLE_ID)
    if role is None:
        await interaction.response.send_message(
            "⚠️ Whitelist role isn't configured correctly. Contact an admin.",
            ephemeral=True,
        )
        return
    if role not in member.roles:
        await interaction.response.send_message(
            f"ℹ️ {member.mention} doesn't currently have whitelist access.", ephemeral=True
        )
        return
    try:
        await member.remove_roles(role, reason=f"Force-unwhitelisted by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "⚠️ I don't have permission to remove that role from that member "
            "(check my role is above the whitelist role).",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"✅ {member.mention} has been forcefully unwhitelisted.", ephemeral=True
    )
    await log_usage(
        guild,
        f"🔨 **forceunwhitelist** — {interaction.user.mention} (`{interaction.user.id}`) removed whitelist from {member.mention} (`{member.id}`)",
    )

@bot.tree.command(
    name="say",
    description="[Staff only] Make the bot say something in this channel",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(message="What the bot should say")
async def say(interaction: discord.Interaction, message: str):
    if not is_staff(interaction.user):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return
    # Confirm privately first, then post publicly, so it's not silently anonymous to the requester
    await interaction.response.send_message("✅ Sent.", ephemeral=True)
    await interaction.channel.send(message)
    await log_usage(
        interaction.guild,
        f"📢 **say** — {interaction.user.mention} (`{interaction.user.id}`) in <#{interaction.channel.id}>: {message[:200]}{'…' if len(message) > 200 else ''}",
    )

@bot.tree.command(
    name="imagetogif",
    description="Convert an uploaded image into a GIF",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(image="The image to convert (png/jpg/webp/gif)")
async def imagetogif(interaction: discord.Interaction, image: discord.Attachment):
    await interaction.response.defer()
    if not (image.content_type and image.content_type.startswith("image/")):
        await interaction.followup.send("❌ That attachment isn't an image.", ephemeral=True)
        return
    MAX_SIZE = 8 * 1024 * 1024  # 8MB safety limit
    if image.size > MAX_SIZE:
        await interaction.followup.send(
            "❌ That image is too large to convert (max 8MB).", ephemeral=True
        )
        return
    try:
        image_bytes = await image.read()
        source = Image.open(io.BytesIO(image_bytes))
        output_buffer = io.BytesIO()
        if getattr(source, "is_animated", False):
            # Already an animated image (e.g. animated webp) -> re-encode all frames as GIF
            frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(source)]
            frames[0].save(
                output_buffer,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                loop=0,
                duration=source.info.get("duration", 100),
            )
        else:
            # Static image -> single-frame looping GIF
            source.convert("RGBA").save(output_buffer, format="GIF")
        output_buffer.seek(0)
    except Exception as e:
        log.exception("Error converting image to GIF: %s", e)
        await interaction.followup.send(
            "⚠️ Something went wrong converting that image.", ephemeral=True
        )
        return
    filename = os.path.splitext(image.filename)[0] + ".gif"
    await interaction.followup.send(
        content="✅ Here's your GIF:",
        file=discord.File(fp=output_buffer, filename=filename),
    )
    await log_usage(
        interaction.guild,
        f"🖼️ **imagetogif** — {interaction.user.mention} (`{interaction.user.id}`) converted `{image.filename}`",
    )

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
