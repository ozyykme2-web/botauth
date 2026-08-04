"""
Discord Roblox Whitelist Bot
-----------------------------
/login <license_key> -> validates a KeyAuth license key. If valid,
                                 marks the Discord user as "logged in".
/whitelist <roblox_username> -> (requires login) verifies the username is a real
                                 Roblox account, then grants a role.
All commands except /login and /info require the user to be logged in via
KeyAuth first. Login sessions are in-memory only.
Whitelisted Roblox usernames are mirrored to JSONBin.io.
"""
import io
import os
import json
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

REQUIRED_ENV_VARS = [
    "DISCORD_TOKEN",
    "WHITELIST_ROLE_ID",
    "GUILD_ID",
    "LOG_WEBHOOK_URL",
    "KEYAUTH_NAME",
    "KEYAUTH_OWNERID",
    "KEYAUTH_APP_SECRET",
    "JSONBIN_BIN_ID",
    "JSONBIN_API_KEY",
]
_missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
if _missing:
    log.error(
        "Missing required environment variable(s): %s. "
        "Set these in Railway's Variables tab, then redeploy.",
        ", ".join(_missing),
    )
    raise SystemExit(1)

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
WHITELIST_ROLE_ID = int(os.environ["WHITELIST_ROLE_ID"])
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ROLE_ID = int(os.environ["ADMIN_ROLE_ID"]) if os.environ.get("ADMIN_ROLE_ID") else None
CHECK_ROLE_ID = int(os.environ.get("CHECK_ROLE_ID", "1532682936996860024"))
BOT_START_TIME = time.time()

LOG_WEBHOOK_URL = os.environ["LOG_WEBHOOK_URL"]

KEYAUTH_NAME = os.environ["KEYAUTH_NAME"]
KEYAUTH_OWNERID = os.environ["KEYAUTH_OWNERID"]
KEYAUTH_APP_SECRET = os.environ["KEYAUTH_APP_SECRET"]
KEYAUTH_VERSION = os.environ.get("KEYAUTH_VERSION", "1.0")
KEYAUTH_API_URL = "https://keyauth.win/api/1.3/"

JSONBIN_BIN_ID = os.environ["JSONBIN_BIN_ID"]
JSONBIN_API_KEY = os.environ["JSONBIN_API_KEY"]
JSONBIN_BASE_URL = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

bot.logged_in_users: dict[str, dict] = {}
bot.roblox_usernames: list[str] = []

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

async def jsonbin_load() -> dict:
    headers = {"X-Master-Key": JSONBIN_API_KEY}
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{JSONBIN_BASE_URL}/latest", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    record = data.get("record", {}) or {}
                    if isinstance(record, dict):
                        return record
                    return {}
                body = await resp.text()
                log.warning("JSONBin load failed (%s): %s", resp.status, body)
                return {}
    except Exception as e:
        log.exception("Error loading JSONBin: %s", e)
        return {}

async def jsonbin_save():
    headers = {
        "X-Master-Key": JSONBIN_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"roblox_usernames": bot.roblox_usernames}
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(JSONBIN_BASE_URL, headers=headers, json=payload) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    log.warning("JSONBin save failed (%s): %s", resp.status, body)
    except Exception as e:
        log.exception("Error saving JSONBin: %s", e)

async def add_roblox_username(username: str):
    if username not in bot.roblox_usernames:
        bot.roblox_usernames.append(username)
        await jsonbin_save()

async def remove_roblox_username(username: str | None):
    if username and username in bot.roblox_usernames:
        bot.roblox_usernames.remove(username)
        await jsonbin_save()

def set_logged_in(user: discord.abc.User, key: str):
    bot.logged_in_users[str(user.id)] = {"key": key, "roblox_username": None}

def set_logged_out(user_id: int):
    bot.logged_in_users.pop(str(user_id), None)

def is_logged_in(user_id: int) -> bool:
    return str(user_id) in bot.logged_in_users

def get_session_roblox_username(user_id: int) -> str | None:
    rec = bot.logged_in_users.get(str(user_id))
    return rec.get("roblox_username") if rec else None

async def update_roblox_username(user_id: int, roblox_username: str, has_check_role: bool):
    rec = bot.logged_in_users.get(str(user_id))
    old_username = rec.get("roblox_username") if rec else None
    if rec is not None:
        rec["roblox_username"] = roblox_username if has_check_role else old_username
    if has_check_role:
        if old_username and old_username != roblox_username:
            await remove_roblox_username(old_username)
        await add_roblox_username(roblox_username)

async def keyauth_check_key(license_key: str, hwid: str) -> tuple[bool, str]:
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        init_params = {
            "type": "init",
            "name": KEYAUTH_NAME,
            "ownerid": KEYAUTH_OWNERID,
            "ver": KEYAUTH_VERSION,
            "hash": hashlib.sha256(KEYAUTH_APP_SECRET.encode()).hexdigest(),
        }
        try:
            async with session.get(KEYAUTH_API_URL, params=init_params) as resp:
                init_data = await resp.json(content_type=None)
        except Exception as e:
            log.exception("KeyAuth init request failed: %s", e)
            return False, "Could not reach KeyAuth right now."
        if not init_data.get("success"):
            return False, init_data.get("message", "KeyAuth session init failed.")
        session_id = init_data.get("sessionid")
        license_params = {
            "type": "license",
            "key": license_key,
            "hwid": hwid,
            "sessionid": session_id,
            "name": KEYAUTH_NAME,
            "ownerid": KEYAUTH_OWNERID,
        }
        try:
            async with session.get(KEYAUTH_API_URL, params=license_params) as resp:
                lic_data = await resp.json(content_type=None)
        except Exception as e:
            log.exception("KeyAuth license check failed: %s", e)
            return False, "Could not reach KeyAuth right now."
        if lic_data.get("success"):
            return True, "Key is valid."
        return False, lic_data.get("message", "Invalid key.")

def make_hwid(user_id: int) -> str:
    return hashlib.sha256(f"discord-{user_id}".encode()).hexdigest()

async def roblox_user_exists(username: str) -> tuple[bool, str | None, str | None]:
    url = "https://users.roblox.com/v1/usernames/users"
    timeout = aiohttp.ClientTimeout(total=8)
    payload = {"usernames": [username], "excludeBannedUsers": False}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                return False, None, None
            data = await resp.json()
            results = data.get("data") or []
            if not results:
                return False, None, None
            match = results[0]
            return True, match.get("name"), str(match.get("id"))

def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_roles:
        return True
    if ADMIN_ROLE_ID is not None:
        return any(r.id == ADMIN_ROLE_ID for r in member.roles)
    return False

async def require_login(interaction: discord.Interaction) -> bool:
    if is_logged_in(interaction.user.id):
        return True
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

@bot.event
async def on_ready():
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        log.info(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")
    except Exception as e:
        log.exception("Failed to sync commands: %s", e)
    record = await jsonbin_load()
    usernames = record.get("roblox_usernames", []) if isinstance(record, dict) else []
    bot.roblox_usernames = [u for u in usernames if isinstance(u, str)]
    log.info(f"Restored {len(bot.roblox_usernames)} roblox username(s) from JSONBin")
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Unhandled app command error: %s", error)
    if not interaction.response.is_done():
        try:
            await interaction.response.send_message(
                "⚠️ Something went wrong running that command.", ephemeral=True
            )
        except Exception:
            pass

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
        await interaction.followup.send(
            "⚠️ Couldn't reach KeyAuth right now. Try again in a moment.", ephemeral=True
        )
        return
    if not valid:
        await interaction.followup.send(f"❌ Login failed: {message}", ephemeral=True)
        return
    set_logged_in(interaction.user, key)
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
    set_logged_out(interaction.user.id)
    await interaction.response.send_message("✅ You've been logged out.", ephemeral=True)

@bot.tree.command(
    name="whitelist",
    description="Whitelist yourself using your Roblox username",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(roblox_username="Your Roblox username")
async def whitelist(interaction: discord.Interaction, roblox_username: str):
    await interaction.response.defer(ephemeral=True)
    if not await require_login(interaction):
        return
    try:
        exists, username, roblox_user_id = await roblox_user_exists(roblox_username)
    except Exception as e:
        log.exception("Error contacting Roblox API: %s", e)
        await interaction.followup.send(
            "⚠️ Couldn't reach Roblox's servers right now. Try again in a moment.",
            ephemeral=True,
        )
        return
    if not exists:
        await interaction.followup.send(
            f"❌ No Roblox account found with username `{roblox_username}`. "
            f"Double-check the spelling and try again.",
            ephemeral=True,
        )
        return
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
        await member.add_roles(role, reason=f"Whitelisted via Roblox username {username}")
    except discord.Forbidden:
        await interaction.followup.send(
            "⚠️ I don't have permission to give you that role. Contact an admin.",
            ephemeral=True,
        )
        return
    has_check_role = any(r.id == CHECK_ROLE_ID for r in member.roles)
    await update_roblox_username(interaction.user.id, username, has_check_role)
    if has_check_role:
        await log_event(json.dumps({"roblox_username": username}))
    await interaction.followup.send(
        f"✅ Verified! Roblox account **{username}** (ID `{roblox_user_id}`) is real. "
        f"You've been given access to the channel.",
        ephemeral=True,
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
    roblox_username = get_session_roblox_username(member.id)
    await remove_roblox_username(roblox_username)
    set_logged_out(member.id)
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
            "⚠️ I don't have permission to remove that role from that member.",
            ephemeral=True,
        )
        return
    roblox_username = get_session_roblox_username(member.id)
    await remove_roblox_username(roblox_username)
    set_logged_out(member.id)
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
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return
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
        await interaction.followup.send("❌ That attachment isn't an image.", ephemeral=True)
        return
    MAX_SIZE = 8 * 1024 * 1024
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
        await interaction.followup.send(
            "⚠️ Something went wrong converting that image.", ephemeral=True
        )
        return
    filename = os.path.splitext(image.filename)[0] + ".gif"
    await interaction.followup.send(
        content="✅ Here's your GIF:",
        file=discord.File(fp=output_buffer, filename=filename),
    )

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
