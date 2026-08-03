"""
Discord Roblox Whitelist Bot
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

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
WHITELIST_ROLE_ID = int(os.environ["WHITELIST_ROLE_ID"])
GUILD_ID = int(os.environ["GUILD_ID"])
ADMIN_ROLE_ID = int(os.environ["ADMIN_ROLE_ID"]) if os.environ.get("ADMIN_ROLE_ID") else None
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

# Structure: DiscordID → {discord_user, key, roblox_id, roblox_username, timestamp}
bot.logged_in_users: dict[str, dict] = {}

async def log_event(text: str):
    if len(text) > 1900:
        text = text[:1900] + "... [truncated]"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(LOG_WEBHOOK_URL, json={"content": text}) as resp:
                if resp.status not in (200, 204):
                    log.warning("Webhook failed (%s)", resp.status)
    except Exception as e:
        log.exception("Webhook error: %s", e)

def fmt_user(user: discord.abc.User) -> str:
    return f"{user} ({user.id})"

async def jsonbin_load() -> dict:
    headers = {"X-Master-Key": JSONBIN_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{JSONBIN_BASE_URL}/latest", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("record", {}) or {}
                return {}
    except Exception:
        return {}

async def jsonbin_save():
    headers = {"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.put(JSONBIN_BASE_URL, headers=headers, json={"logged_in_users": bot.logged_in_users}) as resp:
                if resp.status not in (200, 201):
                    log.warning("JSONBin save failed (%s)", resp.status)
    except Exception as e:
        log.exception("JSONBin save error: %s", e)

async def set_logged_in(user: discord.User, key: str):
    bot.logged_in_users[str(user.id)] = {
        "discord_user": str(user),
        "discord_id": str(user.id),
        "key": key,
        "roblox_id": None,
        "roblox_username": None,
        "logged_in_at": int(time.time())
    }
    await jsonbin_save()

async def set_logged_out(user_id: int):
    if str(user_id) in bot.logged_in_users:
        del bot.logged_in_users[str(user_id)]
        await jsonbin_save()

def is_logged_in(user_id: int) -> bool:
    return str(user_id) in bot.logged_in_users

async def keyauth_check_key(license_key: str, hwid: str) -> tuple[bool, str]:
    async with aiohttp.ClientSession() as session:
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
        except Exception:
            return False, "Could not reach KeyAuth."

        if not init_data.get("success"):
            return False, init_data.get("message", "Init failed.")

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
        except Exception:
            return False, "Could not reach KeyAuth."

        if lic_data.get("success"):
            return True, "Key is valid."
        return False, lic_data.get("message", "Invalid key.")

def make_hwid(user_id: int) -> str:
    return hashlib.sha256(f"discord-{user_id}".encode()).hexdigest()

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

async def require_login(interaction: discord.Interaction) -> bool:
    if is_logged_in(interaction.user.id):
        return True
    await log_event(f"🔒 {fmt_user(interaction.user)} tried /{interaction.command.name} without login")
    msg = "🔒 You need to log in first. Use `/login <key>`."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return False

@bot.event
async def on_ready():
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        log.info(f"Synced {len(synced)} commands")
    except Exception as e:
        log.exception("Sync failed: %s", e)

    record = await jsonbin_load()
    bot.logged_in_users = record.get("logged_in_users", {}) or {}
    log.info(f"Restored {len(bot.logged_in_users)} sessions")
    await log_event(f"🟢 Bot online as {bot.user} | Restored {len(bot.logged_in_users)} sessions")

@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await log_event(f"❗ Error in /{interaction.command.name if interaction.command else '?'} by {fmt_user(interaction.user)}: {error}")
    if not interaction.response.is_done():
        try:
            await interaction.response.send_message("⚠️ Something went wrong.", ephemeral=True)
        except Exception:
            pass

@bot.tree.command(name="login", description="Log in with your KeyAuth key", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(key="Your KeyAuth license key")
async def login(interaction: discord.Interaction, key: str):
    await interaction.response.defer(ephemeral=True)
    if is_logged_in(interaction.user.id):
        await interaction.followup.send("ℹ️ Already logged in.", ephemeral=True)
        return

    hwid = make_hwid(interaction.user.id)
    valid, message = await keyauth_check_key(key, hwid)
    if not valid:
        await log_event(f"/login by {fmt_user(interaction.user)} rejected: {message}")
        await interaction.followup.send(f"❌ Login failed: {message}", ephemeral=True)
        return

    await set_logged_in(interaction.user, key)
    await log_event(f"/login SUCCESS | {fmt_user(interaction.user)} | key={key}")
    await interaction.followup.send("✅ Logged in! You can now use `/whitelist`.", ephemeral=True)

@bot.tree.command(name="logout", description="Log out", guild=discord.Object(id=GUILD_ID))
async def logout(interaction: discord.Interaction):
    if not is_logged_in(interaction.user.id):
        await interaction.response.send_message("ℹ️ Not logged in.", ephemeral=True)
        return
    await set_logged_out(interaction.user.id)
    await log_event(f"/logout by {fmt_user(interaction.user)}")
    await interaction.response.send_message("✅ Logged out.", ephemeral=True)

@bot.tree.command(name="whitelist", description="Whitelist with your Roblox User ID", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(roblox_user_id="Your numeric Roblox User ID")
async def whitelist(interaction: discord.Interaction, roblox_user_id: str):
    await interaction.response.defer(ephemeral=True)
    if not await require_login(interaction):
        return

    if not roblox_user_id.isdigit():
        await interaction.followup.send("❌ Roblox ID must be numbers only.", ephemeral=True)
        return

    exists, username = await roblox_user_exists(roblox_user_id)
    if not exists:
        await interaction.followup.send(f"❌ No Roblox account found for `{roblox_user_id}`.", ephemeral=True)
        return

    role = interaction.guild.get_role(WHITELIST_ROLE_ID)
    if role is None:
        await interaction.followup.send("⚠️ Whitelist role not configured.", ephemeral=True)
        return

    try:
        await interaction.user.add_roles(role, reason=f"Whitelisted via Roblox {roblox_user_id}")
    except discord.Forbidden:
        await interaction.followup.send("⚠️ I can't give you the role.", ephemeral=True)
        return

    # Full log to JSONBin
    entry = bot.logged_in_users.get(str(interaction.user.id), {})
    entry.update({
        "discord_user": str(interaction.user),
        "discord_id": str(interaction.user.id),
        "roblox_id": roblox_user_id,
        "roblox_username": username,
        "whitelisted_at": int(time.time())
    })
    bot.logged_in_users[str(interaction.user.id)] = entry
    await jsonbin_save()

    await log_event(
        f"/whitelist SUCCESS | Discord: {fmt_user(interaction.user)} | "
        f"Key: {entry.get('key')} | Roblox: {username} ({roblox_user_id})"
    )
    await interaction.followup.send(
        f"✅ Whitelisted **{username}** (`{roblox_user_id}`). Access granted.",
        ephemeral=True,
    )

@bot.tree.command(name="unwhitelist", description="Remove your whitelist", guild=discord.Object(id=GUILD_ID))
async def unwhitelist(interaction: discord.Interaction):
    if not await require_login(interaction):
        return
    role = interaction.guild.get_role(WHITELIST_ROLE_ID)
    if role is None or role not in interaction.user.roles:
        await interaction.response.send_message("ℹ️ You're not whitelisted.", ephemeral=True)
        return
    try:
        await interaction.user.remove_roles(role, reason="Self-unwhitelist")
    except discord.Forbidden:
        await interaction.response.send_message("⚠️ Can't remove role.", ephemeral=True)
        return
    await set_logged_out(interaction.user.id)
    await log_event(f"/unwhitelist by {fmt_user(interaction.user)}")
    await interaction.response.send_message("✅ Unwhitelisted + logged out.", ephemeral=True)

@bot.tree.command(name="forceunwhitelist", description="[Staff] Force remove whitelist", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="Member to unwhitelist")
async def forceunwhitelist(interaction: discord.Interaction, member: discord.Member):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    role = interaction.guild.get_role(WHITELIST_ROLE_ID)
    if role is None or role not in member.roles:
        await interaction.response.send_message(f"ℹ️ {member.mention} not whitelisted.", ephemeral=True)
        return
    try:
        await member.remove_roles(role, reason=f"Force by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("⚠️ Missing permissions.", ephemeral=True)
        return
    await set_logged_out(member.id)
    await log_event(f"/forceunwhitelist by {fmt_user(interaction.user)} → {fmt_user(member)}")
    await interaction.response.send_message(f"✅ {member.mention} force-unwhitelisted.", ephemeral=True)

@bot.tree.command(name="info", description="Bot info", guild=discord.Object(id=GUILD_ID))
async def info(interaction: discord.Interaction):
    uptime = int(time.time() - BOT_START_TIME)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    embed = discord.Embed(title=f"{bot.user.name}", color=discord.Color.blurple())
    embed.add_field(name="Uptime", value=f"{h}h {m}m {s}s", inline=True)
    embed.add_field(name="Ping", value=f"{round(bot.latency*1000)}ms", inline=True)
    embed.add_field(name="Sessions", value=str(len(bot.logged_in_users)), inline=True)
    embed.add_field(name="Commands", value="/login /logout /whitelist /unwhitelist /whoami /sessions /avatar /say /imagetogif", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="whoami", description="Show your stored session info", guild=discord.Object(id=GUILD_ID))
async def whoami(interaction: discord.Interaction):
    if not await require_login(interaction):
        return
    data = bot.logged_in_users.get(str(interaction.user.id), {})
    embed = discord.Embed(title="Your Session", color=discord.Color.green())
    embed.add_field(name="Discord", value=data.get("discord_user", "—"), inline=False)
    embed.add_field(name="Key", value=f"`{data.get('key', '—')}`", inline=False)
    embed.add_field(name="Roblox", value=f"{data.get('roblox_username') or 'Not set'} (`{data.get('roblox_id') or '—'}`)", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="sessions", description="[Staff] View all logged-in sessions", guild=discord.Object(id=GUILD_ID))
async def sessions(interaction: discord.Interaction):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    if not bot.logged_in_users:
        await interaction.response.send_message("No active sessions.", ephemeral=True)
        return
    lines = []
    for uid, data in list(bot.logged_in_users.items())[:20]:
        lines.append(
            f"**{data.get('discord_user', uid)}**\n"
            f"Key: `{data.get('key')}` | Roblox: {data.get('roblox_username') or '—'} (`{data.get('roblox_id') or '—'}`)"
        )
    embed = discord.Embed(title=f"Active Sessions ({len(bot.logged_in_users)})", description="\n\n".join(lines), color=discord.Color.orange())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="avatar", description="Get someone's Discord or Roblox avatar", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="Discord member (optional)")
async def avatar(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    embed = discord.Embed(title=f"{target.display_name}'s Avatar", color=discord.Color.blurple())
    embed.set_image(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="say", description="[Staff] Make the bot say something", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(message="Message to send")
async def say(interaction: discord.Interaction, message: str):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        return
    await interaction.response.send_message("✅ Sent.", ephemeral=True)
    await interaction.channel.send(message)

@bot.tree.command(name="imagetogif", description="Convert image to GIF", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(image="Image to convert")
async def imagetogif(interaction: discord.Interaction, image: discord.Attachment):
    await interaction.response.defer()
    if not await require_login(interaction):
        return
    if not (image.content_type and image.content_type.startswith("image/")):
        await interaction.followup.send("❌ Not an image.", ephemeral=True)
        return
    if image.size > 8 * 1024 * 1024:
        await interaction.followup.send("❌ Max 8MB.", ephemeral=True)
        return
    try:
        img_bytes = await image.read()
        source = Image.open(io.BytesIO(img_bytes))
        buf = io.BytesIO()
        if getattr(source, "is_animated", False):
            frames = [f.convert("RGBA") for f in ImageSequence.Iterator(source)]
            frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], loop=0, duration=source.info.get("duration", 100))
        else:
            source.convert("RGBA").save(buf, format="GIF")
        buf.seek(0)
        filename = os.path.splitext(image.filename)[0] + ".gif"
        await interaction.followup.send("✅ Here's your GIF:", file=discord.File(fp=buf, filename=filename))
    except Exception as e:
        await interaction.followup.send("⚠️ Conversion failed.", ephemeral=True)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
