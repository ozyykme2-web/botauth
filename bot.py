"""
Discord Roblox Whitelist Bot
-----------------------------
/whitelist <roblox_user_id>  -> verifies the ID is a real Roblox account,
then grants the user a role that gives access to a specific channel.

Setup instructions are in README.md.
"""

import os
import logging
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("whitelist-bot")

# ---------- CONFIG (set these as environment variables, see README) ----------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
WHITELIST_ROLE_ID = int(os.environ["WHITELIST_ROLE_ID"])       # role that unlocks the channel
GUILD_ID = int(os.environ["GUILD_ID"])                         # your server's ID
# -------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True  # needed to add roles to members

bot = commands.Bot(command_prefix="!", intents=intents)


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


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
