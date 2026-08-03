"""
Discord Roblox Whitelist Bot
-----------------------------
/login <license_key>         -> validates a KeyAuth license key. If valid,
                                 marks the Discord user as "logged in" and
                                 syncs the session list to JSONBin.io.
/whitelist <roblox_user_id>  -> (requires login) verifies the ID is a real
                                 Roblox account, then grants a role that
                                 gives access to a specific channel.

All commands except /login and /info require the user to be logged in via
KeyAuth first. The set of currently-logged-in Discord user IDs is kept in
memory and mirrored to a JSONBin.io bin, so it survives restarts and can be
inspected/edited externally if needed.

All command activity is also logged as plain text to a Discord webhook.
"""

import io
import os
import time
import hashlib
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
# Role that must be present before we log/record a user's Roblox username
CHECK_ROLE_ID = int(os.environ.get("CHECK_ROLE_ID", "1532682936996860024"))
BOT_START_TIME = time.time()

# ---------- Logging webhook ----------
LOG_WEBHOOK_URL = os.environ["LOG_WEBHOOK_URL"]

# ---------- KeyAuth ----------
KEYAUTH_NAME = os.environ["KEYAUTH_NAME"]                # your KeyAuth application name
KEYAUTH_OWNERID = os.environ["KEYAUTH_OWNERID"]           # your KeyAuth owner ID
KEYAUTH_APP_SECRET = os.environ["KEYAUTH_APP_SECRET"]     # your KeyAuth application secret
KEYAUTH_VERSION = os.environ.get("KEYAUTH_VERSION", "1.0")
KEYAUTH_API_URL = "https://keyauth.win/api/1.3/"          # KeyAuth's current API endpoint

# ---------- JSONBin.io ----------
JSONBIN_BIN_ID = os.environ["JSONBIN_BIN_ID"]
JSONBIN_API_KEY = os.environ["JSONBIN_API_KEY"]           # your "X-Master-Key"
JSONBIN_BASE_URL = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
# -----------------------------------------------------------

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory list of logged-in session records. Each record looks like:
# {
#   "discord_username": "someuser",
#   "discord_user_id": "123456789012345678",
#   "roblox_username": "SomeRobloxUser" or None,
#   "key": "XXXX-XXXX-XXXX-XXXX",
# }
bot.logged_in_users: list[dict] = []


# ---------- Webhook logging ----------

async def log_event(text: str):
    if len(text) > 1900:
        text = text[:1900] + "... [truncated]"
    payload = {"content": text}
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(LOG_WEBHOOK_URL, json=payload) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    log.warning("Webhook logging failed (%s): %s", resp.status, body)
    except Exception as e:
        log.exception("Error sending log to webhook: %s", e)


def fmt_user(user: discord.abc.User) -> str:
    return f"{user} ({user.id})"


# ---------- JSONBin sync ----------

async def jsonbin_load() -> dict:
    """Fetch the current bin contents. Returns {} if empty/unreachable."""
    headers = {"X-Master-Key": JSONBIN_API_KEY}
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{JSONBIN_BASE_URL}/latest", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("record", {}) or {}
                body = await resp.text()
                log.warning("JSONBin load failed (%s): %s", resp.status, body)
                return {}
    except Exception as e:
        log.exception("Error loading JSONBin: %s", e)
        return {}


async def jsonbin_save():
    """Push the current in-memory logged_in_users list to JSONBin."""
    headers = {
        "X-Master-Key": JSONBIN_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"logged_in_users": bot.logged_in_users}
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(JSONBIN_BASE_URL, headers=headers, json=payload) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    log.warning("JSONBin save failed (%s): %s", resp.status, body)
    except Exception as e:
        log.exception("Error saving JSONBin: %s", e)


def _find_record(user_id: int) -> dict | None:
    uid = str(user_id)
    for rec in bot.logged_in_users:
        if rec.get("discord_user_id") == uid:
            return rec
    return None


async def set_logged_in(
    user: discord.abc.User,
    key: str,
    roblox_username: str | None = None,
):
    """Add or update a session record, then re-sync to JSONBin."""
    existing = _find_record(user.id)
    if existing is not None:
        existing["discord_username"] = str(user)
        existing["key"] = key
        if roblox_username is not None:
            existing["roblox_username"] = roblox_username
    else:
        bot.logged_in_users.append(
            {
                "discord_username": str(user),
                "discord_user_id": str(user.id),
                "roblox_username": roblox_username,
                "whitelisted_active": False,
                "key": key,
            }
        )
    await jsonbin_save()


async def update_roblox_username(user_id: int, roblox_username: str, has_check_role: bool):
    """
    Update the whitelist status on an existing session record.
    The Roblox username is only recorded if the member currently has
    CHECK_ROLE_ID; `whitelisted_active` always reflects that role check.
    """
    rec = _find_record(user_id)
    if rec is not None:
        rec["whitelisted_active"] = has_check_role
        if has_check_role:
            rec["roblox_username"] = roblox_username
        await jsonbin_save()


async def set_logged_out(user_id: int):
    before = len(bot.logged_in_users)
    bot.logged_in_users = [
        rec for rec in bot.logged_in_users if rec.get("discord_user_id") != str(user_id)
    ]
    if len(bot.logged_in_users) != before:
        await jsonbin_save()


def is_logged_in(user_id: int) -> bool:
    return _find_record(user_id) is not None


# ---------- KeyAuth ----------

async def keyauth_check_key(license_key: str, hwid: str) -> tuple[bool, str]:
    """
    Validates a license key against KeyAuth. Performs init + license() as
    KeyAuth's API requires an init/session step before checking a license.
    Returns (is_valid, message).
    """
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        init_payload = {
            "type": "init",
            "name": KEYAUTH_NAME,
            "ownerid": KEYAUTH_OWNERID,
            "ver": KEYAUTH_VERSION,
            "hash": hashlib.sha256(KEYAUTH_APP_SECRET.encode()).hexdigest(),
        }
        try:
            async with session.post(KEYAUTH_API_URL, data=init_payload) as resp:
                init_data = await resp.json()
        except Exception as e:
            log.exception("KeyAuth init request failed: %s", e)
            return False, "Could not reach KeyAuth right now."

        if not init_data.get("success"):
            return False, init_data.get("message", "KeyAuth session init failed.")

        session_id = init_data.get("sessionid")

        license_payload = {
            "type": "license",
            "key": license_key,
            "hwid": hwid,
            "sessionid": session_id,
            "name": KEYAUTH_NAME,
            "ownerid": KEYAUTH_OWNERID,
        }
        try:
            async with session.post(KEYAUTH_API_URL, data=license_payload) as resp:
                lic_data = await resp.json()
        except Exception as e:
            log.exception("KeyAuth license check failed: %s", e)
            return False, "Could not reach KeyAuth right now."

        if lic_data.get("success"):
            return True, "Key is valid."
        return False, lic_data.get("message", "Invalid key.")


def make_hwid(user_id: int) -> str:
    """
    Discord bots have no real hardware to fingerprint, so we derive a stable
    per-user pseudo-HWID from their Discord ID. This satisfies KeyAuth's
    hwid-locking feature without needing a real machine identifier.
    """
    return hashlib.sha256(f"discord-{user_id}".encode()).hexdigest()


# ---------- Helpers ----------

async def roblox_user_exists(user_id: str) -> tuple[bool, str | None]:
    url = f"https://users.roblox.com/v1/users/{user_id}"
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
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


async def require_login(interaction: discord.Interaction) -> bool:
    """
    Call at the top of any gated command. Sends a rejection message and
    returns False if the user hasn't logged in via /login yet.
    """
    if is_logged_in(interaction.user.id):
        return True

    await log_event(
        f"🔒 {fmt_user(interaction.user)} tried /{interaction.command.name} without being logged in"
    )
    if interaction.response.is_done():
        await interaction.followup.send(
            "🔒 You need to log in first. Use `/login <key>` with a valid KeyAuth key.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "🔒 You need to log in first. Use `/login <key>` with a valid KeyAuth key.",
            ephemeral=True,
        )
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

    # Restore session state from JSONBin on startup
    record = await jsonbin_load()
    bot.logged_in_users = record.get("logged_in_users", []) or []
    log.info(f"Restored {len(bot.logged_in_users)} logged-in session(s) from JSONBin")

    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await log_event(
        f"🟢 Bot started up — logged in as {bot.user} (ID: {bot.user.id}); "
        f"restored {len(bot.logged_in_users)} session(s) from JSONBin"
    )


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
    name="login",
    description="Log in with your KeyAuth license key to unlock bot commands",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(key="Your KeyAuth license key")
async def login(interaction: discord.Interaction, key: str):
    await interaction.response.defer(ephemeral=True)

    if is_logged_in(interaction.user.id):
        await interaction.followup.send("ℹ️ You're already logged in.", ephemeral=True)
        return

    hwid = make_hwid(interaction.user.id)
    try:
        valid, message = await keyauth_check_key(key, hwid)
    except Exception as e:
        log.exception("KeyAuth check errored: %s", e)
        await log_event(
            f"/login by {fmt_user(interaction.user)} - error contacting KeyAuth: {e}"
        )
        await interaction.followup.send(
            "⚠️ Couldn't reach KeyAuth right now. Try again in a moment.", ephemeral=True
        )
        return

    if not valid:
        await log_event(
            f"/login by {fmt_user(interaction.user)} - rejected: {message}"
        )
        await interaction.followup.send(f"❌ Login failed: {message}", ephemeral=True)
        return

    await set_logged_in(interaction.user, key)
    await log_event(
        f"🔑 Key redeemed — discord_username={interaction.user} "
        f"discord_user_id={interaction.user.id} roblox_username=(not set yet) "
        f"key={key}"
    )

    await interaction.followup.send(
        "✅ Key verified! You're now logged in and can use the bot's commands.",
        ephemeral=True,
    )


@bot.tree.command(
    name="logout",
    description="Log out and revoke your current session",
    guild=discord.Object(id=GUILD_ID),
)
async def logout(interaction: discord.Interaction):
    if not is_logged_in(interaction.user.id):
        await interaction.response.send_message("ℹ️ You're not currently logged in.", ephemeral=True)
        return

    await set_logged_out(interaction.user.id)
    await log_event(f"/logout by {fmt_user(interaction.user)} - session removed, JSONBin synced")

    await interaction.response.send_message("✅ You've been logged out.", ephemeral=True)


@bot.tree.command(
    name="whitelist",
    description="Whitelist yourself using your Roblox User ID",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(roblox_user_id="Your numeric Roblox User ID")
async def whitelist(interaction: discord.Interaction, roblox_user_id: str):
    await interaction.response.defer(ephemeral=True)
    if not await require_login(interaction):
        return

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

    # Check whether the member actually holds the required role before we
    # log or store their Roblox username; whitelisted_active always reflects
    # this check regardless.
    has_check_role = any(r.id == CHECK_ROLE_ID for r in member.roles)

    await update_roblox_username(interaction.user.id, username, has_check_role)

    if has_check_role:
        await log_event(
            f"✅ Whitelisted — discord_username={interaction.user} "
            f"discord_user_id={interaction.user.id} roblox_username={username} "
            f"whitelisted_active=true"
        )
    else:
        await log_event(
            f"⚠️ Whitelisted — discord_username={interaction.user} "
            f"discord_user_id={interaction.user.id} does not have required role "
            f"({CHECK_ROLE_ID}); roblox_username not recorded, whitelisted_active=false"
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
    embed.add_field(name="Logged-in sessions", value=str(len(bot.logged_in_users)), inline=True)
    embed.add_field(
        name="Commands",
        value="/login, /logout, /whitelist, /unwhitelist, /forceunwhitelist, /info, /say, /imagetogif",
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
    if not await require_login(interaction):
        return

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

    # Unwhitelisting also revokes their KeyAuth session + JSONBin entry
    await set_logged_out(member.id)
    await log_event(
        f"/unwhitelist by {fmt_user(interaction.user)} - success (session revoked, JSONBin synced)"
    )

    await interaction.response.send_message(
        "✅ You've been unwhitelisted, logged out, and no longer have access to the channel.",
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

    await set_logged_out(member.id)
    await log_event(
        f"/forceunwhitelist by {fmt_user(interaction.user)} - success: "
        f"target={fmt_user(member)} (session revoked, JSONBin synced)"
    )

    await interaction.response.send_message(
        f"✅ {member.mention} has been forcefully unwhitelisted and logged out.", ephemeral=True
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
    if not await require_login(interaction):
        return

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
