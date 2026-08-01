"""
Discord Roblox Whitelist Bot
-----------------------------
/whitelist <roblox_user_id>  -> verifies the ID is a real Roblox account,
then grants the user a role that gives access to a specific channel.

Every command invocation is also logged as a plain-text line to a
Supabase table (see the "command_logs" table + SUPABASE_URL / SUPABASE_KEY
env vars below).
"""

import io
import os
import time
import logging
import aiohttp
import aiomysql
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

# ---------- MySQL (Railway) ----------
MYSQL_HOST = os.environ["MYSQL_HOST"]
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", 3306))
MYSQL_USER = os.environ["MYSQL_USER"]
MYSQL_PASSWORD = os.environ["MYSQL_PASSWORD"]
MYSQL_DATABASE = os.environ["MYSQL_DATABASE"]
# ------------------------------------

# ---------- Supabase (plain-text command logging) ----------
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]  # service_role key (server-side only!)
SUPABASE_LOG_TABLE = os.environ.get("SUPABASE_LOG_TABLE", "command_logs")
# ------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.db_pool: aiomysql.Pool | None = None


# ---------- Database ----------

CREATE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS whitelist_attempts (
        id INT AUTO_INCREMENT PRIMARY KEY,
        discord_user_id BIGINT NOT NULL,
        discord_username VARCHAR(255) NOT NULL,
        roblox_user_id VARCHAR(64) NOT NULL,
        roblox_username VARCHAR(255) NULL,
        success BOOLEAN NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS whitelist_actions (
        id INT AUTO_INCREMENT PRIMARY KEY,
        action VARCHAR(32) NOT NULL,
        target_discord_id BIGINT NOT NULL,
        target_username VARCHAR(255) NOT NULL,
        actor_discord_id BIGINT NOT NULL,
        actor_username VARCHAR(255) NOT NULL,
        roblox_user_id VARCHAR(64) NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB
    """,
]


async def init_db_pool() -> aiomysql.Pool:
    pool = await aiomysql.create_pool(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        db=MYSQL_DATABASE,
        autocommit=True,
        minsize=1,
        maxsize=5,
    )
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for statement in CREATE_TABLES_SQL:
                await cur.execute(statement)
    log.info("Connected to MySQL and ensured tables exist.")
    return pool


async def log_whitelist_attempt(
    discord_user: discord.abc.User,
    roblox_user_id: str,
    roblox_username: str | None,
    success: bool,
):
    if bot.db_pool is None:
        log.warning("DB pool not ready, skipping log_whitelist_attempt")
        return
    try:
        async with bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO whitelist_attempts
                        (discord_user_id, discord_username, roblox_user_id, roblox_username, success)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (discord_user.id, str(discord_user), roblox_user_id, roblox_username, success),
                )
    except Exception as e:
        log.exception("Failed to log whitelist attempt: %s", e)


async def log_whitelist_action(
    action: str,
    target: discord.abc.User,
    actor: discord.abc.User,
    roblox_user_id: str | None = None,
):
    if bot.db_pool is None:
        log.warning("DB pool not ready, skipping log_whitelist_action")
        return
    try:
        async with bot.db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO whitelist_actions
                        (action, target_discord_id, target_username,
                         actor_discord_id, actor_username, roblox_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (action, target.id, str(target), actor.id, str(actor), roblox_user_id),
                )
    except Exception as e:
        log.exception("Failed to log whitelist action: %s", e)


async def log_to_supabase(log_text: str):
    """
    Insert a single plain-text log line into the Supabase `command_logs` table.
    This is a lightweight audit trail of every command run on the bot, separate
    from the structured MySQL tables above.
    """
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_LOG_TABLE}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    payload = {"log_text": log_text}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status not in (200, 201, 204):
                    body = await resp.text()
                    log.warning("Supabase logging failed (%s): %s", resp.status, body)
    except Exception as e:
        log.exception("Error sending log to Supabase: %s", e)


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
    if bot.db_pool is None:
        try:
            bot.db_pool = await init_db_pool()
        except Exception as e:
            log.exception("Failed to connect to MySQL: %s", e)

    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        log.info(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")
    except Exception as e:
        log.exception("Failed to sync commands: %s", e)

    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_close():
    if bot.db_pool is not None:
        bot.db_pool.close()
        await bot.db_pool.wait_closed()


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
        await log_to_supabase(
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
        await log_to_supabase(
            f"/whitelist by {fmt_user(interaction.user)} - error contacting Roblox API "
            f"for roblox_user_id={roblox_user_id}: {e}"
        )
        await interaction.followup.send(
            "⚠️ Couldn't reach Roblox's servers right now. Try again in a moment.",
            ephemeral=True,
        )
        return

    if not exists:
        await log_whitelist_attempt(interaction.user, roblox_user_id, None, success=False)
        await log_to_supabase(
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
        await log_to_supabase(
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
        await log_whitelist_attempt(interaction.user, roblox_user_id, username, success=False)
        await log_to_supabase(
            f"/whitelist by {fmt_user(interaction.user)} - failed: "
            f"missing permission to add whitelist role"
        )
        await interaction.followup.send(
            "⚠️ I don't have permission to give you that role. Contact an admin.",
            ephemeral=True,
        )
        return

    await log_whitelist_attempt(interaction.user, roblox_user_id, username, success=True)
    await log_whitelist_action("whitelist", target=member, actor=member, roblox_user_id=roblox_user_id)
    await log_to_supabase(
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
    await log_to_supabase(f"/info by {fmt_user(interaction.user)}")

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
        await log_to_supabase(
            f"/unwhitelist by {fmt_user(interaction.user)} - failed: whitelist role not configured"
        )
        await interaction.response.send_message(
            "⚠️ Whitelist role isn't configured correctly. Contact an admin.",
            ephemeral=True,
        )
        return

    member = interaction.user
    if role not in member.roles:
        await log_to_supabase(
            f"/unwhitelist by {fmt_user(interaction.user)} - no-op: user was not whitelisted"
        )
        await interaction.response.send_message(
            "ℹ️ You don't currently have whitelist access.", ephemeral=True
        )
        return

    try:
        await member.remove_roles(role, reason="Self-unwhitelisted")
    except discord.Forbidden:
        await log_to_supabase(
            f"/unwhitelist by {fmt_user(interaction.user)} - failed: "
            f"missing permission to remove whitelist role"
        )
        await interaction.response.send_message(
            "⚠️ I don't have permission to remove that role. Contact an admin.",
            ephemeral=True,
        )
        return

    await log_whitelist_action("unwhitelist", target=member, actor=member)
    await log_to_supabase(f"/unwhitelist by {fmt_user(interaction.user)} - success")

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
        await log_to_supabase(
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
        await log_to_supabase(
            f"/forceunwhitelist by {fmt_user(interaction.user)} - failed: "
            f"whitelist role not configured (target={fmt_user(member)})"
        )
        await interaction.response.send_message(
            "⚠️ Whitelist role isn't configured correctly. Contact an admin.",
            ephemeral=True,
        )
        return

    if role not in member.roles:
        await log_to_supabase(
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
        await log_to_supabase(
            f"/forceunwhitelist by {fmt_user(interaction.user)} - failed: "
            f"missing permission to remove role from target {fmt_user(member)}"
        )
        await interaction.response.send_message(
            "⚠️ I don't have permission to remove that role from that member.",
            ephemeral=True,
        )
        return

    await log_whitelist_action("forceunwhitelist", target=member, actor=interaction.user)
    await log_to_supabase(
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
        await log_to_supabase(
            f"/say by {fmt_user(interaction.user)} - denied: not staff"
        )
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    await log_to_supabase(
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
        await log_to_supabase(
            f"/imagetogif by {fmt_user(interaction.user)} - rejected: "
            f"attachment '{image.filename}' is not an image"
        )
        await interaction.followup.send("❌ That attachment isn't an image.", ephemeral=True)
        return

    MAX_SIZE = 8 * 1024 * 1024
    if image.size > MAX_SIZE:
        await log_to_supabase(
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
        await log_to_supabase(
            f"/imagetogif by {fmt_user(interaction.user)} - error converting "
            f"'{image.filename}': {e}"
        )
        await interaction.followup.send(
            "⚠️ Something went wrong converting that image.", ephemeral=True
        )
        return

    filename = os.path.splitext(image.filename)[0] + ".gif"
    await log_to_supabase(
        f"/imagetogif by {fmt_user(interaction.user)} - success: "
        f"converted '{image.filename}' -> '{filename}'"
    )
    await interaction.followup.send(
        content="✅ Here's your GIF:",
        file=discord.File(fp=output_buffer, filename=filename),
    )


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
