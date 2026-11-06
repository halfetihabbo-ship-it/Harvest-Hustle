# Harvest Hustle deployment update
import os
import sqlite3
import asyncio
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ.get('DISCORD_TOKEN')
GUILD_ID = int(os.environ.get('GUILD_ID', '0'))
HOST_IDS = {int(x.strip()) for x in os.environ.get('HOST_IDS', '1501587787294703657').split(',') if x.strip()}
CHANNEL_ID = int(os.environ.get('EVENT_CHANNEL_ID', '1567980502965551144'))
ROLE_PANEL_CHANNEL_ID = int(os.environ.get('ROLE_PANEL_CHANNEL_ID', str(CHANNEL_ID)))
ROLE_ID = int(os.environ.get('EVENT_ROLE_ID', '1567982021106602065'))
DB_PATH = os.environ.get('DATABASE_PATH', 'harvest.sqlite3')
PANEL_BANNER = Path(__file__).with_name('harvest_hustle_role_banner.png')
WINDOW = 12 * 3600
DURATION = 14 * WINDOW
FARMING_AT = int(datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc).timestamp())
ACTIONS = 10
START_COINS = 2000
PLOTS = 10
CROPS = {
    'carrot': {'name':'🥕 Carrots', 'cost':80, 'sale':230, 'hours':2},
    'corn': {'name':'🌽 Corn', 'cost':130, 'sale':320, 'hours':4},
    'apple': {'name':'🍎 Apples', 'cost':200, 'sale':440, 'hours':6},
    'pumpkin': {'name':'🎃 Pumpkins', 'cost':250, 'sale':540, 'hours':8},
    'gourd': {'name':'🍂 Golden Gourds', 'cost':250, 'sale':500, 'hours':12},
}
UPGRADES = {'extra_plot': {'name':'🌱 Extra Plot', 'cost':500, 'max':10, 'resale':250}, 'market': {'name':'🛒 Market Stall', 'cost':1200, 'max':2, 'resale':600}}

def plot_price(extra_owned):
    return 500 if extra_owned < 4 else (900 if extra_owned < 7 else 1500)

def plot_resale_value(extra_owned):
    return sum(plot_price(i) // 2 for i in range(extra_owned))

def crop_ready_ts(plot, event_row):
    planted = plot['planted_ts']
    # Backward compatibility for farms planted before the timestamp migration.
    if not planted:
        planted = event_row['start_ts'] + plot['planted_window'] * WINDOW
    return int(planted) + CROPS[plot['crop']]['hours'] * 3600 - plot['boost_seconds']

# All game changes occur under one lock and are committed together.
lock = asyncio.Lock()
con = sqlite3.connect(DB_PATH, check_same_thread=False)
con.row_factory = sqlite3.Row
con.execute('PRAGMA journal_mode=WAL')
con.execute('PRAGMA busy_timeout=5000')
con.executescript('''
CREATE TABLE IF NOT EXISTS event(id INTEGER PRIMARY KEY CHECK(id=1), start_ts INTEGER, ended INTEGER DEFAULT 0, winner_id INTEGER);
CREATE TABLE IF NOT EXISTS players(user_id INTEGER PRIMARY KEY, joined_ts INTEGER NOT NULL, coins INTEGER NOT NULL DEFAULT 2000, actions_used INTEGER NOT NULL DEFAULT 0, action_window INTEGER NOT NULL DEFAULT -1, extra_plot INTEGER NOT NULL DEFAULT 0, market INTEGER NOT NULL DEFAULT 0, harvested INTEGER NOT NULL DEFAULT 0, sold INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS plots(user_id INTEGER NOT NULL, slot INTEGER NOT NULL, crop TEXT NOT NULL, planted_window INTEGER NOT NULL, PRIMARY KEY(user_id,slot));
CREATE TABLE IF NOT EXISTS inventory(user_id INTEGER NOT NULL, crop TEXT NOT NULL, qty INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,crop));
''')
# Additive schema migrations for existing installations.
def add_column(table, column, definition):
    cols = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
    if column not in cols:
        con.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
add_column('event','registration_ts','INTEGER')
add_column('event','live_test','INTEGER NOT NULL DEFAULT 0')
add_column('event','registration_post_id','INTEGER')
add_column('event','registration_channel_id','INTEGER')
add_column('event','last_session_notice','INTEGER NOT NULL DEFAULT -1')
add_column('event','midweek_sent','INTEGER NOT NULL DEFAULT 0')
add_column('event','final_sent','INTEGER NOT NULL DEFAULT 0')
add_column('plots','planted_ts','INTEGER')
add_column('plots','boost_seconds','INTEGER NOT NULL DEFAULT 0')
add_column('plots','boost_used','INTEGER NOT NULL DEFAULT 0')
add_column('players','planted','INTEGER NOT NULL DEFAULT 0')
add_column('players','actions_total','INTEGER NOT NULL DEFAULT 0')
add_column('players','upgrades_bought','INTEGER NOT NULL DEFAULT 0')
add_column('players','objectives_claimed','INTEGER NOT NULL DEFAULT 0')
con.execute('''CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    ts INTEGER NOT NULL, session INTEGER NOT NULL, action TEXT NOT NULL,
    detail TEXT NOT NULL, coin_change INTEGER NOT NULL DEFAULT 0
)''')
con.execute('''CREATE TABLE IF NOT EXISTS objective_claims (
    user_id INTEGER NOT NULL, day INTEGER NOT NULL, objective TEXT NOT NULL,
    PRIMARY KEY(user_id,day,objective)
)''')
con.execute('''CREATE TABLE IF NOT EXISTS harvest_alerts (
    user_id INTEGER NOT NULL, ready_ts INTEGER NOT NULL,
    PRIMARY KEY(user_id, ready_ts)
)''')
con.commit()

# Separate persistent databases: live tests never touch official player tables.
# A one-time, backed-up cleanup is authorized for the pre-launch legacy test data.
official_con = con
TEST_DB_PATH = str(Path(DB_PATH).with_name(Path(DB_PATH).stem + '_test.sqlite3'))
official_con.execute('CREATE TABLE IF NOT EXISTS harvest_runtime (id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL)')
official_con.execute("INSERT OR IGNORE INTO harvest_runtime(id,mode) VALUES(1,'official')")
official_con.execute('CREATE TABLE IF NOT EXISTS harvest_migrations (name TEXT PRIMARY KEY)')
if not official_con.execute("SELECT 1 FROM harvest_migrations WHERE name='prelaunch_cleanup_v1'").fetchone():
    # Backup the original SQLite database before clearing legacy test players.
    legacy_backup = str(Path(DB_PATH).with_name(Path(DB_PATH).stem + '_prelaunch_backup.sqlite3'))
    if not Path(legacy_backup).exists():
        backup_con = sqlite3.connect(legacy_backup)
        official_con.backup(backup_con)
        backup_con.close()
    for table in ('harvest_alerts','transactions','objective_claims','inventory','plots','players','event'):
        official_con.execute(f'DELETE FROM {table}')
    official_con.execute("UPDATE harvest_runtime SET mode='official' WHERE id=1")
    official_con.execute("INSERT INTO harvest_migrations(name) VALUES('prelaunch_cleanup_v1')")
    official_con.commit()
    logging.warning('Cleared legacy prelaunch game records; database backup saved to %s', legacy_backup)

test_con = sqlite3.connect(TEST_DB_PATH, check_same_thread=False)
test_con.row_factory = sqlite3.Row
test_con.execute('PRAGMA journal_mode=WAL')
test_con.execute('PRAGMA busy_timeout=5000')
# Mirror the official schema without copying player records.
for schema_row in official_con.execute("SELECT type,name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
    if schema_row['name'] in ('harvest_runtime', 'harvest_migrations'):
        continue
    test_con.execute(schema_row['sql'].replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1)) if schema_row['sql'].startswith('CREATE TABLE ') else None
test_con.commit()

def test_mode_active():
    return official_con.execute('SELECT mode FROM harvest_runtime WHERE id=1').fetchone()['mode'] == 'test'

con = test_con if test_mode_active() else official_con

def clear_test_tables():
    for table in ('harvest_alerts','transactions','objective_claims','inventory','plots','players','event'):
        test_con.execute(f'DELETE FROM {table}')
    test_con.commit()

def event():
    return con.execute('SELECT * FROM event WHERE id=1').fetchone()

def current_window(e):
    return max(0, min(13, (int(time.time()) - e['start_ts']) // WINDOW))

def registration_open(e):
    return e is not None and not e['ended'] and time.time() < e['start_ts'] + DURATION and bool(e['registration_post_id'])

def started(e):
    return e is not None and time.time() >= e['start_ts']

def active(e):
    return e is not None and not e['ended'] and e['start_ts'] <= int(time.time()) < e['start_ts'] + DURATION

def player(uid):
    return con.execute('SELECT * FROM players WHERE user_id=?', (uid,)).fetchone()

def sync_actions(uid, e):
    w = current_window(e)
    p = player(uid)
    if p and p['action_window'] != w:
        con.execute('UPDATE players SET actions_used=0,action_window=? WHERE user_id=?', (w,uid))

def use_action(uid):
    con.execute('UPDATE players SET actions_used=actions_used+1,actions_total=actions_total+1 WHERE user_id=?',(uid,))

def log_action(uid, e, action, detail, delta=0):
    con.execute('INSERT INTO transactions(user_id,ts,session,action,detail,coin_change) VALUES(?,?,?,?,?,?)', (uid,int(time.time()),current_window(e),action,detail,delta))

def available_plots(p):
    return PLOTS + p['extra_plot']

def inventory_value(uid, p):
    multiplier = 1 + p['market'] * 0.06
    return sum(int(q['qty'] * CROPS[q['crop']]['sale'] * multiplier) for q in con.execute('SELECT * FROM inventory WHERE user_id=?',(uid,)))

def farm_value(uid, p, e):
    # Unharvested crops have no end-of-event value; the rule is visible throughout.
    return p['coins'] + inventory_value(uid,p) + plot_resale_value(p['extra_plot']) + p['market'] * UPGRADES['market']['resale']

def board(e):
    rows=[]
    for p in con.execute('SELECT * FROM players'):
        rows.append((farm_value(p['user_id'],p,e),p['sold'],p['harvested'],p['user_id']))
    # Tie rule: farm value, lifetime sales, lifetime harvests, then earliest join.
    rows.sort(key=lambda x:(-x[0],-x[1],-x[2],player(x[3])['joined_ts'],x[3]))
    return rows

def stamp(ts):
    return f'<t:{int(ts)}:F> (<t:{int(ts)}:R>)'

# Consistent, lightweight autumn styling. No external images needed for private actions.
AUTUMN = {'orange': 0xC47736, 'green': 0x657B43, 'gold': 0xD6A448,
          'brown': 0x79523D, 'red': 0xA95343}

def make_embed(title, description='', color=AUTUMN['orange']):
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text='🍂 Harvest Hustle  •  Your autumn farming adventure')
    return embed

def action_embed(message, kind='info'):
    """Convert existing gameplay results into a clear private autumn card."""
    success = not any(word in message.lower() for word in (
        'not enough', 'not currently active', 'not active', 'no crops',
        'nothing to sell', 'maximum', 'no planting actions', 'all plots',
        'not joined', 'not ready', 'wait until', 'not currently', 'ended'))
    names = {
        'plant': ('🌱 Seeds in the Soil', '🌱 Planting Update'),
        'harvest': ('🧺 Harvest Complete', '⏳ Harvest Update'),
        'sell': ('🪙 Produce Sold', '🧺 Market Update'),
        'upgrade': ('🏡 Farm Expanded', '🏡 Upgrade Shop'),
        'info': ('🍁 Farm Update', '🍁 Farm Update'),
    }
    title = names.get(kind, names['info'])[0 if success else 1]
    color = AUTUMN['green'] if success else AUTUMN['gold']
    embed = make_embed(title, message, color)
    if success and kind == 'harvest':
        embed.add_field(name='Next step', value='Click **Sell Produce** to turn harvested crops into coins.', inline=False)
    elif success and kind == 'plant':
        embed.add_field(name='Next step', value='Your crop grows automatically. Return when the timer finishes and click **Harvest Ready**.', inline=False)
    elif success and kind == 'sell':
        embed.add_field(name='Next step', value='Reinvest your earnings with **Plant** or **Upgrades**.', inline=False)
    return embed


def is_host(interaction):
    return interaction.user.id in HOST_IDS

class FarmView(discord.ui.View):
    def __init__(self, uid):
        super().__init__(timeout=1800)
        self.uid=uid

    async def interaction_check(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message('This is someone else’s private farm menu. Use `/harvest` for yours.',ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Plant',emoji='🌱',style=discord.ButtonStyle.success,row=0)
    async def plant(self,interaction,button):
        await interaction.response.send_message(embed=make_embed('🌱 Choose Your Seeds', 'Select a crop below to plant in your next empty plot. Each seed costs **1 planting action**.', AUTUMN['green']),view=CropView(self.uid),ephemeral=True)

    @discord.ui.button(label='Harvest Ready',emoji='🧺',style=discord.ButtonStyle.primary,row=0)
    async def harvest(self,interaction,button):
        await do_harvest(interaction,self.uid)

    @discord.ui.button(label='Sell Produce',emoji='🪙',style=discord.ButtonStyle.primary,row=0)
    async def sell(self,interaction,button):
        await do_sell(interaction,self.uid)

    @discord.ui.button(label='Upgrades',emoji='⬆️',style=discord.ButtonStyle.secondary,row=1)
    async def upgrades(self,interaction,button):
        await interaction.response.send_message(embed=make_embed('🏡 The Farm Shop', 'Expand your growing space or increase your selling bonus. Upgrades cost coins, but **no planting actions**.', AUTUMN['brown']),view=UpgradeView(self.uid),ephemeral=True)

    @discord.ui.button(label='Daily Missions',emoji='🎯',style=discord.ButtonStyle.secondary,row=2)
    async def objectives(self,interaction,button):
        await show_objectives(interaction)

    @discord.ui.button(label='Refresh Farm',emoji='🔄',style=discord.ButtonStyle.secondary,row=1)
    async def refresh(self,interaction,button):
        await show_farm(interaction)

    @discord.ui.button(label='Save Farm to DMs', emoji='📩', style=discord.ButtonStyle.secondary, row=3)
    async def save_farm(self, interaction, button):
        # The game channel remains ephemeral; this is an optional lasting copy.
        async with lock:
            e=event()
            p=player(self.uid)
            if not e or not p:
                await interaction.response.send_message('Join the game first.', ephemeral=True)
                return
            coins=p['coins']
            value=farm_value(self.uid,p,e)
            inv=', '.join(f"{CROPS[r['crop']]['name']} ×{r['qty']}" for r in
                          con.execute('SELECT * FROM inventory WHERE user_id=? AND qty>0',(self.uid,))) or 'Empty'
            ready=sum(1 for r in con.execute('SELECT * FROM plots WHERE user_id=?',(self.uid,))
                      if crop_ready_ts(r,e)<=int(time.time()))
            msg=make_embed('🍁 Your Harvest Hustle Farm — Saved Copy',
                f'**Coins:** {coins:,}c\n**Farm value:** {value:,}c\n'
                f'**Plots:** {available_plots(p)}/20\n**Crops ready:** {ready}\n'
                f'**Inventory:** {inv}\n\nOpen `/harvest farm` in the game channel to play.')
        try:
            await interaction.user.send(embed=msg)
            await interaction.response.send_message('📩 A lasting copy of your farm summary was sent to your DMs.', ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message('Could not send a DM. Check your server DM privacy settings.', ephemeral=True)

    @discord.ui.button(label='Close', emoji='✖️', style=discord.ButtonStyle.secondary, row=2)
    async def close(self, interaction, button):
        # Explicitly dismiss only this private farm panel when the player chooses.
        await interaction.response.edit_message(view=None)
        self.stop()


class CropSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='Select a crop',options=[discord.SelectOption(label=f"{v['name']} — {v['cost']}c",value=k,description=f"Ready in {v['hours']} hours; sells for {v['sale']}c") for k,v in CROPS.items()])
    async def callback(self,interaction):
        uid=interaction.user.id
        crop=self.values[0]
        async with lock:
            e=event()
            if not active(e):
                msg='The event is not currently active.'
            elif not player(uid):
                msg='Join the game using the Join Harvest Hustle button in the main game channel.'
            else:
                sync_actions(uid,e)
                p=player(uid)
                used={r['slot'] for r in con.execute('SELECT slot FROM plots WHERE user_id=?',(uid,))}
                free=next((i for i in range(1,available_plots(p)+1) if i not in used),None)
                c=CROPS[crop]
                if p['actions_used']>=ACTIONS: msg='No planting actions left this session.'
                elif free is None: msg='All plots are occupied. Harvest mature crops first.'
                elif p['coins']<c['cost']: msg='Not enough coins for that seed.'
                else:
                    con.execute('INSERT INTO plots(user_id,slot,crop,planted_window,planted_ts) VALUES(?,?,?,?,?)',(uid,free,crop,current_window(e),int(time.time())))
                    con.execute('UPDATE players SET coins=coins-? WHERE user_id=?',(c['cost'],uid))
                    use_action(uid)
                    con.execute('UPDATE players SET planted=planted+1 WHERE user_id=?',(uid,))
                    log_action(uid,e,'plant',crop,-c['cost'])
                    msg=f"Planted {c['name']} in plot {free} for {c['cost']} coins. Ready at {stamp(int(time.time())+c['hours']*3600)}, if the event is still running."
                con.commit()
        await show_farm(interaction, latest_action='🌱 **Planting:** ' + msg, edit_existing=True)

class CropView(discord.ui.View):
    def __init__(self,uid):
        super().__init__(timeout=180)
        self.uid=uid
        self.add_item(CropSelect())
    async def interaction_check(self,interaction):
        if interaction.user.id!=self.uid:
            await interaction.response.send_message('Open your own menu with `/harvest`.',ephemeral=True)
            return False
        return True

class UpgradeView(discord.ui.View):
    def __init__(self,uid):
        super().__init__(timeout=180)
        self.uid=uid
    async def interaction_check(self,interaction):
        if interaction.user.id!=self.uid:
            await interaction.response.send_message('Open your own menu with `/harvest`.',ephemeral=True)
            return False
        return True
    @discord.ui.button(label='Extra Plot · from 500c',emoji='🌱',style=discord.ButtonStyle.success)
    async def plot(self,interaction,button):
        await buy_upgrade(interaction,'extra_plot')
    @discord.ui.button(label='Market Stall · 1200c',emoji='🛒',style=discord.ButtonStyle.primary)
    async def market(self,interaction,button):
        await buy_upgrade(interaction,'market')

async def buy_upgrade(interaction,kind):
    uid=interaction.user.id
    cfg=UPGRADES[kind]
    async with lock:
        e=event()
        if not active(e) or not player(uid): msg='The event is not active or you have not joined.'
        else:
            sync_actions(uid,e)
            p=player(uid)
            price=plot_price(p['extra_plot']) if kind=='extra_plot' else cfg['cost']
            if p[kind]>=cfg['max']: msg='You have reached the maximum level for this upgrade.'
            elif p['coins']<price: msg=f'Not enough coins. This upgrade costs {price:,} coins.'
            else:
                con.execute(f'UPDATE players SET {kind}={kind}+1,coins=coins-? WHERE user_id=?',(price,uid))
                con.execute('UPDATE players SET upgrades_bought=upgrades_bought+1 WHERE user_id=?',(uid,))
                log_action(uid,e,'upgrade',kind,-price)
                msg=f"Purchased {cfg['name']} for {price} coins!" + (f' Plots Owned: {available_plots(player(uid))}/{PLOTS + UPGRADES["extra_plot"]["max"]}.' if kind=='extra_plot' else ' Your stored produce sells for 6% more per level.')
            con.commit()
    await show_farm(interaction, latest_action='🏡 **Upgrade:** ' + msg, edit_existing=True)

async def do_harvest(interaction,uid):
    async with lock:
        e=event()
        if not active(e) or not player(uid): msg='The event is not active or you have not joined.'
        else:
            sync_actions(uid,e)
            p=player(uid)
            w=current_window(e)
            ready=[r for r in con.execute('SELECT * FROM plots WHERE user_id=?',(uid,)) if crop_ready_ts(r,e)<=int(time.time())]
            if not ready:
                pending=list(con.execute('SELECT * FROM plots WHERE user_id=?',(uid,)))
                if pending:
                    soon=min(crop_ready_ts(r,e) for r in pending)
                    msg=f'⏳ No crops are ready yet. Your next harvest is ready <t:{soon}:R>. When ready, click **Harvest Ready**.'
                else: msg='No crops planted yet. Plant a crop first.'
            else:
                # Harvesting all ready plots is free; only planting spends session actions.
                for r in ready:
                    con.execute('INSERT INTO inventory(user_id,crop,qty) VALUES(?,?,1) ON CONFLICT(user_id,crop) DO UPDATE SET qty=qty+1',(uid,r['crop']))
                    con.execute('DELETE FROM plots WHERE user_id=? AND slot=?',(uid,r['slot']))
                con.execute('UPDATE players SET harvested=harvested+? WHERE user_id=?',(len(ready),uid))
                log_action(uid,e,'harvest',f'{len(ready)} crops')
                msg=f'🧺 Harvested {len(ready)} mature crop(s) into your inventory for free. Sell them whenever you like.'
                for threshold in (5,15,30,50):
                    if p['harvested'] < threshold <= p['harvested']+len(ready):
                        msg+=f' 🏅 Milestone reached: {threshold} crops harvested!'
            con.commit()
    await show_farm(interaction, latest_action='🧺 **Harvest:** ' + msg, edit_existing=True)

async def do_sell(interaction,uid):
    async with lock:
        e=event()
        if not active(e) or not player(uid): msg='The event is not active or you have not joined.'
        else:
            sync_actions(uid,e)
            p=player(uid)
            items=list(con.execute('SELECT * FROM inventory WHERE user_id=? AND qty>0',(uid,)))
            if not items: msg='🧺 Nothing to sell yet. Wait until crops are ready, click **Harvest Ready**, then click **Sell Produce**.'
            else:
                earned=inventory_value(uid,p)
                con.execute('DELETE FROM inventory WHERE user_id=?',(uid,))
                con.execute('UPDATE players SET coins=coins+?,sold=sold+? WHERE user_id=?',(earned,earned,uid))
                log_action(uid,e,'sell',f'{sum(x["qty"] for x in items)} crops',earned)
                msg=f'🪙 Sold {sum(x["qty"] for x in items)} crop(s) for {earned} coins (free).'
            con.commit()
    await show_farm(interaction, latest_action='🪙 **Selling:** ' + msg, edit_existing=True)

async def show_farm(interaction, latest_action=None, edit_existing=False):
    uid=interaction.user.id
    async with lock:
        e=event()
        if not e:
            msg='No Harvest Hustle event has been scheduled. A host must use `/harvest start`.'
            await interaction.response.send_message(msg,ephemeral=True)
            return
        if not player(uid):
            await interaction.response.send_message('Use the Join Harvest Hustle button in the host announcement first.',ephemeral=True)
            return
        if active(e):
            sync_actions(uid,e)
            con.commit()
        p=player(uid)
        w=current_window(e)
        if not started(e):
            await interaction.response.send_message(f'🍁 You are registered! Farming begins {stamp(e["start_ts"])}. Use `/harvest rules` while you wait.',ephemeral=True)
            return
        plots={r['slot']:r for r in con.execute('SELECT * FROM plots WHERE user_id=?',(uid,))}
        lines=[]
        for slot in range(1,available_plots(p)+1):
            r=plots.get(slot)
            if not r: lines.append(f'**{slot}.** 🌱 Empty')
            else:
                c=CROPS[r['crop']]
                ready_ts=crop_ready_ts(r,e)
                remaining=max(0,ready_ts-int(time.time()))
                lines.append(f'**{slot}.** {c["name"]} — '+('✅ **Ready! Click Harvest Ready below**' if remaining==0 else f'⏳ Ready <t:{ready_ts}:R> · <t:{ready_ts}:t>'))
        inv=', '.join(f"{CROPS[r['crop']]['name']} ×{r['qty']}" for r in con.execute('SELECT * FROM inventory WHERE user_id=? AND qty>0',(uid,))) or 'Empty'
        embed=make_embed('🍁 Your Autumn Farm',f'**Day {w//2+1} of 7**  •  Session {w%2+1} of 2\n🏁 **Event ends:** {stamp(e["start_ts"]+DURATION)}', AUTUMN['green'])
        embed.add_field(name='💰 Coins',value=str(p['coins']))
        embed.add_field(name='🌱 Planting actions left',value=f'{max(0,ACTIONS-p["actions_used"])}/{ACTIONS}')
        embed.add_field(name='🏆 Farm value',value=f'{farm_value(uid,p,e):,}')
        next_reset=e['start_ts']+(w+1)*WINDOW
        if active(e): embed.add_field(name='⏳ Next planting reset',value=stamp(next_reset),inline=False)
        milestone=max((n for n in (5,15,30,50) if p['harvested']>=n),default=0)
        embed.add_field(name='🏅 Harvest milestone',value=f'{milestone} crops' if milestone else 'Not yet unlocked',inline=True)
        embed.add_field(name='🌱 Plots Owned',value=f'{available_plots(p)}/{PLOTS + UPGRADES["extra_plot"]["max"]} · {len(plots)} occupied · {available_plots(p)-len(plots)} empty',inline=False)
        embed.add_field(name='🌾 Your Growing Plots',value='\n'.join(lines)[:1024],inline=False)
        embed.add_field(name='🧺 Inventory',value=inv,inline=False)
        embed.add_field(name='📊 Your Season So Far',value=f'🌱 Planted **{p["planted"]}**  •  🧺 Harvested **{p["harvested"]}**\n🪙 Sales Revenue **{p["sold"]:,}c**',inline=False)
        embed.add_field(name='⬆️ Upgrades',value=f'Extra plots: {p["extra_plot"]}/10 · Market stalls: {p["market"]}/2',inline=False)
        if latest_action:
            embed.add_field(name='🍂 LATEST ACTION', value=latest_action[:1024], inline=False)
        embed.set_footer(text='When a crop is ready, click Harvest Ready, then Sell Produce. Only planting uses actions.')
    if edit_existing:
        await interaction.response.edit_message(embed=embed, view=FarmView(uid) if active(e) else None)
    else:
        await interaction.response.send_message(embed=embed,view=FarmView(uid) if active(e) else None,ephemeral=True)

# Role panel buttons persist across restarts. The last post ID is saved only to avoid repeat @everyone pings.
official_con.execute('CREATE TABLE IF NOT EXISTS role_panel(id INTEGER PRIMARY KEY CHECK(id=1), message_id INTEGER NOT NULL)')
official_con.commit()

# Store every panel so role sign-ups can refresh all visible counters.
official_con.execute('CREATE TABLE IF NOT EXISTS role_panel_messages(message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)')
official_con.commit()


def role_signup_count(guild: discord.Guild) -> int:
    role = guild.get_role(ROLE_ID)
    return len(role.members) if role else 0


def role_panel_embed(signups: int, with_banner: bool = True):
    embed = discord.Embed(
        title='🍂 HARVEST HUSTLE | AUTUMN SHOWDOWN',
        description=(
            '🎮 **TRICK OR TREAT WAS JUST THE BEGINNING!**\n'
            'Our next big Secret Service Discord game is here. '
            'Grow your farm, earn coins and compete for the top spot!'
        ),
        color=discord.Color.from_rgb(170, 112, 45),
    )
    embed.add_field(
        name='🏆 375 CREDITS + 3 HC BOXES TO WIN',
        value=(
            '🥇 **1st — 200 credits + HC Box**\n'
            '🥈 **2nd — 100 credits + HC Box**\n'
            '🥉 **3rd — 75 credits + HC Box**'
        ), inline=False,
    )
    embed.add_field(
        name='📅 EVENT DATES',
        value='🟢 **Starts:** 8 November, 12:00 AM GMT\n🔴 **Ends:** 15 November, 12:00 AM GMT',
        inline=False,
    )
    embed.add_field(
        name=f'👥 {signups} MEMBERS SIGNED UP',
        value='Click **Join / Leave Harvest Hustle** below to claim your event role!',
        inline=False,
    )
    embed.add_field(
        name='🚜 HOW IT WORKS',
        value=(
            '🌱 Plant and harvest crops\n'
            '💰 Sell produce and upgrade your farm\n'
            '🏆 Climb the leaderboard over seven days\n\n'
            'Claiming this role **does not start your farm**. Watch for the official game launch!'
        ), inline=False,
    )
    if with_banner:
        embed.set_image(url='attachment://harvest_hustle_role_banner.png')
    embed.set_footer(text='🌻 7 DAYS • 3 WINNERS • 1 HARVEST CHAMPION')
    return embed


async def refresh_role_panel_counts(guild: discord.Guild):
    count = role_signup_count(guild)
    records = official_con.execute('SELECT message_id, channel_id FROM role_panel_messages').fetchall()
    for record in records:
        channel = guild.get_channel(record['channel_id'])
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            message = await channel.fetch_message(record['message_id'])
            has_banner = bool(message.embeds and message.embeds[0].image and message.embeds[0].image.url)
            await message.edit(embed=role_panel_embed(count, with_banner=has_banner))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue


class HarvestRolePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Join / Leave Harvest Hustle', emoji='🍂', style=discord.ButtonStyle.success,
                       custom_id='harvest_hustle:toggle_event_role:v1')
    async def toggle_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message('Use this panel inside the server.', ephemeral=True)
            return
        role = interaction.guild.get_role(ROLE_ID)
        if role is None:
            await interaction.response.send_message('The event role could not be found. Please contact a host.', ephemeral=True)
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
            except discord.HTTPException:
                await interaction.response.send_message('Could not check your server membership. Try again.', ephemeral=True)
                return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason='Harvest Hustle self-service role panel')
                reply = '🍂 You have left the Harvest Hustle role. You can rejoin anytime.'
            else:
                await member.add_roles(role, reason='Harvest Hustle self-service role panel')
                reply = '🌾 You now have the Harvest Hustle role! Watch for the official game launch.'
            await interaction.response.send_message(reply, ephemeral=True)
            await refresh_role_panel_counts(interaction.guild)
        except discord.Forbidden:
            await interaction.response.send_message('I need **Manage Roles** and my highest role must be **above** the event role.', ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message('Discord could not update your role. Please try again.', ephemeral=True)

intents=discord.Intents.default()
intents.members = True  # Enable Server Members Intent in Discord Developer Portal too.
bot=commands.Bot(command_prefix='!',intents=intents)
harvest=app_commands.Group(name='harvest',description='Harvest Hustle farming competition')

@harvest.command(name='role-panel', description='Host only: post a new Harvest Hustle role panel')
async def role_panel(interaction: discord.Interaction):
    if not is_host(interaction):
        await interaction.response.send_message('Host only.', ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message('Use this command inside the server.', ephemeral=True)
        return
    role = interaction.guild.get_role(ROLE_ID)
    if role is None:
        await interaction.response.send_message(f'Role {ROLE_ID} was not found in this server.', ephemeral=True)
        return
    channel = interaction.guild.get_channel(ROLE_PANEL_CHANNEL_ID) or bot.get_channel(ROLE_PANEL_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message('The configured ROLE_PANEL_CHANNEL_ID is unavailable or is not a text channel.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        # Always post a fresh, visible panel. Never edit a previously posted message.
        # Only the first panel pings @everyone; reposts stay quiet.
        previous = official_con.execute('SELECT message_id FROM role_panel WHERE id=1').fetchone()
        has_banner = PANEL_BANNER.is_file()
        mention_everyone = previous is None
        content = ('@everyone ' if mention_everyone else '') + (
            '🎮 **TRICK OR TREAT WAS JUST THE BEGINNING!** 🍂 '
            '**HARVEST HUSTLE** is coming! 🏆 **375 HABBO CREDITS + 3 HC BOXES TO BE WON!** '
            '🚜 Claim your event role below and get ready for **8 NOVEMBER, 12 AM GMT!**')
        kwargs = dict(content=content, embed=role_panel_embed(role_signup_count(interaction.guild), with_banner=has_banner),
                      view=HarvestRolePanel(), allowed_mentions=discord.AllowedMentions(everyone=mention_everyone, roles=False, users=False))
        if has_banner:
            kwargs['file'] = discord.File(PANEL_BANNER, filename='harvest_hustle_role_banner.png')
        message = await channel.send(**kwargs)
        official_con.execute('INSERT INTO role_panel(id,message_id) VALUES(1,?) '
                    'ON CONFLICT(id) DO UPDATE SET message_id=excluded.message_id', (message.id,))
        official_con.commit()
        official_con.execute('INSERT OR REPLACE INTO role_panel_messages(message_id, channel_id) VALUES(?, ?)', (message.id, channel.id))
        official_con.commit()
        await interaction.followup.send(
            f'🍂 A NEW role panel was posted in {channel.mention}. ' + ('@everyone was included.' if mention_everyone else 'No repeat @everyone ping was sent.'), ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            'I need View Channel, Send Messages, Embed Links, Attach Files and permission '
            'to mention @everyone in the role-panel channel.', ephemeral=True)
    except discord.HTTPException:
        await interaction.followup.send('Discord could not post the role panel. Please retry.', ephemeral=True)

@harvest.command(name='role-panel-test', description='Host only: post a Harvest Hustle role panel in this channel')
async def role_panel_test(interaction: discord.Interaction):
    """Post a working role panel in the channel where this command is run."""
    if not is_host(interaction):
        await interaction.response.send_message('Host only.', ephemeral=True)
        return
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message('Use this command in a server text channel.', ephemeral=True)
        return
    if interaction.guild.get_role(ROLE_ID) is None:
        await interaction.response.send_message(f'Role {ROLE_ID} was not found in this server.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        # This command ignores ROLE_PANEL_CHANNEL_ID and EVENT_CHANNEL_ID.
        # No @everyone ping, no game launch, and no modification of the official panel.
        has_banner = PANEL_BANNER.is_file()
        kwargs = dict(
            content='🍂 **HARVEST HUSTLE — ROLE PANEL PREVIEW** 🚜',
            embed=role_panel_embed(role_signup_count(interaction.guild), with_banner=has_banner),
            view=HarvestRolePanel(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if has_banner:
            kwargs['file'] = discord.File(PANEL_BANNER, filename='harvest_hustle_role_banner.png')
        message = await interaction.channel.send(**kwargs)
        # Register this preview so its sign-up counter also updates live.
        official_con.execute(
            'INSERT OR REPLACE INTO role_panel_messages(message_id, channel_id) VALUES(?, ?)',
            (message.id, interaction.channel.id),
        )
        official_con.commit()
        await interaction.followup.send(
            f'🍂 Test role panel posted in {interaction.channel.mention}. No @everyone ping sent.',
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            'I need View Channel, Send Messages, Embed Links and Attach Files in this channel.',
            ephemeral=True,
        )
    except discord.HTTPException:
        await interaction.followup.send('Discord could not post the test role panel. Please retry.', ephemeral=True)

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    before_has = any(r.id == ROLE_ID for r in before.roles)
    after_has = any(r.id == ROLE_ID for r in after.roles)
    if before_has != after_has:
        await refresh_role_panel_counts(after.guild)


class HarvestRegistrationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Join Harvest Hustle', emoji='🌱', style=discord.ButtonStyle.success,
                       custom_id='harvest_hustle:register:v1')
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with lock:
            e = event()
            if not registration_open(e) or not interaction.message or interaction.message.id != e['registration_post_id']:
                message = 'Joining is not open yet, or the event has ended. Please use the official registration post.'
            elif player(interaction.user.id):
                message = ('🌾 You have already joined! Open `/harvest farm` to play now.' if active(e) else '🌾 You have already joined! Farming opens 8 November at 00:00 GMT.')
            else:
                con.execute('INSERT INTO players(user_id,joined_ts,action_window) VALUES(?,?,?)',
                            (interaction.user.id, int(time.time()), current_window(e)))
                con.commit()
                message = ('🎉 **You’re in!** Welcome to Harvest Hustle! 🌾\n'
                           + ('**The farm is OPEN NOW!** Use `/harvest farm` to play. ' if active(e) else 'Farming begins **8 November at 00:00 GMT**. ')
                           + 'You start with **2,000 coins and 10 plots**. Use `/harvest help` for the full guide.')
        await interaction.response.send_message(message, ephemeral=True)


def registration_embed(live_test=False):
    return discord.Embed(
        title=('🧪 Harvest Hustle — LIVE TEST' if live_test else '🌾 Harvest Hustle — Join the Competition!'),
        description=(
            '🚜 **It’s time to get farming!**\n\n'
            'Ready to grow your farm and compete for some fantastic prizes?\n\n'
            'Click **Join Harvest Hustle** below to enter the competition!\n\n'
            + ('🧪 **LIVE TEST IS ACTIVE — farming is available NOW!**\n' if live_test else '🌱 **Farming begins:** 8 November, 00:00 GMT\n')
            + '🏆 **Prizes:** 1st 200c + HC Box · 2nd 100c + HC Box · 3rd 75c + HC Box\n\n'
            '📖 Want to know how it works? Use `/harvest help`.'
        ),
        color=discord.Color.from_rgb(160, 104, 49)
    )


@harvest.command(name='start-test', description='Host only: start a fresh live test in this channel')
async def start_test(interaction: discord.Interaction):
    global con
    if not is_host(interaction):
        await interaction.response.send_message('Host only.', ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message('Run this in your test text channel.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with lock:
        if test_mode_active():
            await interaction.followup.send('A test is already running. Use `/harvest end-test` first.', ephemeral=True)
            return
        if int(time.time()) >= FARMING_AT:
            await interaction.followup.send('Pre-launch testing has closed. The official event schedule is now in effect.', ephemeral=True)
            return
        official_event = official_con.execute('SELECT * FROM event WHERE id=1').fetchone()
        official_players = official_con.execute('SELECT COUNT(*) FROM players').fetchone()[0]
        if official_players or (official_event and official_event['registration_post_id']):
            await interaction.followup.send('Official registrations exist; refusing to start a test over official data.', ephemeral=True)
            return
        clear_test_tables()
        try:
            message = await interaction.channel.send(
                content='🧪 **Harvest Hustle LIVE TEST — open now!** (No @everyone ping)',
                embed=registration_embed(live_test=True),
                view=HarvestRegistrationView(),
                allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=False))
        except discord.HTTPException:
            logging.exception('Could not post test announcement')
            await interaction.followup.send('Could not post the test Join announcement. Check channel permissions.', ephemeral=True)
            return
        now = int(time.time())
        test_con.execute('INSERT INTO event(id,start_ts,ended,live_test,registration_post_id,registration_channel_id,midweek_sent) VALUES(1,?,0,1,?,?,1)',
                         (now, message.id, interaction.channel.id))
        test_con.commit()
        official_con.execute("UPDATE harvest_runtime SET mode='test' WHERE id=1")
        official_con.commit()
        con = test_con
    await interaction.followup.send(f'🧪 Test started in {interaction.channel.mention}. Click **Join Harvest Hustle** and use `/harvest farm`. Finish with `/harvest end-test`.', ephemeral=True)


@harvest.command(name='end-test', description='Host only: end the test and clear test progress')
@app_commands.describe(confirm='Confirm clearing all test-only farms, crops and coins')
async def end_test(interaction: discord.Interaction, confirm: bool = False):
    global con
    if not is_host(interaction):
        await interaction.response.send_message('Host only.', ephemeral=True)
        return
    if not confirm:
        await interaction.response.send_message('This clears **test-only** farms, crops, coins and scores. Official records are untouched. Run `/harvest end-test` with **confirm = True**.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with lock:
        if not test_mode_active():
            await interaction.followup.send('No test is active. Official data was not touched.', ephemeral=True)
            return
        e = test_con.execute('SELECT * FROM event WHERE id=1').fetchone()
        post_id = e['registration_post_id'] if e else None
        channel_id = e['registration_channel_id'] if e else None
        clear_test_tables()
        official_con.execute("UPDATE harvest_runtime SET mode='official' WHERE id=1")
        official_con.commit()
        con = official_con
    if post_id and channel_id:
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            await (await channel.fetch_message(post_id)).delete()
        except discord.HTTPException:
            logging.warning('Test announcement could not be deleted, but its Join button is invalid.')
    await interaction.followup.send('✅ Test finished and test progress cleared. Official data is untouched. On launch day, run `/harvest start`.', ephemeral=True)


@harvest.command(name='start', description='Host only: publish the official Join announcement in the configured channel')
async def start(interaction: discord.Interaction):
    if not is_host(interaction):
        await interaction.response.send_message('Host only.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with lock:
        if test_mode_active():
            await interaction.followup.send('End your test with `/harvest end-test` before launching.', ephemeral=True)
            return
        if int(time.time()) < FARMING_AT:
            await interaction.followup.send('Official launch opens 8 November 2026 at 00:00 GMT. Use `/harvest start-test` to test before then.', ephemeral=True)
            return
        e = event()
        if e and (e['ended'] or e['registration_post_id']):
            await interaction.followup.send('The official event is finished or its Join announcement already exists. No duplicate ping sent.', ephemeral=True)
            return
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(CHANNEL_ID)
            except discord.HTTPException:
                await interaction.followup.send('Could not access EVENT_CHANNEL_ID.', ephemeral=True)
                return
        try:
            message = await channel.send(
                content='@everyone 🌾 **Harvest Hustle is OPEN!**',
                embed=registration_embed(live_test=False),
                view=HarvestRegistrationView(),
                allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False))
        except discord.HTTPException:
            logging.exception('Could not post official announcement')
            await interaction.followup.send('Unable to post the official Join announcement.', ephemeral=True)
            return
        if e is None:
            con.execute('INSERT INTO event(id,start_ts,ended) VALUES(1,?,0)', (FARMING_AT,))
        con.execute('UPDATE event SET registration_post_id=?,registration_channel_id=? WHERE id=1', (message.id, channel.id))
        con.commit()
    await interaction.followup.send(f'🌾 Official Join announcement posted in {channel.mention}. The event ends 15 November 2026 at 00:00 GMT.', ephemeral=True)


@harvest.command(name='farm',description='Open your private farming menu')
async def farm(interaction:discord.Interaction):
    await show_farm(interaction)

@harvest.command(name='leaderboard',description='View the top farms')
async def leaderboard(interaction:discord.Interaction):
    async with lock:
        e=event()
        if not e:
            await interaction.response.send_message('No event exists yet.',ephemeral=True);return
        ranks=board(e)
        desc='\n'.join(f'**{i}.** <@{r[3]}> — **{r[0]:,}** farm value' for i,r in enumerate(ranks[:15],1)) or 'No players yet.'
        mine=next((i for i,r in enumerate(ranks,1) if r[3]==interaction.user.id),None)
        if mine and mine>15: desc+=f'\n\n**Your rank:** #{mine} — {ranks[mine-1][0]:,} farm value'
        embed=make_embed('🏆 Autumn Harvest Leaderboard',desc, AUTUMN['gold'])
        embed.set_footer(text='Ties: farm value → total sales → harvest count → earlier registration → Discord ID.')
    await interaction.response.send_message(embed=embed)

@harvest.command(name='rules',description='Learn the game rules')
async def rules(interaction:discord.Interaction):
    crops='\n'.join(f"{v['name']}: seed {v['cost']}c → sells {v['sale']}c · grows {v['hours']} hours" for v in CROPS.values())
    e=make_embed('📖 Harvest Hustle Rules',
        '**Join using the host-posted Join button; late joining is allowed while the event is active.** Seven days, fourteen 12-hour sessions. You get **10 planting actions per session**; unused actions do not roll over. Each player starts with **2,000 coins and 10 plots**.\n\n'
        '**Plant**: one planting action and the seed cost per plot. **Harvest**: free, collects ALL ready crops. **Sell**: free, sells ALL stored produce. **Upgrade**: free action (coins required).\n\n'
        '**Crops**\n'+crops+'\n\n'
        '**Upgrades**\n🌱 Extra Plot: 500c for plots 11–14, 900c for 15–17, 1,500c for 18–20; resale is 50% of each plot’s purchase price.\n🛒 Market Stall: 1,200c, up to 2, +6% sale price per level, resale value 600c each.\n\n'
        '**Winner**: coins + stored produce sale value + upgrade resale value. **Growing crops are worth 0 at closing.** Tie order: lifetime sales, total crops harvested, earlier join, then Discord user ID.\n\n'
        'For a full walkthrough, use `/harvest help`. Harvest milestones at 5, 15, 30 and 50 crops are recognition-only. Daily missions: plant 2 crops (+50c), harvest 2 crops (+75c), sell once (+100c), plus a +75c completion bonus; claim with `/harvest objectives` each event day. No random crop failures, market price swings or speed bonuses. Players join using the host-posted button; there is no separate registration countdown. All progress is stored in SQLite.')
    await interaction.response.send_message(embed=e,ephemeral=True)

@harvest.command(name='help', description='Complete guide to Harvest Hustle, commands, missions and prizes')
async def harvest_help(interaction: discord.Interaction):
    intro = make_embed('🌾 Harvest Hustle | How to Play',
        '**Goal:** Build the highest-value farm over **7 days**, starting **8 November 2026 at 00:00 GMT**.\n\n'
        '**Joining the event**\n'
        'First, react to the original role panel to access the event channels. Then, when the host posts the separate **Join Harvest Hustle** announcement in the main game channel, click its **Join** button. **No join slash command and no 24-hour registration wait.**\n\n'
        '**Getting started**\n'
        '1. Click the **Join Harvest Hustle** button in the host announcement.\n'
        '2. Once farming begins, use `/harvest farm` to open your private controls.\n'
        '3. Select **Plant**, choose a crop and an empty plot. Seeds cost coins; each planting uses **1 action**.\n'
        '4. Watch the live crop timer. Once ready, use **Harvest Ready** to collect all mature crops.\n'
        '5. Click **Sell Produce** to sell everything in your inventory for coins. Harvesting and selling are **free actions**.\n\n'
        '**Starting resources:** 2,000 in-game coins, 10 plots and **10 planting actions per 12-hour session**. Unused actions do not carry over. Crops grow in real time, independent of action resets.\n\n'
        '**Private updates:** Farm actions appear privately in the game channel. Discord controls how long ephemeral messages stay visible. Use **Save Farm to DMs** for a lasting copy.\n\n'
        '**Crop readiness:** The bot displays relative ready times and attempts to DM you when crops mature. If DMs are disabled, check `/harvest farm` yourself. Ready crops remain available until harvested.')
    crops = '\n'.join(f"{v['name']} — **{v['hours']}h** · Seed **{v['cost']}c** · Sell **{v['sale']}c**" for v in CROPS.values())
    economy = make_embed('🌱 Crops, Upgrades & Daily Missions',
        '**Crop guide**\n' + crops + '\n\n'
        '**Expand your farm**\n'
        '🌱 Extra plots: **500c each** for plots 11–14, **900c each** for plots 15–17 and **1,500c each** for plots 18–20. Maximum **20 plots**. Each purchased plot adds one planting space, not an extra planting action. Resale value: **50% of purchase price**.\n'
        '🛒 Market Stall: **1,200c** per level, up to **2 levels**. Each level increases produce sale prices by **6%** (maximum +12%). Resale value: **600c** per level.\n\n'
        '**Three daily missions** (claim via `/harvest objectives`)\n'
        '• Plant 2 crops: **+50c**\n'
        '• Harvest 2 crops: **+75c**\n'
        '• Sell produce at least once: **+100c**\n'
        '• Complete all three: **+75c daily bonus**\n'
        'Missions reset each event day. Open objectives to claim earned rewards **before the next day**. Maximum **300c per day** (2,100c over seven days). Harvest milestones (5/15/30/50) are recognition-only.')
    finish = make_embed('🏆 Winning, Prizes & Commands',
        '**Final farm value** = remaining coins + harvested produce still in inventory at its current sale value + upgrade resale value. **Unharvested crops are worth 0 at event closing.**\n'
        'Ties: lifetime produce sales → crops harvested → earlier registration → Discord user ID.\n\n'
        '**Prizes**\n🥇 **200 Habbo credits + HC Box**\n🥈 **100 Habbo credits + HC Box**\n🥉 **75 Habbo credits + HC Box**\n\n'
        '**Player commands**\n'
        '`/harvest help` — This guide\n'
        '`/harvest rules` — Rules summary\n'
        'Registration — Click **Join Harvest Hustle** in the host announcement\n'
        '`/harvest farm` — Open your farm; use **Plant**, **Harvest Ready**, **Sell Produce**, **Upgrades** and **Refresh Farm** buttons\n'
        '`/harvest objectives` — View and claim daily missions (claim before the next event day)\n'
        '`/harvest leaderboard` — Rankings\n'
        '`/harvest stats` — Your farm statistics\n'
        '`/harvest status` — Event schedule\n\n'
        '**Fair play:** No random crop failures, market price swings or speed bonuses. All coin amounts above are **in-game coins** unless explicitly described as Habbo credits.')
    await interaction.response.send_message(embeds=[intro, economy, finish], ephemeral=True)

@harvest.command(name='status',description='Check event timing and player count')
async def status(interaction:discord.Interaction):
    async with lock:
        e=event()
        if not e: msg='No event has started.'
        else:
            count=con.execute('SELECT COUNT(*) FROM players').fetchone()[0]
            msg=f'**Status:** {"Finished" if e["ended"] else "Live" if active(e) else "Registration" if registration_open(e) else "Closing"}\n**Players:** {count}\n**Start:** {stamp(e["start_ts"])}\n**End:** {stamp(e["start_ts"]+DURATION)}'
    await interaction.response.send_message(msg,ephemeral=True)

@harvest.command(name='reset',description='Host only: delete a completed event to prepare a new one')
@app_commands.describe(confirm='Type RESET to permanently erase the completed event')
async def reset(interaction:discord.Interaction,confirm:str):
    if not is_host(interaction):
        await interaction.response.send_message('Host only.',ephemeral=True);return
    if confirm!='RESET':
        await interaction.response.send_message('To confirm, type exactly RESET.',ephemeral=True);return
    async with lock:
        e=event()
        if e and not e['ended']: msg='Cannot reset a scheduled or active event.'
        else:
            for table in ('harvest_alerts','transactions','objective_claims','inventory','plots','players','event'):
                con.execute(f'DELETE FROM {table}')
            con.commit()
            msg='Completed event data cleared. You can start a new event.'
    await interaction.response.send_message(msg,ephemeral=True)

async def send_announcement(message,ping=False):
    if not CHANNEL_ID: return
    channel=bot.get_channel(CHANNEL_ID)
    if channel is None:
        try: channel=await bot.fetch_channel(CHANNEL_ID)
        except (discord.HTTPException,discord.Forbidden): return
    content=(f'<@&{ROLE_ID}>\n' if ping and ROLE_ID else '')+message
    try:
        await channel.send(content,allowed_mentions=discord.AllowedMentions(roles=True,users=False,everyone=False))
    except discord.HTTPException:
        logging.exception('Unable to post event announcement')

# Three achievable missions per event day, claimable via /harvest objectives or
# the Daily Missions farm button. Rewards are modest, deterministic and once/day.
DAILY_MISSIONS = (
    ('first_seed', 'Plant at least 2 crops', 'plant', 2, 50),
    ('harvest', 'Harvest at least 2 crops', 'harvest', 2, 75),
    ('trade', 'Sell produce at least once', 'sell', 1, 100),
)

async def show_objectives(interaction):
    uid=interaction.user.id
    async with lock:
        e=event()
        if not active(e) or not player(uid):
            await interaction.response.send_message('Daily missions are available while you are farming.',ephemeral=True)
            return
        day=current_window(e)//2
        start=e['start_ts']+day*24*3600
        end=start+24*3600
        logs=list(con.execute('SELECT action,detail FROM transactions WHERE user_id=? AND ts>=? AND ts<?', (uid,start,end)))
        planted=sum(1 for r in logs if r['action']=='plant')
        harvested=sum(int(r['detail'].split()[0]) for r in logs if r['action']=='harvest' and r['detail'].split()[0].isdigit())
        sales=sum(1 for r in logs if r['action']=='sell')
        counts={'plant':planted,'harvest':harvested,'sell':sales}
        lines=[];earned=0
        for key,label,action,target,reward in DAILY_MISSIONS:
            previously=con.execute('SELECT 1 FROM objective_claims WHERE user_id=? AND day=? AND objective=?',(uid,day,key)).fetchone()
            if counts[action]>=target and not previously:
                con.execute('INSERT OR IGNORE INTO objective_claims VALUES(?,?,?)',(uid,day,key))
                con.execute('UPDATE players SET coins=coins+? WHERE user_id=?',(reward,uid))
                log_action(uid,e,'mission',key,reward)
                earned+=reward
            done=counts[action]>=target
            lines.append(f'{"✅" if done else "⬜"} **{label}** — {min(counts[action],target)}/{target} · **+{reward}c**' + (' (claimed)' if done else ''))
        # The completion bonus is claimed once per event day, only after all 3
        # mission targets have been achieved. A unique DB key prevents duplicates.
        all_done=all(counts[action]>=target for _,_,action,target,_ in DAILY_MISSIONS)
        bonus_claimed=con.execute('SELECT 1 FROM objective_claims WHERE user_id=? AND day=? AND objective=?',
                                  (uid,day,'daily_bonus')).fetchone()
        if all_done and not bonus_claimed:
            cur=con.execute('INSERT OR IGNORE INTO objective_claims VALUES(?,?,?)',
                            (uid,day,'daily_bonus'))
            if cur.rowcount:
                con.execute('UPDATE players SET coins=coins+75 WHERE user_id=?',(uid,))
                log_action(uid,e,'mission','daily_bonus',75)
                earned+=75
        lines.append(('✅' if all_done else '⬜') + ' **Complete all three missions** — ' +
                     ('3/3' if all_done else f'{sum(counts[action]>=target for _,_,action,target,_ in DAILY_MISSIONS)}/3') +
                     ' · **+75c bonus**' + (' (claimed)' if all_done else ''))
        con.execute('UPDATE players SET objectives_claimed=(SELECT COUNT(*) FROM objective_claims WHERE user_id=?) WHERE user_id=?',(uid,uid))
        total=con.execute("SELECT COUNT(*) FROM objective_claims WHERE user_id=? AND objective!='daily_bonus'",(uid,)).fetchone()[0]
        con.commit()
    message='\n'.join(lines)+f'\n\n🏅 **Missions completed:** {total} · **Rewards claimed now:** +{earned} coins\nMaximum **300c per day** including the completion bonus. Claim before the next event day. Rewards are once per mission per day.'
    await interaction.response.send_message(embed=make_embed(f'🎯 Day {day+1} Daily Missions',message),ephemeral=True)

@harvest.command(name='objectives',description='View and claim achievable daily farming missions')
async def objectives(interaction:discord.Interaction):
    await show_objectives(interaction)

@harvest.command(name='stats',description='View your farming performance')
async def stats(interaction:discord.Interaction):
    async with lock:
        e=event(); p=player(interaction.user.id)
        if not e or not p:
            await interaction.response.send_message('Join the event to see your statistics.',ephemeral=True);return
        rows=board(e)
        rank=next((i for i,r in enumerate(rows,1) if r[3]==interaction.user.id),0)
        detail=(f'**Rank:** #{rank} of {len(rows)}\n**Farm value:** {farm_value(p["user_id"],p,e):,}\n'
                f'**Seeds planted:** {p["planted"]}\n**Crops harvested:** {p["harvested"]}\n'
                f'**Lifetime produce sales:** {p["sold"]:,} coins\n**Actions spent:** {p["actions_total"]}\n'
                f'**Upgrades purchased:** {p["upgrades_bought"]}\n**Daily missions completed:** {p["objectives_claimed"]}')
    await interaction.response.send_message(embed=make_embed('📊 Your Farm Statistics',detail),ephemeral=True)

@harvest.command(name='adjust-coins', description='Host only: add or remove in-game coins (e.g. crossword rewards)')
@app_commands.describe(
    member='Registered Harvest Hustle player to adjust',
    amount='Coins to add (positive) or remove (negative)',
    reason='Reason for the adjustment, e.g. Crossword: 8 correct words'
)
async def adjust_coins(interaction: discord.Interaction, member: discord.Member, amount: int, reason: str):
    if not is_host(interaction):
        await interaction.response.send_message('Host only.', ephemeral=True)
        return
    if amount == 0 or abs(amount) > 100000:
        await interaction.response.send_message('Enter a nonzero amount between -100,000 and 100,000.', ephemeral=True)
        return
    reason = reason.strip()
    if not reason or len(reason) > 150:
        await interaction.response.send_message('Provide a reason of 1–150 characters.', ephemeral=True)
        return
    async with lock:
        e = event()
        if e is None or not active(e):
            await interaction.response.send_message('Coin adjustments are available only while farming is active.', ephemeral=True)
            return
        p = player(member.id)
        if p is None:
            await interaction.response.send_message('That member is not registered for Harvest Hustle.', ephemeral=True)
            return
        new_balance = p['coins'] + amount
        if new_balance < 0:
            await interaction.response.send_message(
                f'Cannot remove {abs(amount):,} coins; this player only has {p["coins"]:,}.', ephemeral=True
            )
            return
        con.execute('UPDATE players SET coins=? WHERE user_id=?', (new_balance, member.id))
        log_action(member.id, e, 'host_coin_adjustment',
                   f'Host {interaction.user.id}: {reason}', amount)
        con.commit()
    await interaction.response.send_message(
        f'✅ Updated {member.mention}\'s Harvest Hustle coins by **{amount:+,}**.\n'
        f'**New balance:** {new_balance:,} coins\n**Reason:** {reason}\n'
        'This adjustment is recorded in `/harvest history`.', ephemeral=True
    )

@harvest.command(name='dashboard',description='Host only: activity, standings and recent transactions')
async def dashboard(interaction:discord.Interaction):
    if not is_host(interaction):
        await interaction.response.send_message('Host only.',ephemeral=True);return
    async with lock:
        e=event()
        if not e:
            await interaction.response.send_message('No event exists.',ephemeral=True);return
        total=con.execute('SELECT COUNT(*) FROM players').fetchone()[0]
        last=con.execute('SELECT COUNT(DISTINCT user_id) FROM transactions WHERE ts>=?',(int(time.time())-24*3600,)).fetchone()[0]
        actions=con.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
        leaders=board(e)[:5]
        recent=list(con.execute('SELECT user_id,action,detail,coin_change,ts FROM transactions ORDER BY id DESC LIMIT 8'))
        desc=(f'**Registrations:** {total}\n**Active last 24h:** {last}\n**Total transactions:** {actions}\n'
              f'**Farming start:** {stamp(e["start_ts"])}\n**End:** {stamp(e["start_ts"]+DURATION)}\n\n'
              '**Top farms**\n'+('\n'.join(f'{i}. <@{r[3]}> — {r[0]:,}c' for i,r in enumerate(leaders,1)) or 'None')+'\n\n'
              '**Recent actions**\n'+('\n'.join(f'<@{r["user_id"]}> · {r["action"]} ({r["detail"]})' for r in recent) or 'None'))
    await interaction.response.send_message(embed=make_embed('🛡️ Host Monitoring Dashboard',desc[:4000]),ephemeral=True)

@harvest.command(name='history',description='Host only: inspect one player’s recent transactions')
async def history(interaction:discord.Interaction,member:discord.User):
    if not is_host(interaction):
        await interaction.response.send_message('Host only.',ephemeral=True);return
    async with lock:
        rows=list(con.execute('SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 20',(member.id,)))
    desc='\n'.join(f'{stamp(r["ts"])} · {r["action"]} ({r["detail"]}) · {r["coin_change"]:+,}c' for r in rows) or 'No transactions.'
    await interaction.response.send_message(embed=make_embed(f'📋 Transactions — {member.display_name}',desc[:4000]),ephemeral=True)

async def send_announcement_checked(message,ping=False):
    if not CHANNEL_ID: return False
    channel=bot.get_channel(CHANNEL_ID)
    if channel is None:
        try: channel=await bot.fetch_channel(CHANNEL_ID)
        except (discord.HTTPException,discord.Forbidden): return False
    content=(f'<@&{ROLE_ID}>\n' if ping and ROLE_ID else '')+message
    try:
        await channel.send(content,allowed_mentions=discord.AllowedMentions(roles=True,users=False,everyone=False))
        return True
    except discord.HTTPException:
        logging.exception('Unable to post event announcement')
        return False

@tasks.loop(minutes=1)
async def event_watcher():
    async with lock:
        e=event()
        if not e:return
        if e['live_test']:
            return  # No scheduled session, midweek or final-result pings during live testing.
        if e['ended'] and e['final_sent']:return
        now=int(time.time())
        notice=None
        if now >= e['start_ts']+DURATION:
            ranks=board(e)
            winner=ranks[0][3] if ranks else None
            con.execute('UPDATE event SET ended=1,winner_id=? WHERE id=1',(winner,))
            con.commit()
            msg=('🏆 **HARVEST HUSTLE — FINAL RESULTS!**\n'+'\n'.join(f'**{i}.** <@{r[3]}> — **{r[0]:,}** farm value' for i,r in enumerate(ranks[:10],1))+f'\n\n🍂 **Champion: <@{winner}>!**') if ranks else '🍂 Harvest Hustle has ended. No players joined.'
            notice=('final',msg,False)
        elif now < e['start_ts']:
            return
        else:
            w=current_window(e)
            if e['last_session_notice']<w:
                day=w//2+1; session=w%2+1
                msg=(f'🍁 **HARVEST HUSTLE — DAY {day}, SESSION {session} IS OPEN!**\n'
                     f'You have **10 planting actions** until {stamp(e["start_ts"]+(w+1)*WINDOW)}.\n'
                     'Open `/harvest farm` to plant, harvest, sell or upgrade. Only planting uses actions.\n'
                     'Use `/harvest objectives` to claim today’s mission rewards.')
                notice=('session',msg,True,w)
            elif w>=7 and not e['midweek_sent']:
                ranks=board(e)
                msg='🍂 **HARVEST HUSTLE — MIDWEEK LEADERBOARD!**\n'+('\n'.join(f'**{i}.** <@{r[3]}> — **{r[0]:,}** farm value' for i,r in enumerate(ranks[:10],1)) or 'No players yet.')+'\nFour days remaining!'
                notice=('midweek',msg,False)
            else:return
    # Send outside DB lock, then persist notification state on success.
    if await send_announcement_checked(notice[1],ping=notice[2]):
        async with lock:
            if notice[0]=='session':
                con.execute('UPDATE event SET last_session_notice=? WHERE id=1 AND last_session_notice<?',(notice[3],notice[3]))
            elif notice[0]=='midweek':
                con.execute('UPDATE event SET midweek_sent=1 WHERE id=1')
            elif notice[0]=='final':
                con.execute('UPDATE event SET final_sent=1 WHERE id=1')
            con.commit()
# Individual harvest-ready DMs, checked once per minute. One alert per player
# and maturity timestamp; delivery failures do not stop the event watcher.
@tasks.loop(minutes=1)
async def harvest_alert_watcher():
    async with lock:
        e=event()
        if not active(e):
            return
        now=int(time.time())
        due={}
        for r in con.execute('SELECT * FROM plots'):
            ready_ts=crop_ready_ts(r,e)
            if ready_ts<=now and not con.execute(
                'SELECT 1 FROM harvest_alerts WHERE user_id=? AND ready_ts=?',
                (r['user_id'],ready_ts)).fetchone():
                due.setdefault((r['user_id'],ready_ts),[]).append(r['crop'])
    for (uid,ready_ts),crops in due.items():
        try:
            user=bot.get_user(uid) or await bot.fetch_user(uid)
            names=', '.join(CROPS[k]['name'] for k in crops)
            await user.send(f'🧺 **Harvest Hustle — Your crops are ready!**\n'
                            f'{names} have finished growing. Open **`/harvest farm`** '
                            f'and click **Harvest Ready**, then **Sell Produce** when you want to sell. '
                            f'Your crops stay ready until harvested.')
        except (discord.Forbidden,discord.HTTPException) as exc:
            logging.info('Could not DM harvest alert to %s: %s',uid,exc)
        finally:
            # Avoid repeated DMs every minute, even when privacy settings block them.
            async with lock:
                con.execute('INSERT OR IGNORE INTO harvest_alerts(user_id,ready_ts) VALUES(?,?)',(uid,ready_ts))
                con.commit()

@harvest_alert_watcher.before_loop
async def before_harvest_alerts():
    await bot.wait_until_ready()

@event_watcher.before_loop
async def before_watcher():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    logging.info('Logged in as %s',bot.user)


# Host-only sandbox. This is deliberately separate from the live event and SQLite.
# All state lives in this private Discord view; no event, player or leaderboard data is touched.
class HarvestTestView(discord.ui.View):
    def __init__(self, host_id):
        super().__init__(timeout=1800)
        self.host_id = host_id
        self.session = 0
        self.test_hour = 0
        self.coins = START_COINS
        self.plots = {}
        self.inventory = {crop: 0 for crop in CROPS}
        self.actions = 0
        self.extra_plot = 0
        self.market = 0
        self.planted = 0
        self.harvested = 0
        self.sold = 0

    async def interaction_check(self, interaction):
        if interaction.user.id != self.host_id:
            await interaction.response.send_message('Only the host who opened this sandbox can use it.', ephemeral=True)
            return False
        return True

    def value(self):
        return (self.coins + sum(int(qty * CROPS[k]['sale'] * (1 + self.market * 0.06))
                   for k, qty in self.inventory.items())
                + plot_resale_value(self.extra_plot)
                + self.market * UPGRADES['market']['resale'])

    def summary(self, latest_action=None):
        lines = []
        for slot in range(1, PLOTS + self.extra_plot + 1):
            if slot not in self.plots:
                lines.append(f'{slot}. Empty')
            else:
                crop, planted_session = self.plots[slot]
                left = max(0, CROPS[crop]['hours'] - (self.test_hour - planted_session))
                lines.append(f'{slot}. {CROPS[crop]["name"]} — ' + ('✅ Ready! Click Harvest Ready' if not left else f'⏳ {left} simulated hours remaining'))
        inv = ', '.join(f'{CROPS[k]["name"]} x{qty}' for k, qty in self.inventory.items() if qty) or 'Empty'
        embed = make_embed('🧪 Harvest Hustle — PRIVATE TEST SANDBOX',
                           f'**Day {self.test_hour // 24 + 1} · Session {self.test_hour // 12 % 2 + 1}** (of 14) · Hour {self.test_hour}/168\n'
                           f'**Coins:** {self.coins:,} · **Farm value:** {self.value():,}\n'
                           f'**Planting actions:** {self.actions}/{ACTIONS} used\n'
                           f'**Plots:** {PLOTS + self.extra_plot} · **Market stalls:** {self.market}\n'
                           f'**Planted:** {self.planted} · **Harvested:** {self.harvested} · **Sales Revenue:** {self.sold:,}c\n\n'
                           '**Plots**\n' + '\n'.join(lines) + '\n\n**Inventory:** ' + inv + '\n\n⏩ **Advance 2 Hours** grows crops; planting actions refresh every 12 hours. Once ready, click **Harvest Ready**, then **Sell Produce**.')
        if latest_action:
            embed.add_field(name='🍂 LATEST ACTION', value=latest_action[:1024], inline=False)
        embed.set_footer(text='TEST ONLY · No real coins, event records, announcements or leaderboard changes')
        return embed

    async def refresh(self, interaction, notice):
        # Keep the farm and the latest action together in one private message.
        await interaction.response.edit_message(embed=self.summary(latest_action=notice), view=self)

    @discord.ui.select(placeholder='🧪 Plant a test crop', row=0,
                       options=[discord.SelectOption(label=v['name'], value=k,
                                  description=f'Seed {v["cost"]}c · {v["hours"]}h · sells {v["sale"]}c')
                                for k, v in CROPS.items()])
    async def plant_test(self, interaction, select):
        crop = select.values[0]
        cfg = CROPS[crop]
        free = next((i for i in range(1, PLOTS + self.extra_plot + 1) if i not in self.plots), None)
        if self.test_hour >= 168:
            msg = 'Test has ended. Reset to start again.'
        elif self.actions >= ACTIONS:
            msg = 'No planting actions left; advance to the next session.'
        elif free is None:
            msg = 'All plots are occupied. Harvest mature crops first.'
        elif self.coins < cfg['cost']:
            msg = 'Not enough test coins.'
        else:
            self.plots[free] = (crop, self.test_hour)
            self.coins -= cfg['cost']
            self.actions += 1
            self.planted += 1
            msg = f'Planted {cfg["name"]} in plot {free}.'
        await self.refresh(interaction, msg)

    @discord.ui.button(label='Harvest Ready', emoji='🧺', style=discord.ButtonStyle.success, row=1)
    async def harvest_test(self, interaction, button):
        ready = [(slot, crop) for slot, (crop, planted) in self.plots.items()
                 if self.test_hour - planted >= CROPS[crop]['hours']]
        for slot, crop in ready:
            self.inventory[crop] += 1
            del self.plots[slot]
        self.harvested += len(ready)
        await self.refresh(interaction, f'Harvested {len(ready)} ready crop(s).')

    @discord.ui.button(label='Sell Produce', emoji='🪙', style=discord.ButtonStyle.primary, row=1)
    async def sell_test(self, interaction, button):
        earned = sum(int(qty * CROPS[k]['sale'] * (1 + self.market * 0.06))
                     for k, qty in self.inventory.items())
        self.coins += earned
        self.sold += earned
        self.inventory = {crop: 0 for crop in CROPS}
        await self.refresh(interaction, f'Sold stored produce for {earned:,} test coins.')

    @discord.ui.button(label='Advance 2 Hours', emoji='⏩', style=discord.ButtonStyle.secondary, row=1)
    async def next_test(self, interaction, button):
        if self.test_hour < 168:
            previous=self.test_hour
            self.test_hour=min(168,self.test_hour+2)
            self.session=self.test_hour//12
            if self.test_hour//12>previous//12 and self.test_hour<168:
                self.actions=0
            newly_ready=[crop for slot,(crop,planted) in self.plots.items()
                         if previous < planted + CROPS[crop]['hours'] <= self.test_hour]
            ready_count=sum(1 for crop, planted in self.plots.values() if self.test_hour-planted>=CROPS[crop]['hours'])
            msg=(f'Advanced to hour {self.test_hour}/168. '
                 + (f'🔔 **SIMULATED HARVEST ALERT:** {len(newly_ready)} crop(s) just matured! '
                    f'In the live game this triggers a DM. Click **Harvest Ready**, then **Sell Produce**. '
                    if newly_ready else
                    (f'🧺 {ready_count} crop(s) ready! Click **Harvest Ready**, then **Sell Produce**.'
                     if ready_count else 'Crops are still growing.')))

        else:
            msg=f'Test finished! Final farm value: {self.value():,}. Unharvested crops count for zero.'
        await self.refresh(interaction,msg)

    @discord.ui.button(label='Buy Plot (tiered)', emoji='🌱', style=discord.ButtonStyle.secondary, row=2)
    async def plot_test(self, interaction, button):
        if self.extra_plot >= 10:
            msg = 'Maximum extra plots reached.'
        elif self.coins < plot_price(self.extra_plot):
            msg = 'Not enough test coins.'
        else:
            self.coins -= plot_price(self.extra_plot)
            self.extra_plot += 1
            msg = 'Purchased an extra test plot.'
        await self.refresh(interaction, msg)

    @discord.ui.button(label='Buy Stall (1200c)', emoji='🛒', style=discord.ButtonStyle.secondary, row=2)
    async def market_test(self, interaction, button):
        if self.market >= 2:
            msg = 'Maximum market stalls reached.'
        elif self.coins < 1200:
            msg = 'Not enough test coins.'
        else:
            self.coins -= 1200
            self.market += 1
            msg = 'Purchased a market stall (+6% sale value).'
        await self.refresh(interaction, msg)

    @discord.ui.button(label='Send Test DM', emoji='🔔', style=discord.ButtonStyle.secondary, row=3)
    async def dm_test(self, interaction, button):
        try:
            await interaction.user.send(
                '🧪 **Harvest Hustle — Test harvest notification**\n'
                'This is a test of the automatic DM system. When your crops mature '
                'in the live event, you will receive a message telling you to open '
                '`/harvest farm` and click **Harvest Ready**, then **Sell Produce**.\n'
                'This test does not affect the live event.')
            msg='✅ Test DM sent! Check your Discord direct messages.'
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.info('Could not send test harvest DM to %s: %s', interaction.user.id, exc)
            msg='⚠️ Could not deliver the test DM. Check your Discord DM privacy settings and try again.'
        await self.refresh(interaction, msg)

    @discord.ui.button(label='Save Farm to DMs', emoji='📩', style=discord.ButtonStyle.secondary, row=3)
    async def save_test(self, interaction, button):
        try:
            await interaction.user.send(embed=self.summary())
            await interaction.response.send_message('📩 A copy of your test farm was sent to your DMs, where it will remain available.', ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message('I could not send a DM. Please enable server DMs for the bot.', ephemeral=True)

    @discord.ui.button(label='Close', emoji='✖️', style=discord.ButtonStyle.secondary, row=3)
    async def close_test(self, interaction, button):
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label='Reset Test', emoji='🔄', style=discord.ButtonStyle.danger, row=2)
    async def reset_test(self, interaction, button):
        self.__init__(self.host_id)
        await self.refresh(interaction, 'Test sandbox reset to the starting farm.')


@harvest.command(name='test', description='Host only: try farming in a private sandbox without affecting the live game')
async def harvest_test(interaction: discord.Interaction):
    if not is_host(interaction):
        await interaction.response.send_message('Host only.', ephemeral=True)
        return
    view = HarvestTestView(interaction.user.id)
    await interaction.response.send_message('Your latest action appears at the bottom of this private farm dashboard. Use **Advance 2 Hours** to simulate crop maturity alerts, or **Send Test DM** to check real DM delivery. This test farm is separate from the live game.',
                                            embed=view.summary(), view=view, ephemeral=True)

bot.tree.add_command(harvest)

logging.info("RUNNING FILE: %s", __file__)
logging.info("LOCAL GROUPS: %s", [c.name for c in bot.tree.get_commands()])
logging.info("HARVEST SUBCOMMANDS: %s", [c.name for c in harvest.commands])

async def main():
    async with bot:
        await bot.login(TOKEN)

        guild = discord.Object(id=GUILD_ID)
        # Replace stale guild-scoped commands (such as the previous /quiz group)
        # with the current Harvest Hustle command tree before syncing.
        # This only changes this bot account's commands in this guild.
        bot.tree.clear_commands(guild=guild)
        bot.tree.copy_global_to(guild=guild)
        logging.info("TARGET GUILD: %s", GUILD_ID)
        logging.info("GUILD COMMANDS BEFORE SYNC: %s", [c.name for c in bot.tree.get_commands(guild=guild)])

        synced = await bot.tree.sync(guild=guild)
        print(f"Successfully synced {len(synced)} slash commands.")

        bot.add_view(HarvestRolePanel())
        bot.add_view(HarvestRegistrationView())
        # Preserve existing event data, but correct the previous 9 November schedule when no players have joined.
        async with lock:
            if event() is None and not test_mode_active():
                con.execute('INSERT INTO event(id,start_ts,ended) VALUES(1,?,0)', (FARMING_AT,))
                con.commit()
                logging.info('Scheduled game start for 8 November 2026 00:00 UTC; host posts join panel manually')
            elif (event()['start_ts'] == FARMING_AT + 24*3600 and
                  con.execute('SELECT COUNT(*) FROM players').fetchone()[0] == 0 and
                  not event()['ended'] and not event()['live_test']):
                con.execute('UPDATE event SET start_ts=?, registration_ts=NULL WHERE id=1', (FARMING_AT,))
                con.commit()
                logging.info('Migrated unused November event start to 8 November 2026 00:00 UTC')

        if not event_watcher.is_running():
            event_watcher.start()
        if not harvest_alert_watcher.is_running():
            harvest_alert_watcher.start()

        await bot.connect(reconnect=True)


if __name__ == "__main__":
    asyncio.run(main())
