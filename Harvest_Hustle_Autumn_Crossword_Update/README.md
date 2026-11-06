# 🍂 Harvest Hustle — Seven-Day Discord Bot

A self-hosted Discord game for a seven-day autumn farming championship. Players have **two 12-hour sessions per day**, **10 planting actions per session**, private menus, a shared leaderboard and automatic final results.

## Quick start

1. Create a Discord application and bot in the Discord Developer Portal. Invite it with **bot** and **applications.commands** scopes, and **Send Messages**, **View Channels**, **Embed Links** permissions in the event channel.
2. Install Python 3.11+ and run `pip install -r requirements.txt`.
3. Configure the environment variables in `.env.example` (set them in your host's environment; the bot does not load `.env` files automatically).
4. Run `python bot.py`. With `GUILD_ID` set, slash commands sync to that server.
5. Host: `/harvest start`. Players: `/harvest join`, `/harvest farm`.

## Railway

Push these files to GitHub and deploy as a Railway service. Set `DISCORD_TOKEN`, `GUILD_ID`, `HOST_IDS`, `EVENT_CHANNEL_ID`, `EVENT_ROLE_ID`, and `DATABASE_PATH` in Railway Variables. Use start command `python bot.py`.

**Important:** Railway's ephemeral filesystem may be reset on redeploy. Attach a **persistent Railway Volume** mounted at `/data`, then set `DATABASE_PATH=/data/harvest.sqlite3`. Without a persistent volume, a redeploy could erase the game. Only run **one** bot instance against the database. Back up the database before any maintenance.

## Commands

- `/harvest start` — host-only, opens the 168-hour event immediately.
- `/harvest join` — joins the active event.
- `/harvest farm` — opens your private farm and interactive buttons.
- `/harvest leaderboard` — public ranking of the top 15 farms.
- `/harvest rules` — complete game rules.
- `/harvest status` — event start/end and participant count.
- `/harvest reset confirm:RESET` — host-only, wipes completed event data; cannot reset a live event.

## Mechanics

Each player starts with 2,000 coins and eight plots. The game has 14 sessions, each 12 hours long, measured from the exact event start. Ten planting actions refresh at each new session and do not accumulate. Planting one seed costs one planting action; harvesting **all mature crops**, selling **all inventory**, and purchasing upgrades cost **zero planting actions**. Crop maturity is based on elapsed sessions and does not require background jobs.

| Crop | Seed | Sale | Sessions to mature |
| --- | ---: | ---: | ---: |
| Carrots | 80 | 200 | 1 |
| Corn | 160 | 340 | 2 |
| Apples | 280 | 560 | 3 |
| Pumpkins | 440 | 820 | 4 |
| Golden Gourds | 720 | 1,220 | 6 |

Upgrades: Extra Plot costs 1,000, up to four; Market Stall costs 1,700, up to two, each providing a 10% selling bonus. Market bonus is applied at sale and for stored-produce valuation. Extra Plot retains 500 coins as final farm value; Market Stall retains 848 coins per level.

**Final farm value** = coins + unsold harvested produce value (including market bonuses) + upgrade resale value. Crops still growing at event close have zero value. Players must harvest them before the deadline to count. The bot picks the winner by highest farm value, then lifetime sales, then number of crops harvested, then earlier join time, then lowest Discord user ID. This ensures a deterministic single champion, though the last two tie rules are administrative rather than skill-based.

Late joiners are allowed but do not receive missed actions. A background task checks once a minute for event completion; if the bot is offline at the deadline, results will be announced after restart. The SQLite database persists the event and player progress. No privileged Message Content or Members intents are required.

## Event scheduling note

`/harvest start` begins the seven-day clock immediately. Schedule the launch at a convenient time (e.g. 9 AM or 10 AM UK time) so both 12-hour sessions fall at predictable times. The game uses Unix timestamps and is unaffected by local daylight-saving changes, although local displayed clock times may shift.

## Testing before going live

Use a test server and test bot or a separate database. Verify planting, harvesting after a session boundary, selling, upgrade purchases, action resets, and end-of-event results. The real event cannot be reset while live; use a fresh test database for rehearsal.

## Balance test

The included `balance_simulation.py` is a deterministic policy comparison across 14 sessions, not an exhaustive proof of optimality. It compares five crop-focused policies and a profit-per-growing-session heuristic with 36 upgrade schedules per policy (216 schedules total). See `balance_report.csv` and `BALANCE_REPORT.md`. Crop prices and upgrade costs were adjusted from the original version. **Use a fresh test database** for a new event; changing rules during an active competition would be unfair.

## Reward rebalancing
See `REWARD_UPDATE.md` and run `python reward_simulation.py` to reproduce the 216 sampled strategy outcomes. This release scales all currency amounts 4× and adds harvest milestone badges and a next-reset timestamp. Start with a **fresh database**; do not apply during an active competition.

## Upgrade feasibility

**Balance warning:** These action/plot changes alter the economy. Run a full strategy simulation and live test before a prize event.

**Version 3 simulation note:** See `UPGRADE_FEASIBILITY.md` and `upgrade_feasibility.csv`. The previous balance simulations were for different action rules and should not be treated as valid for this version.

## v4 balanced economy

See `BALANCE_V4_REPORT.md` and `balance_v4_results.csv`. Current crop costs and upgrade pricing are defined in `bot.py`. This update is intended for a **new event**, not a live economy migration.

## Current v5 upgrade prices
Extra Plot: **500 coins** each, maximum 4, **250 coins final scoring credit** each. Market Stall: **800 coins** each, maximum 2, **+6% sale price per level**, **400 coins final scoring credit** each. Ten planting actions and eight initial plots per 12-hour session. These values supersede any earlier balancing notes.

## Persistent event role panel (new)

Run **`/harvest role-panel`** as an authorized host. The bot posts an autumn-themed opt-in role panel to the configured `EVENT_CHANNEL_ID` (default `1567980502965551144`). The button gives/removes `EVENT_ROLE_ID` (default `1567982021106602065`) when a member clicks it. Button interactions are private; the panel survives bot restarts. Running the command again updates the existing panel instead of posting a duplicate, provided the original message remains in the configured channel. If the panel message was deleted, the command posts a replacement.

**Required permissions:** Manage Roles (bot's highest role must be above the event role), View Channel, Send Messages, Embed Links. The role is for announcement notifications and is **separate** from `/harvest join` registration. Users may remove their role without leaving the competition. The panel uses a persistent Discord component ID; keep the same bot and SQLite database for reliable management.

**Current game rules:** 8 starting plots, maximum 12 plots; 500 coins per Extra Plot; 800 coins per Market Stall level (+6% each, maximum 2). The existing final-scoring credits remain enabled in this release (250 per Extra Plot, 400 per Market Stall level).
