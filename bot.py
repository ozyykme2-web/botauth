"""
Discord Roblox Whitelist Bot
-----------------------------
/whitelist <roblox_user_id>  -> verifies the ID is a real Roblox account,
then grants the user a role that gives access to a specific channel.

All logging (every command invocation, success, failure, and error) is sent
as plain-text messages to a Discord webhook — no database required.
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

# ---------- CONFIG ----------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
WHITELIST_ROLE_ID = int(os.environ["WHITELIST_ROLE_ID"])
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ROLE_ID = int(os.environ["ADMIN_ROLE_ID"]) if os.environ.get("ADMIN_ROLE_ID") else None
BOT_START_TIME = time.time()

# ---------- Logging webhook ----------
LOG_WEBHOOK_URL = os.environ["LOG_WEBHOOK_URL"]
# -------------------------------------

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- Webhook logging ----------

async def log_event(text: str):
    """
    Send a plain-text log line to the configured Discord webhook.
    Used for every command invocation across the whole bot.
    """
    # Discord message limit is 2000 chars; keep a safety margin.
    if len(text) > 1900:
        text = text[:1900] + "... [truncated]"

    payload = {"content": text}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(LOG_WEBHOOK_URL, json=payload) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    log.warning("Webhook logging failed (%s): %s", resp.status, body)
    except Exception as e:
        log.exception("Error sending log to webhook: %s", e)


def fmt_user(user: discord.abc.User) -> str:
    return f"{user} ({user.id})"


# ---------- Helpers ----------

async def roblox_user_exists(user_id: str) -> tuple[bool, str | None]:
    url = f"https://users.roblox.com/v1/users/{user_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                return True, data.get("name")
            return False, None


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_roles:
        return True
    if ADMIN_ROLE_ID is not None:
        return any(r.id == ADMIN_ROLE_ID for r in member.roles)
    return False


# ---------- Events ----------

@bot.event
async def on_ready():
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        log.info(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")
    except Exception as e:
        log.exception("Failed to sync commands: %s", e)

    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await log_event(f"🟢 Bot started up — logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    cmd_name = interaction.command.name if interaction.command else "unknown"
    await log_event(
        f"❗ Unhandled error in /{cmd_name} used by {fmt_user(interaction.user)}: {error}"
    )
    log.exception("Unhandled app command error: %s", error)
    if not interaction.response.is_done():
        try:
            await interaction.response.send_message(
                "⚠️ Something went wrong running that command.", ephemeral=True
            )
        except Exception:
            pass


# ---------- Commands ----------

@bot.tree.command(
    name="whitelist",
    description="Whitelist yourself using your Roblox User ID",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(roblox_user_id="Your numeric Roblox User ID")
async def whitelist(interaction: discord.Interaction, roblox_user_id: str):
    await interaction.response.defer(ephemeral=True)

    if not roblox_user_id.isdigit():
        await log_event(
            f"/whitelist by {fmt_user(interaction.user)} - rejected: "
            f"invalid roblox_user_id format '{roblox_user_id}'"
        )
        await interaction.followup.send(
            "❌ That doesn't look like a valid Roblox User ID (must be numbers only).",
            ephemeral=True,
        )
        return

    try:
        exists, username = await roblox_user_exists(roblox_user_id)
    except Exception as e:
        log.exception("Error contacting Roblox API: %s", e)
        await log_event(
            f"/whitelist by {fmt_user(interaction.user)} - error contacting Roblox API "
            f"for roblox_user_id={roblox_user_id}: {e}"
        )
        await interaction.followup.send(
            "⚠️ Couldn't reach Roblox's servers right now. Try again in a moment.",
            ephemeral=True,
        )
        return

    if not exists:
        await log_event(
            f"/whitelist by {fmt_user(interaction.user)} - failed: "
            f"no Roblox account found for roblox_user_id={roblox_user_id}"
        )
        await interaction.followup.send(
            f"❌ No Roblox account found with ID `{roblox_user_id}`. Double-check the ID and try again.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    role = guild.get_role(WHITELIST_ROLE_ID)
    if role is None:
        await log_event(
            f"/whitelist by {fmt_user(interaction.user)} - failed: whitelist role not configured"
        )
        await interaction.followup.send(
            "⚠️ Whitelist role isn't configured correctly. Contact an admin.",
            ephemeral=True,
        )
        return

    member = interaction.user
    try:
        await member.add_roles(role, reason=f"Whitelisted via Roblox ID {roblox_user_id}")
    except discord.Forbidden:
        await log_event(
            f"/whitelist by {fmt_user(interaction.user)} - failed: "
            f"missing permission to add whitelist role"
        )
        await interaction.followup.send(
            "⚠️ I don't have permission to give you that role. Contact an admin.",
            ephemeral=True,
        )
        return

    await log_event(
        f"/whitelist by {fmt_user(interaction.user)} - success: "
        f"roblox_username={username} roblox_user_id={roblox_user_id}"
    )

    await interaction.followup.send(
        f"✅ Verified! Roblox account **{username}** (`{roblox_user_id}`) is real. "
        f"You've been given access to the channel.",
        ephemeral=True,
    )


@bot.tree.command(
    name="info",
    description="Show info about this bot",
    guild=discord.Object(id=GUILD_ID),
)
async def info(interaction: discord.Interaction):
    await log_event(f"/info by {fmt_user(interaction.user)}")

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


@bot.tree.command(
    name="unwhitelist",
    description="Remove your own whitelist access",
    guild=discord.Object(id=GUILD_ID),
)
async def unwhitelist(interaction: discord.Interaction):
    guild = interaction.guild
    role = guild.get_role(WHITELIST_ROLE_ID)
    if role is None:
        await log_event(
            f"/unwhitelist by {fmt_user(interaction.user)} - failed: whitelist role not configured"
        )
        await interaction.response.send_message(
            "⚠️ Whitelist role isn't configured correctly. Contact an admin.",
            ephemeral=True,
        )
        return

    member = interaction.user
    if role not in member.roles:
        await log_event(
            f"/unwhitelist by {fmt_user(interaction.user)} - no-op: user was not whitelisted"
        )
        await interaction.response.send_message(
            "ℹ️ You don't currently have whitelist access.", ephemeral=True
        )
        return

    try:
        await member.remove_roles(role, reason="Self-unwhitelisted")
    except discord.Forbidden:
        await log_event(
            f"/unwhitelist by {fmt_user(interaction.user)} - failed: "
            f"missing permission to remove whitelist role"
        )
        await interaction.response.send_message(
            "⚠️ I don't have permission to remove that role. Contact an admin.",
            ephemeral=True,
        )
        return

    await log_event(f"/unwhitelist by {fmt_user(interaction.user)} - success")

    await interaction.response.send_message(
        "✅ You've been unwhitelisted and no longer have access to the channel.",
        ephemeral=True,
    )


@bot.tree.command(
    name="forceunwhitelist",
    description="[Staff only] Remove whitelist access from another user",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(member="The member to unwhitelist")
async def forceunwhitelist(interaction: discord.Interaction, member: discord.Member):
    if not is_staff(interaction.user):
        await log_event(
            f"/forceunwhitelist by {fmt_user(interaction.user)} - denied: "
            f"not staff (target={fmt_user(member)})"
        )
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    guild = interaction.guild
    role = guild.get_role(WHITELIST_ROLE_ID)
    if role is None:
        await log_event(
            f"/forceunwhitelist by {fmt_user(interaction.user)} - failed: "
            f"whitelist role not configured (target={fmt_user(member)})"
        )
        await interaction.response.send_message(
            "⚠️ Whitelist role isn't configured correctly. Contact an admin.",
            ephemeral=True,
        )
        return

    if role not in member.roles:
        await log_event(
            f"/forceunwhitelist by {fmt_user(interaction.user)} - no-op: "
            f"target {fmt_user(member)} was not whitelisted"
        )
        await interaction.response.send_message(
            f"ℹ️ {member.mention} doesn't currently have whitelist access.", ephemeral=True
        )
        return

    try:
        await member.remove_roles(role, reason=f"Force-unwhitelisted by {interaction.user}")
    except discord.Forbidden:
        await log_event(
            f"/forceunwhitelist by {fmt_user(interaction.user)} - failed: "
            f"missing permission to remove role from target {fmt_user(member)}"
        )
        await interaction.response.send_message(
            "⚠️ I don't have permission to remove that role from that member.",
            ephemeral=True,
        )
        return

    await log_event(
        f"/forceunwhitelist by {fmt_user(interaction.user)} - success: "
        f"target={fmt_user(member)}"
    )

    await interaction.response.send_message(
        f"✅ {member.mention} has been forcefully unwhitelisted.", ephemeral=True
    )


@bot.tree.command(
    name="say",
    description="[Staff only] Make the bot say something in this channel",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(message="What the bot should say")
async def say(interaction: discord.Interaction, message: str):
    if not is_staff(interaction.user):
        await log_event(f"/say by {fmt_user(interaction.user)} - denied: not staff")
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    await log_event(
        f"/say by {fmt_user(interaction.user)} in #{interaction.channel} - message: {message}"
    )

    await interaction.response.send_message("✅ Sent.", ephemeral=True)
    await interaction.channel.send(message)


@bot.tree.command(
    name="imagetogif",
    description="Convert an uploaded image into a GIF",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(image="The image to convert (png/jpg/webp/gif)")
async def imagetogif(interaction: discord.Interaction, image: discord.Attachment):
    await interaction.response.defer()

    if not (image.content_type and image.content_type.startswith("image/")):
        await log_event(
            f"/imagetogif by {fmt_user(interaction.user)} - rejected: "
            f"attachment '{image.filename}' is not an image"
        )
        await interaction.followup.send("❌ That attachment isn't an image.", ephemeral=True)
        return

    MAX_SIZE = 8 * 1024 * 1024
    if image.size > MAX_SIZE:
        await log_event(
            f"/imagetogif by {fmt_user(interaction.user)} - rejected: "
            f"'{image.filename}' too large ({image.size} bytes)"
        )
        await interaction.followup.send(
            "❌ That image is too large to convert (max 8MB).", ephemeral=True
        )
        return

    try:
        image_bytes = await image.read()
        source = Image.open(io.BytesIO(image_bytes))

        output_buffer = io.BytesIO()
        if getattr(source, "is_animated", False):
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
            source.convert("RGBA").save(output_buffer, format="GIF")

        output_buffer.seek(0)
    except Exception as e:
        log.exception("Error converting image to GIF: %s", e)
        await log_event(
            f"/imagetogif by {fmt_user(interaction.user)} - error converting "
            f"'{image.filename}': {e}"
        )
        await interaction.followup.send(
            "⚠️ Something went wrong converting that image.", ephemeral=True
        )
        return

    filename = os.path.splitext(image.filename)[0] + ".gif"
    await log_event(
        f"/imagetogif by {fmt_user(interaction.user)} - success: "
        f"converted '{image.filename}' -> '{filename}'"
    )
    await interaction.followup.send(
        content="✅ Here's your GIF:",
        file=discord.File(fp=output_buffer, filename=filename),
    )


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
