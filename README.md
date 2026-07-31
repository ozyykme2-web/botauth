# Roblox Whitelist Discord Bot — Setup Guide

## What it does
A user runs `/whitelist <roblox_user_id>` in your server. The bot checks
Roblox's public API to confirm that ID belongs to a real account, and if so,
gives the user a Discord role. That role should be set up (in Discord's
channel permissions) as the only thing required to view your restricted
channel.

---

## 1. Create the Discord Bot

1. Go to https://discord.com/developers/applications → **New Application**.
2. Go to the **Bot** tab → **Add Bot**.
3. Under **Privileged Gateway Intents**, enable **Server Members Intent**.
4. Click **Reset Token** → copy the token (this is your `DISCORD_TOKEN`). Keep it secret.
5. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Manage Roles`, `Send Messages`, `View Channels`
6. Copy the generated URL, open it in your browser, and invite the bot to your server.

## 2. Set up the whitelist role & channel

1. In your Discord server, create a role (e.g. `Whitelisted`).
2. **Important:** In Server Settings → Roles, drag the bot's own role **above**
   the `Whitelisted` role (and above any staff/admin role you use for
   `ADMIN_ROLE_ID`, if it also needs role management) — a bot can only grant
   or remove roles ranked below its own.
3. Go to your restricted channel → Edit Channel → Permissions:
   - Deny `View Channel` for `@everyone`.
   - Add the `Whitelisted` role and allow `View Channel` for it.
4. Get the **Role ID**: enable Developer Mode (Settings → Advanced), then
   right-click the role in Server Settings → **Copy ID**.
5. Get the **Server (Guild) ID**: right-click the server icon → **Copy ID**.

## 3. Configure the bot

Set these three environment variables (see hosting steps below for exactly
where to put them):

```
DISCORD_TOKEN=your_bot_token_here
WHITELIST_ROLE_ID=123456789012345678
GUILD_ID=123456789012345678
ADMIN_ROLE_ID=123456789012345678
```

`ADMIN_ROLE_ID` is optional — it's the role allowed to use `/forceunwhitelist` and `/say`.
If you don't set it, anyone with the **Manage Roles** or **Administrator**
permission can still use those commands; the role is just an extra way to
grant access without giving full admin perms.

## 4. Install dependencies (only needed if testing locally first)

```bash
pip install -r requirements.txt
```

Run locally to test:
```bash
python bot.py
```

---

## How to use it (for your server members)

### `/whitelist <roblox_user_id>`
1. Type `/whitelist` in any channel the bot can see.
2. Enter your **Roblox User ID** (the numeric ID, found in your Roblox
   profile URL, e.g. `https://www.roblox.com/users/123456789/profile` → ID is `123456789`).
3. The bot checks Roblox and, if valid, instantly gives you the role that
   unlocks the restricted channel.

### `/unwhitelist`
Removes the whitelist role from **yourself**. Anyone can use this on their
own account at any time.

### `/forceunwhitelist <member>` — staff only
Removes the whitelist role from **another member**. Only usable by someone
with the `ADMIN_ROLE_ID` role, or the **Manage Roles**/**Administrator**
permission.

### `/info`
Shows the bot's uptime, ping, server count, and a list of available commands.
Anyone can use this.

### `/say <message>` — staff only
Makes the bot post a message in the current channel as itself. Restricted the
same way as `/forceunwhitelist` (staff role or Manage Roles/Administrator),
so it can't be used to impersonate or spam by regular members.

### `/imagetogif <image>`
Upload an image (png/jpg/webp/gif, up to 8MB) and the bot converts it into a
`.gif` file and sends it back in the channel. If you upload an already
animated image (like an animated webp), it preserves the animation frames;
static images become a single-frame looping GIF.

---

## Running it 24/7 without your PC on (free options)

Your computer doesn't need to run this — host it for free on one of these:

### Option A: Railway.app (easiest, free tier available)
1. Push this folder to a GitHub repo.
2. Go to https://railway.app → **New Project → Deploy from GitHub repo**.
3. Select your repo.
4. In the project's **Variables** tab, add `DISCORD_TOKEN`, `WHITELIST_ROLE_ID`, `GUILD_ID`.
5. Railway auto-detects Python and runs `python bot.py`. Done — it stays online continuously.

### Option B: Render.com
1. Push the folder to GitHub.
2. Render → **New → Background Worker** → connect your repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Add the same environment variables under **Environment**.

### Option C: A cheap VPS (e.g. Oracle Cloud free tier, DigitalOcean)
1. `git clone` your repo onto the server.
2. `pip install -r requirements.txt`
3. Run it persistently with a process manager so it restarts if it crashes/reboots:
   ```bash
   sudo apt install -y python3-pip
   pip3 install -r requirements.txt
   pip3 install pm2 -g   # or use systemd, shown below
   ```
   Or with **systemd** (recommended on a VPS), create `/etc/systemd/system/whitelistbot.service`:
   ```ini
   [Unit]
   Description=Roblox Whitelist Discord Bot
   After=network.target

   [Service]
   WorkingDirectory=/path/to/discord_bot
   Environment=DISCORD_TOKEN=your_token
   Environment=WHITELIST_ROLE_ID=123456789012345678
   Environment=GUILD_ID=123456789012345678
   ExecStart=/usr/bin/python3 bot.py
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```
   Then:
   ```bash
   sudo systemctl enable whitelistbot
   sudo systemctl start whitelistbot
   ```

Any of these keep the bot running continuously without your PC being on.

---

## Notes / Limitations

- The check only confirms the Roblox ID belongs to a **real, existing account** —
  it does not verify that the Discord user actually owns that Roblox account.
  If you need real ownership verification, you'd need an additional step (e.g.
  asking the user to put a one-time code in their Roblox profile "About" bio,
  which the bot checks before whitelisting). Let me know if you want that added.
- Slash commands sync instantly to the one server (`GUILD_ID`) specified —
  this is intentional so you don't have to wait up to an hour for global
  command sync.
