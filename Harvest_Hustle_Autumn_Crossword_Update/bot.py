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
CHANNEL_ID = 1567980153039098028  # Official game channel; fixed to avoid stale Railway EVENT_CHANNEL_ID
ECONOMY_CHANNEL_ID = 1568316136104140810  # Six-hour weather and market announcements
ROLE_PANEL_CHANNEL_ID = int(os.environ.get('ROLE_PANEL_CHANNEL_ID', '1567980502965551144'))
ROLE_ID = int(os.environ.get('EVENT_ROLE_ID', '1567982021106602065'))
DB_PATH = os.environ.get('DATABASE_PATH', 'harvest.sqlite3')
WINDOW = 12 * 3600
DURATION = 14 * WINDOW
FARMING_AT = int(datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc).timestamp())
ACTIONS = 10  # Starting actions; each purchased plot adds one action per session.
START_COINS = 2000
PLOTS = 10
CROPS = {
    'carrot': {'name':'🥕 Carrots', 'cost':80, 'sale':230, 'hours':2},
    'corn': {'name':'🌽 Corn', 'cost':130, 'sale':320, 'hours':4},
    'apple': {'name':'🍎 Apples', 'cost':200, 'sale':440, 'hours':6},
    'pumpkin': {'name':'🎃 Pumpkins', 'cost':250, 'sale':540, 'hours':8},
    'gourd': {'name':'🍂 Golden Gourds', 'cost':250, 'sale':500, 'hours':12},
}
# Six-hour economy: deterministic time windows, with persisted market/weather rolls.
import random
import math
WEATHER = ('sun', 'rain', 'breeze', 'clear', 'irrigation')
MARKET_MODIFIERS = (-0.10, -0.10, -0.10, -0.05, 0.0, 0.10, 0.10, 0.15)  # zero-average market; -10% to +15%
POTION_PRICE = 400
ANIMAL_LIMIT = 10
ANIMALS = {
    'chicken': {'name': '🐔 Chicken', 'produce': '🥚 Eggs', 'cost': 900, 'sale': 90, 'hours': 4},
    'pig': {'name': '🐷 Pig', 'produce': '🍄 Truffles', 'cost': 1600, 'sale': 240, 'hours': 8},
    'cow': {'name': '🐄 Cow', 'produce': '🥛 Milk', 'cost': 2400, 'sale': 420, 'hours': 12},
}


def cycle_number(e, now=None):
    return max(0, (int(time.time() if now is None else now)-e['start_ts']) // 21600)

def current_conditions(e):
    n=cycle_number(e)
    row=con.execute('SELECT * FROM farm_cycles WHERE cycle=?',(n,)).fetchone()
    return row

def market_modifier(e, crop):
    row=current_conditions(e)
    return float(row['market_pct']) if row and row['market_crop']==crop else 0.0

def sale_unit(e, p, crop):
    return int(round(CROPS[crop]['sale'] * (1+p['market']*0.06) * (1+market_modifier(e,crop))))

def ensure_cycle(e, n):
    row=con.execute('SELECT * FROM farm_cycles WHERE cycle=?',(n,)).fetchone()
    if row:return row
    # Stable, fair shared conditions across all players; no rerolls after restart.
    rng=random.Random(f'{e["start_ts"]}:harvest:{n}')
    weather=rng.choice(WEATHER)
    crop=rng.choice(list(CROPS))
    pct=rng.choice(MARKET_MODIFIERS)
    con.execute('INSERT INTO farm_cycles(cycle,weather,market_crop,market_pct) VALUES(?,?,?,?)',(n,weather,crop,pct))
    return con.execute('SELECT * FROM farm_cycles WHERE cycle=?',(n,)).fetchone()

def active_orders(e, now=None):
    now=int(time.time() if now is None else now)
    return list(con.execute('SELECT * FROM delivery_orders WHERE starts<=? AND ends>? ORDER BY starts',(now,now)))

def ensure_delivery(e,n):
    if con.execute('SELECT 1 FROM delivery_orders WHERE cycle=?',(n,)).fetchone():return
    rng=random.Random(f'{e["start_ts"]}:delivery:{n}')
    kind=('quick','standard','premium')[n%3]
    options={'quick':('carrot','corn'),'standard':('apple','pumpkin'),'premium':('gourd','pumpkin')}
    crop=rng.choice(options[kind]);qty={'quick':4,'standard':3,'premium':2}[kind]
    duration={'quick':12,'standard':18,'premium':24}[kind]*3600
    starts=e['start_ts']+n*21600
    reward=int(round(qty*CROPS[crop]['sale']*1.18))
    con.execute('INSERT INTO delivery_orders(cycle,starts,ends,kind,crop,qty,reward) VALUES(?,?,?,?,?,?,?)',(n,starts,starts+duration,kind,crop,qty,reward))

def weather_growth_factor(e, ts):
    n=max(0,(ts-e['start_ts'])//21600)
    row=con.execute('SELECT weather FROM farm_cycles WHERE cycle=?',(n,)).fetchone()
    return 0.9 if row and row['weather']=='sun' else 1.0

def apply_automatic_irrigation(e, now):
    row=current_conditions(e)
    if not row or row['weather'] not in ('rain','irrigation'):return
    for r in con.execute('SELECT * FROM plots WHERE boost_used=0'):
        if crop_thirst_ts(r,e)<=now and crop_ready_ts(r,e)>now:
            con.execute('UPDATE plots SET boost_used=1 WHERE user_id=? AND slot=?',(r['user_id'],r['slot']))

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
    return int(planted) + max(60, int(CROPS[plot['crop']]['hours'] * 3600 * (1.0 if plot['boost_used'] else 1.25) * (0.9 if plot['boost_seconds']==1 else 1.0) * (1.15 if plot['pest_penalty'] else 1.0)) - int(plot['potion_seconds']))

def crop_thirst_ts(plot, event_row):
    planted = plot['planted_ts'] or event_row['start_ts'] + plot['planted_window'] * WINDOW
    return int(planted) + int(CROPS[plot['crop']]['hours'] * 3600 * 0.5)

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
con.execute('''CREATE TABLE IF NOT EXISTS watering_alerts (
    user_id INTEGER NOT NULL, slot INTEGER NOT NULL, planted_ts INTEGER NOT NULL,
    PRIMARY KEY(user_id, slot, planted_ts)
)''')
con.execute('''CREATE TABLE IF NOT EXISTS harvest_alerts (
    user_id INTEGER NOT NULL, ready_ts INTEGER NOT NULL,
    PRIMARY KEY(user_id, ready_ts)
)''')
con.executescript("""
CREATE TABLE IF NOT EXISTS farm_cycles(cycle INTEGER PRIMARY KEY, weather TEXT NOT NULL, market_crop TEXT NOT NULL, market_pct REAL NOT NULL, announced INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS delivery_orders(cycle INTEGER PRIMARY KEY, starts INTEGER NOT NULL, ends INTEGER NOT NULL, kind TEXT NOT NULL, crop TEXT NOT NULL, qty INTEGER NOT NULL, reward INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_claims(user_id INTEGER NOT NULL, cycle INTEGER NOT NULL, PRIMARY KEY(user_id,cycle));
CREATE TABLE IF NOT EXISTS pest_alerts(user_id INTEGER NOT NULL, slot INTEGER NOT NULL, planted_ts INTEGER NOT NULL, alerted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,slot,planted_ts));
CREATE TABLE IF NOT EXISTS potions(user_id INTEGER NOT NULL, day INTEGER NOT NULL, bought INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,day));
CREATE TABLE IF NOT EXISTS pest_cycles(user_id INTEGER NOT NULL, cycle INTEGER NOT NULL, PRIMARY KEY(user_id,cycle));
""")
con.executescript("""
CREATE TABLE IF NOT EXISTS livestock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    species TEXT NOT NULL,
    last_collection_ts INTEGER NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS animal_inventory (
    user_id INTEGER NOT NULL,
    species TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id,species)
);
""")
add_column('plots','potion_used','INTEGER NOT NULL DEFAULT 0')
add_column('plots','potion_seconds','INTEGER NOT NULL DEFAULT 0')
add_column('plots','pest_penalty','INTEGER NOT NULL DEFAULT 0')
add_column('plots','breeze_bonus','INTEGER NOT NULL DEFAULT 0')
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
    for table in ('watering_alerts','harvest_alerts','transactions','objective_claims','inventory','plots','players','event','farm_cycles','delivery_orders','delivery_claims','pest_alerts','potions','pest_cycles','livestock','animal_inventory'):
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
# Synchronise ALL additive columns, not just new plot features. Older test DBs
# may lack event registration fields, player counters or watering timestamps.
for table in ('event', 'players', 'plots', 'inventory', 'transactions',
              'objective_claims', 'watering_alerts', 'harvest_alerts',
              'farm_cycles', 'delivery_orders', 'delivery_claims',
              'pest_alerts', 'potions', 'pest_cycles', 'livestock', 'animal_inventory'):
    official_columns = official_con.execute(f'PRAGMA table_info({table})').fetchall()
    test_columns = {r['name'] for r in test_con.execute(f'PRAGMA table_info({table})')}
    for col in official_columns:
        name = col['name']
        if name in test_columns:
            continue
        # All newly added columns in the live schema are optional or have defaults.
        # Reproduce the column definition including its default and NOT NULL flag.
        definition = col['type'] or 'TEXT'
        if col['notnull']:
            definition += ' NOT NULL'
        if col['dflt_value'] is not None:
            definition += f" DEFAULT {col['dflt_value']}"
        test_con.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {definition}')
        logging.info('Migrated test database: %s.%s', table, name)
test_con.commit()

def test_mode_active():
    return official_con.execute('SELECT mode FROM harvest_runtime WHERE id=1').fetchone()['mode'] == 'test'

con = test_con if test_mode_active() else official_con

def clear_test_tables():
    for table in ('watering_alerts','harvest_alerts','transactions','objective_claims','inventory','plots','players','event','farm_cycles','delivery_orders','delivery_claims','pest_alerts','potions','pest_cycles','livestock','animal_inventory'):
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

def planting_action_limit(p):
    return available_plots(p)  # 10–20 actions, matching owned plots.

def inventory_value(uid, p):
    multiplier = 1 + p['market'] * 0.06
    return sum(int(q['qty'] * CROPS[q['crop']]['sale'] * multiplier) for q in con.execute('SELECT * FROM inventory WHERE user_id=?',(uid,)))

def livestock_batches(animal, now=None):
    now = int(time.time() if now is None else now)
    period = ANIMALS[animal['species']]['hours'] * 3600
    return min(2, max(0, (now - animal['last_collection_ts']) // period))

def livestock_resale(uid):
    return sum(ANIMALS[r['species']]['cost'] // 2 for r in con.execute(
        'SELECT species FROM livestock WHERE user_id=?', (uid,)))

def livestock_inventory_value(uid):
    return sum(r['qty'] * ANIMALS[r['species']]['sale'] for r in con.execute(
        'SELECT species,qty FROM animal_inventory WHERE user_id=?', (uid,)))

def farm_value(uid, p, e):
    # Unharvested crops have no end-of-event value; the rule is visible throughout.
    return p['coins'] + inventory_value(uid,p) + plot_resale_value(p['extra_plot']) + p['market'] * UPGRADES['market']['resale'] + livestock_resale(uid) + livestock_inventory_value(uid)

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

    @discord.ui.button(label='Water',emoji='💧',style=discord.ButtonStyle.secondary,row=1)
    async def care(self, interaction, button):
        await do_crop_care(interaction, self.uid)

    @discord.ui.button(label='Sell Produce',emoji='🪙',style=discord.ButtonStyle.primary,row=0)
    async def sell(self,interaction,button):
        await do_sell(interaction,self.uid)

    @discord.ui.button(label='Upgrades',emoji='⬆️',style=discord.ButtonStyle.secondary,row=2)
    async def upgrades(self,interaction,button):
        await interaction.response.send_message(embed=make_embed('🏡 The Farm Shop', 'Expand your growing space or increase your selling bonus. Upgrades cost coins, but **no planting actions**.', AUTUMN['brown']),view=UpgradeView(self.uid),ephemeral=True)

    @discord.ui.button(label='Missions',emoji='🎯',style=discord.ButtonStyle.secondary,row=2)
    async def objectives(self,interaction,button):
        await show_objectives(interaction)

    @discord.ui.button(label='Refresh',emoji='🔄',style=discord.ButtonStyle.secondary,row=3)
    async def refresh(self,interaction,button):
        await show_farm(interaction)

    @discord.ui.button(label='Save to DMs', emoji='📩', style=discord.ButtonStyle.secondary, row=3)
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

    @discord.ui.button(label='Remove Pests',emoji='🐛',style=discord.ButtonStyle.secondary,row=1)
    async def pests(self,interaction,button):
        await do_pests(interaction,self.uid)

    @discord.ui.button(label='Potion · 400c',emoji='🧪',style=discord.ButtonStyle.secondary,row=1)
    async def potion(self,interaction,button):
        await do_potion(interaction,self.uid)

    @discord.ui.button(label='Deliveries',emoji='🚚',style=discord.ButtonStyle.secondary,row=2)
    async def deliveries(self,interaction,button):
        await show_deliveries(interaction,self.uid)

    @discord.ui.button(label='Animals', emoji='🐾', style=discord.ButtonStyle.success, row=2)
    async def animals(self, interaction, button):
        await show_animals(interaction, self.uid)

    @discord.ui.button(label='Close', emoji='✖️', style=discord.ButtonStyle.secondary, row=3)
    async def close(self, interaction, button):
        # Explicitly dismiss only this private farm panel when the player chooses.
        await interaction.response.edit_message(view=None)
        self.stop()


class AnimalView(discord.ui.View):
    def __init__(self, uid):
        super().__init__(timeout=900)
        self.uid = uid

    async def interaction_check(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message('Open your own farm to manage animals.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Buy Chicken',emoji='🐔',style=discord.ButtonStyle.success,row=0)
    async def chicken(self, interaction, button):
        await animal_purchase(interaction,self.uid,'chicken')

    @discord.ui.button(label='Buy Pig',emoji='🐷',style=discord.ButtonStyle.success,row=0)
    async def pig(self, interaction, button):
        await animal_purchase(interaction,self.uid,'pig')

    @discord.ui.button(label='Buy Cow',emoji='🐄',style=discord.ButtonStyle.success,row=0)
    async def cow(self, interaction, button):
        await animal_purchase(interaction,self.uid,'cow')

    @discord.ui.button(label='Collect Produce',emoji='🧺',style=discord.ButtonStyle.primary,row=1)
    async def collect(self, interaction, button):
        await animal_collect(interaction,self.uid)

    @discord.ui.button(label='Sell Produce',emoji='🪙',style=discord.ButtonStyle.primary,row=1)
    async def sell(self, interaction, button):
        await animal_sell(interaction,self.uid)

    @discord.ui.button(label='Sell Animal',emoji='🔄',style=discord.ButtonStyle.secondary,row=2)
    async def resell(self, interaction, button):
        async with lock:
            animals=list(con.execute('SELECT id,species FROM livestock WHERE user_id=? ORDER BY id',(self.uid,)))
        if not animals:
            await interaction.response.send_message('You have no animals to sell.',ephemeral=True)
            return
        await interaction.response.send_message('Choose an animal to sell for 50% of its purchase price. **Collect its produce first**; uncollected batches are lost when sold.',view=AnimalSellView(self.uid,animals),ephemeral=True)

    @discord.ui.button(label='Back to Farm',emoji='↩️',style=discord.ButtonStyle.secondary,row=2)
    async def back(self, interaction, button):
        await show_farm(interaction,edit_existing=True)


def animals_embed(uid):
    p=player(uid)
    rows=list(con.execute('SELECT * FROM livestock WHERE user_id=? ORDER BY id',(uid,)))
    inv=list(con.execute('SELECT species,qty FROM animal_inventory WHERE user_id=? AND qty>0',(uid,)))
    lines=[]
    now=int(time.time())
    for a in rows:
        species=ANIMALS[a['species']]
        batches=livestock_batches(a,now)
        next_ts=a['last_collection_ts']+species['hours']*3600
        status=(f'🧺 {batches}/2 ready' if batches else f'⏳ Next <t:{next_ts}:R>')
        lines.append(f'**#{a["id"]} {species["name"]}** · {status}')
    desc=(f'**Animals:** {len(rows)}/{ANIMAL_LIMIT}  •  **Coins:** {p["coins"]:,}c\n'
          'Each animal stores **up to two batches**. Production pauses when full.\n'
          'Collect produce, then sell it separately. No planting actions needed.\n\n'
          + ('\n'.join(lines) if lines else '*No animals yet.*'))
    embed=make_embed('🐾 Your Farm Animals',desc[:4000],AUTUMN['green'])
    embed.add_field(name='🛒 Animal Shop',value='🐔 Chicken **1,200c** · 🥚 90c / 4h\n🐷 Pig **2,000c** · 🍄 240c / 8h\n🐄 Cow **3,000c** · 🥛 420c / 12h',inline=False)
    embed.add_field(name='🧺 Collected Produce',value='\n'.join(f"{ANIMALS[r['species']]['produce']} ×{r['qty']} · {r['qty']*ANIMALS[r['species']]['sale']:,}c" for r in inv) or 'None',inline=False)
    embed.add_field(name='💰 Animal Resale Value',value=f'{livestock_resale(uid):,}c · included in farm value',inline=False)
    return embed

async def show_animals(interaction,uid,notice=None):
    async with lock:
        e=event()
        if not active(e) or not player(uid):
            await interaction.response.send_message('The official farm is not active for you.',ephemeral=True)
            return
        embed=animals_embed(uid)
        if notice:embed.add_field(name='Latest action',value=notice[:1000],inline=False)
    await interaction.response.edit_message(embed=embed,view=AnimalView(uid))

async def animal_purchase(interaction,uid,species):
    spec=ANIMALS[species]
    async with lock:
        e=event();p=player(uid)
        count=con.execute('SELECT COUNT(*) FROM livestock WHERE user_id=?',(uid,)).fetchone()[0]
        if not active(e) or not p:msg='The farm is not active.'
        elif int(time.time())>=e['start_ts']+DURATION-12*3600:msg='Animal purchases close during the final 12 hours.'
        elif count>=ANIMAL_LIMIT:msg=f'Maximum {ANIMAL_LIMIT} animals reached.'
        elif p['coins']<spec['cost']:msg=f'Not enough coins for {spec["name"]} ({spec["cost"]:,}c).'
        else:
            con.execute('INSERT INTO livestock(user_id,species,last_collection_ts) VALUES(?,?,?)',(uid,species,int(time.time())))
            con.execute('UPDATE players SET coins=coins-? WHERE user_id=?',(spec['cost'],uid))
            log_action(uid,e,'buy_animal',species,-spec['cost'])
            con.commit()
            msg=f'Purchased {spec["name"]} for {spec["cost"]:,}c. First produce in {spec["hours"]} hours.'
    await show_animals(interaction,uid,msg)

async def animal_collect(interaction,uid):
    async with lock:
        e=event();now=int(time.time());totals={}
        if active(e):
            for a in con.execute('SELECT * FROM livestock WHERE user_id=?',(uid,)):
                qty=livestock_batches(a,now)
                if not qty:continue
                totals[a['species']]=totals.get(a['species'],0)+qty
                con.execute('UPDATE livestock SET last_collection_ts=?,notified=0 WHERE id=?',(now,a['id']))
            for species,qty in totals.items():
                con.execute('INSERT INTO animal_inventory(user_id,species,qty) VALUES(?,?,?) ON CONFLICT(user_id,species) DO UPDATE SET qty=qty+excluded.qty',(uid,species,qty))
            if totals:log_action(uid,e,'collect_animals',str(totals))
            con.commit()
        msg=('Collected '+', '.join(f'{ANIMALS[k]["produce"]} ×{v}' for k,v in totals.items())+'. Sell whenever you like.' if totals else 'No animal produce is ready yet.')
    await show_animals(interaction,uid,msg)

async def animal_sell(interaction,uid):
    async with lock:
        e=event();total=0;parts=[]
        if active(e):
            for r in con.execute('SELECT species,qty FROM animal_inventory WHERE user_id=? AND qty>0',(uid,)):
                amount=r['qty']*ANIMALS[r['species']]['sale']
                total+=amount;parts.append(f'{ANIMALS[r["species"]]["produce"]} ×{r["qty"]}')
            if total:
                con.execute('UPDATE players SET coins=coins+?,sold=sold+? WHERE user_id=?',(total,total,uid))
                con.execute('DELETE FROM animal_inventory WHERE user_id=?',(uid,))
                log_action(uid,e,'sell_animal_produce',', '.join(parts),total)
                con.commit()
        msg=f'Sold {", ".join(parts)} for **{total:,}c**.' if total else 'No collected animal produce to sell.'
    await show_animals(interaction,uid,msg)

class AnimalSellSelect(discord.ui.Select):
    def __init__(self,animals):
        super().__init__(placeholder='Select animal to sell',options=[discord.SelectOption(label=f'#{a["id"]} {ANIMALS[a["species"]]["name"]} — {ANIMALS[a["species"]]["cost"]//2}c',value=str(a['id'])) for a in animals[:25]])
    async def callback(self,interaction):
        uid=interaction.user.id
        async with lock:
            e=event();a=con.execute('SELECT * FROM livestock WHERE id=? AND user_id=?',(int(self.values[0]),uid)).fetchone()
            if not active(e) or not a:msg='Animal unavailable or event inactive.'
            else:
                refund=ANIMALS[a['species']]['cost']//2
                con.execute('DELETE FROM livestock WHERE id=? AND user_id=?',(a['id'],uid))
                con.execute('UPDATE players SET coins=coins+? WHERE user_id=?',(refund,uid))
                log_action(uid,e,'sell_animal',a['species'],refund)
                con.commit()
                msg=f'Sold {ANIMALS[a["species"]]["name"]} for {refund:,}c.'
        await interaction.response.edit_message(content=msg,view=None)

class AnimalSellView(discord.ui.View):
    def __init__(self,uid,animals):
        super().__init__(timeout=180)
        self.uid=uid
        self.add_item(AnimalSellSelect(animals))
    async def interaction_check(self,interaction):
        if interaction.user.id!=self.uid:
            await interaction.response.send_message('This menu belongs to another player.',ephemeral=True)
            return False
        return True


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
                if p['actions_used']>=planting_action_limit(p): msg='No planting actions left this session.'
                elif free is None: msg='All plots are occupied. Harvest mature crops first.'
                elif p['coins']<c['cost']: msg='Not enough coins for that seed.'
                else:
                    condition=ensure_cycle(e,cycle_number(e))
                    breeze=int(condition['weather']=='breeze')
                    sunshine=int(condition['weather']=='sun')
                    con.execute('INSERT INTO plots(user_id,slot,crop,planted_window,planted_ts,boost_seconds,breeze_bonus) VALUES(?,?,?,?,?,?,?)',(uid,free,crop,current_window(e),int(time.time()),sunshine,breeze))
                    con.execute('UPDATE players SET coins=coins-? WHERE user_id=?',(c['cost'],uid))
                    use_action(uid)
                    con.execute('UPDATE players SET planted=planted+1 WHERE user_id=?',(uid,))
                    log_action(uid,e,'plant',crop,-c['cost'])
                    msg=f"Planted {c['name']} in plot {free} for {c['cost']} coins. Estimated harvest: {stamp(crop_ready_ts(con.execute('SELECT * FROM plots WHERE user_id=? AND slot=?',(uid,free)).fetchone(),e))}, assuming it is watered on time and pests are treated."
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

async def do_crop_care(interaction, uid):
    """Water only thirsty growing crops; restore normal growth time."""
    async with lock:
        e = event()
        if not active(e) or not player(uid):
            msg = 'The event is not active or you have not joined.'
        else:
            now = int(time.time())
            thirsty = [r for r in con.execute('SELECT * FROM plots WHERE user_id=?', (uid,))
                       if not r['boost_used'] and crop_thirst_ts(r, e) <= now
                       and crop_ready_ts(r, e) > now]
            for r in thirsty:
                con.execute('UPDATE plots SET boost_used=1, boost_seconds=0 WHERE user_id=? AND slot=?',
                            (uid, r['slot']))
            if thirsty:
                log_action(uid, e, 'crop_care', f'Watered {len(thirsty)} thirsty crops', 0)
                msg = (f'💧 Watered {len(thirsty)} thirsty crop(s)! They now follow their normal growth schedule. '
                       'Unwatered crops take 25% longer to mature. No coins or planting actions spent.')
            else:
                msg = 'No crops need watering right now. Crops become thirsty halfway through their normal growth time.'
            con.commit()
    await show_farm(interaction, latest_action='💧 **Watering:** ' + msg, edit_existing=True)

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
                breeze_reward=sum(int(CROPS[r['crop']]['sale']*0.05) for r in ready if r['breeze_bonus'])
                if breeze_reward:
                    con.execute('UPDATE players SET coins=coins+? WHERE user_id=?',(breeze_reward,uid))
                    log_action(uid,e,'breeze_bonus',f'{len(ready)} harvested crops',breeze_reward)
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
                earned=sum(r['qty']*sale_unit(e,p,r['crop']) for r in items)
                con.execute('DELETE FROM inventory WHERE user_id=?',(uid,))
                con.execute('UPDATE players SET coins=coins+?,sold=sold+? WHERE user_id=?',(earned,earned,uid))
                log_action(uid,e,'sell',f'{sum(x["qty"] for x in items)} crops',earned)
                msg=f'🪙 Sold {sum(x["qty"] for x in items)} crop(s) for {earned} coins (free).'
            con.commit()
    await show_farm(interaction, latest_action='🪙 **Selling:** ' + msg, edit_existing=True)

async def do_pests(interaction, uid):
    async with lock:
        e=event(); rows=[]
        if active(e) and player(uid):
            rows=list(con.execute('SELECT * FROM plots WHERE user_id=? AND pest_penalty=1',(uid,)))
            for r in rows:
                con.execute('UPDATE plots SET pest_penalty=0 WHERE user_id=? AND slot=?',(uid,r['slot']))
            if rows:log_action(uid,e,'remove_pests',f'{len(rows)} crops',0)
            con.commit()
    await show_farm(interaction,latest_action=f'🐛 Removed pests from {len(rows)} crops for free.',edit_existing=True)

async def do_potion(interaction, uid):
    async with lock:
        e=event(); msg='The game is not active.'
        if active(e) and player(uid):
            now=int(time.time());day=min(6,(now-e['start_ts'])//86400)
            p=player(uid)
            bought=con.execute('SELECT bought FROM potions WHERE user_id=? AND day=?',(uid,day)).fetchone()
            rows=[r for r in con.execute('SELECT * FROM plots WHERE user_id=? ORDER BY slot',(uid,)) if not r['potion_used'] and crop_ready_ts(r,e)-now>=3600][:5]
            if bought and bought['bought']>=2:msg='You have already purchased 2 potions today.'
            elif p['coins']<POTION_PRICE:msg='You need 400 coins for a Growth Potion.'
            elif not rows:msg='No eligible crops: potions require at least 1 hour remaining and can only be used once per crop.'
            else:
                savings=[]
                for r in rows:
                    reduction=int((crop_ready_ts(r,e)-now)*0.20)
                    savings.append((r['slot'],reduction))
                    con.execute('UPDATE plots SET potion_used=1,potion_seconds=potion_seconds+? WHERE user_id=? AND slot=?',(reduction,uid,r['slot']))
                con.execute('UPDATE players SET coins=coins-? WHERE user_id=?',(POTION_PRICE,uid))
                con.execute('INSERT INTO potions(user_id,day,bought) VALUES(?,?,1) ON CONFLICT(user_id,day) DO UPDATE SET bought=bought+1',(uid,day))
                log_action(uid,e,'growth_potion',f'{len(rows)} crops',-POTION_PRICE)
                msg='🧪 Potion applied to '+str(len(rows))+' crops (400c). Saved: '+', '.join(f'plot {slot}: {seconds//60}m' for slot,seconds in savings)+'. See updated timers below.'
            con.commit()
    await show_farm(interaction,latest_action=msg,edit_existing=True)

async def show_deliveries(interaction, uid):
    async with lock:
        e=event(); orders=active_orders(e) if e else []
        lines=[]
        for o in orders:
            claimed=con.execute('SELECT 1 FROM delivery_claims WHERE user_id=? AND cycle=?',(uid,o['cycle'])).fetchone()
            lines.append(f'**Order #{o["cycle"]+1} ({o["kind"]})** — {o["qty"]} × {CROPS[o["crop"]]["name"]} · **{o["reward"]}c** · ends {stamp(o["ends"])}'+(' · ✅ Delivered' if claimed else ''))
        msg='\n'.join(lines) or 'No active deliveries right now.'
    await interaction.response.send_message(embed=make_embed('🚚 Harvest Deliveries',msg),view=DeliveryView(uid),ephemeral=True)

class DeliveryView(discord.ui.View):
    def __init__(self,uid):
        super().__init__(timeout=180);self.uid=uid
    @discord.ui.button(label='Deliver Available Orders',emoji='🚚',style=discord.ButtonStyle.success)
    async def deliver(self,interaction,button):
        if interaction.user.id!=self.uid:
            await interaction.response.send_message('This is not your delivery menu.',ephemeral=True);return
        async with lock:
            e=event(); total=0;done=[]
            if active(e):
                for o in active_orders(e):
                    if con.execute('SELECT 1 FROM delivery_claims WHERE user_id=? AND cycle=?',(self.uid,o['cycle'])).fetchone():continue
                    stock=con.execute('SELECT qty FROM inventory WHERE user_id=? AND crop=?',(self.uid,o['crop'])).fetchone()
                    if stock and stock['qty']>=o['qty']:
                        con.execute('UPDATE inventory SET qty=qty-? WHERE user_id=? AND crop=?',(o['qty'],self.uid,o['crop']))
                        con.execute('INSERT INTO delivery_claims(user_id,cycle) VALUES(?,?)',(self.uid,o['cycle']))
                        total+=o['reward'];done.append(str(o['cycle']+1))
                if total:
                    con.execute('UPDATE players SET coins=coins+?,sold=sold+? WHERE user_id=?',(total,total,self.uid))
                    log_action(self.uid,e,'delivery',','.join(done),total)
                con.commit()
        await interaction.response.send_message(f'🚚 Delivered {len(done)} order(s), earned {total:,} coins.' if done else 'No active orders can be completed with your current inventory.',ephemeral=True)

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
            ensure_cycle(e,cycle_number(e))
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
                care = (' 🐛 PESTS — Remove Pests!' if r['pest_penalty'] else '') + (' 💧 Watered' if r['boost_used'] else (' 💧 **THIRSTY — Water Crops!**' if crop_thirst_ts(r,e)<=int(time.time()) and remaining>0 else ' 🌱 Growing'))
                hours_left, minutes_left = divmod((remaining + 59)//60, 60)
                duration = f'{hours_left}h {minutes_left:02d}m' if hours_left else f'{minutes_left}m'
                potion_mark = ' 🧪 Potion applied' if r['potion_used'] else ''
                lines.append(f'**{slot}.** {c["name"]} — '+('✅ **Ready! Click Harvest Ready**' if remaining==0 else f'⏳ **{duration} left** · <t:{ready_ts}:t>' + potion_mark + care))
        inv=', '.join(f"{CROPS[r['crop']]['name']} ×{r['qty']}" for r in con.execute('SELECT * FROM inventory WHERE user_id=? AND qty>0',(uid,))) or 'Empty'
        embed=make_embed('🍁 Your Autumn Farm',f'**Day {w//2+1} of 7**  •  Session {w%2+1} of 2\n🏁 **Event ends:** {stamp(e["start_ts"]+DURATION)}', AUTUMN['green'])
        condition=current_conditions(e)
        if condition:
            embed.add_field(name='🌦️ Current Weather / 📈 Market',value=f'{condition["weather"].title()} · {CROPS[condition["market_crop"]]["name"]} {condition["market_pct"]:+.0%} · updates every 6h',inline=False)
        pest_count=sum(1 for r in plots.values() if r['pest_penalty'])
        if pest_count:embed.add_field(name='🐛 Pest Alert',value=f'{pest_count} affected crops — click Remove Pests',inline=False)
        embed.add_field(name='💰 Coins',value=str(p['coins']))
        embed.add_field(name='🌱 Planting actions left',value=f'{max(0,planting_action_limit(p)-p["actions_used"])}/{planting_action_limit(p)}')
        embed.add_field(name='🏆 Farm value',value=f'{farm_value(uid,p,e):,}')
        next_reset=e['start_ts']+(w+1)*WINDOW
        if active(e): embed.add_field(name='⏳ Next planting reset',value=stamp(next_reset),inline=False)
        milestone=max((n for n in (5,15,30,50) if p['harvested']>=n),default=0)
        embed.add_field(name='🏅 Harvest milestone',value=f'{milestone} crops' if milestone else 'Not yet unlocked',inline=True)
        embed.add_field(name='🌱 Plots Owned',value=f'{available_plots(p)}/{PLOTS + UPGRADES["extra_plot"]["max"]} · {len(plots)} occupied · {available_plots(p)-len(plots)} empty',inline=False)
        embed.add_field(name='🌾 Your Growing Plots',value='\n'.join(lines)[:1024],inline=False)
        embed.add_field(name='🧺 Inventory',value=inv,inline=False)
        owned=con.execute('SELECT COUNT(*) FROM livestock WHERE user_id=?',(uid,)).fetchone()[0]
        ready_animals=sum(livestock_batches(a) for a in con.execute('SELECT * FROM livestock WHERE user_id=?',(uid,)))
        animal_inv=', '.join(f"{ANIMALS[r['species']]['produce']} ×{r['qty']}" for r in con.execute('SELECT * FROM animal_inventory WHERE user_id=? AND qty>0',(uid,))) or 'None'
        embed.add_field(name='🐾 Livestock',value=f'**{owned}/{ANIMAL_LIMIT} animals** · {ready_animals} batches ready\nStored produce: {animal_inv}\nUse **Animals** to collect or sell.',inline=False)

        embed.add_field(name='📊 Your Season So Far',value=f'🌱 Planted **{p["planted"]}**  •  🧺 Harvested **{p["harvested"]}**\n🪙 Sales Revenue **{p["sold"]:,}c**',inline=False)
        embed.add_field(name='⬆️ Upgrades',value=f'Extra plots: {p["extra_plot"]}/10 · Market stalls: {p["market"]}/2',inline=False)
        # Official farm uses persistent delivery orders, not TestFarmView state.
        orders = active_orders(e) if e else []
        if orders:
            order_lines = []
            for order in orders[-3:]:
                claimed = con.execute('SELECT 1 FROM delivery_claims WHERE user_id=? AND cycle=?', (uid, order['cycle'])).fetchone()
                order_lines.append(f'{order["qty"]} × {CROPS[order["crop"]]["name"]} → {order["reward"]}c · ends {stamp(order["ends"])}' + (' · ✅ Delivered' if claimed else ''))
            embed.add_field(name='🚚 Active Deliveries', value='\n'.join(order_lines)[:1024], inline=False)
        if latest_action:
            embed.add_field(name='🍂 LATEST ACTION', value=latest_action[:1024], inline=False)
        embed.set_footer(text='Grow & harvest  •  Crop care  •  Farm services  •  Utilities | Only planting uses actions.')
    if edit_existing:
        await interaction.response.edit_message(embed=embed, view=FarmView(uid) if active(e) else None)
    else:
        await interaction.response.send_message(embed=embed,view=FarmView(uid) if active(e) else None,ephemeral=True)

# Role panel buttons persist across restarts; last post ID is retained for compatibility.
official_con.execute('CREATE TABLE IF NOT EXISTS role_panel(id INTEGER PRIMARY KEY CHECK(id=1), message_id INTEGER NOT NULL)')
official_con.commit()

# Store every panel so role sign-ups can refresh all visible counters.
official_con.execute('CREATE TABLE IF NOT EXISTS role_panel_messages(message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)')
official_con.execute('CREATE TABLE IF NOT EXISTS secondary_join_panels(message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)')
official_con.commit()


def role_signup_count(guild: discord.Guild) -> int:
    role = guild.get_role(ROLE_ID)
    return len(role.members) if role else 0


def role_panel_embed(signups: int, with_banner: bool = False):
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
            '🏆 Climb the leaderboard over seven days'
        ), inline=False,
    )
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
            await message.edit(embed=role_panel_embed(count))
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
intents.message_content = True  # Enable Message Content Intent in Discord Developer Portal too.
bot=commands.Bot(command_prefix='!',intents=intents)

@bot.event
async def on_message(message: discord.Message):
    """Keep the official Harvest Hustle channel free of ordinary user messages."""
    if message.guild is not None and message.channel.id == CHANNEL_ID and not message.author.bot:
        try:
            await message.delete()
        except discord.Forbidden:
            logging.warning('Cannot delete message in game channel %s: missing Manage Messages permission', CHANNEL_ID)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logging.exception('Discord failed to delete message in game channel %s', CHANNEL_ID)
        return
    await bot.process_commands(message)

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
        # Always post a fresh role panel without a banner or mass mention.
        content = (
            '🎮 **TRICK OR TREAT WAS JUST THE BEGINNING!** 🍂 '
            '**HARVEST HUSTLE** is coming! 🏆 **375 HABBO CREDITS + 3 HC BOXES TO BE WON!** '
            '🚜 Claim your event role below and get ready for **8 NOVEMBER, 12 AM GMT!**')
        kwargs = dict(content=content, embed=role_panel_embed(role_signup_count(interaction.guild)),
                      view=HarvestRolePanel(), allowed_mentions=discord.AllowedMentions.none())
        message = await channel.send(**kwargs)
        official_con.execute('INSERT INTO role_panel(id,message_id) VALUES(1,?) '
                    'ON CONFLICT(id) DO UPDATE SET message_id=excluded.message_id', (message.id,))
        official_con.commit()
        official_con.execute('INSERT OR REPLACE INTO role_panel_messages(message_id, channel_id) VALUES(?, ?)', (message.id, channel.id))
        official_con.commit()
        await interaction.followup.send(
            f'🍂 A NEW role panel was posted in {channel.mention}. No mass mention was sent.', ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            'I need View Channel, Send Messages and Embed Links in the role-panel channel.', ephemeral=True)
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
        kwargs = dict(
            content='🍂 **HARVEST HUSTLE — ROLE PANEL PREVIEW** 🚜',
            embed=role_panel_embed(role_signup_count(interaction.guild)),
            view=HarvestRolePanel(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
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
            'I need View Channel, Send Messages and Embed Links in this channel.',
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


def registration_message_authorized(message: discord.Message, e) -> bool:
    """Accept the original registration post and host-created secondary join panels."""
    if message.author.id != bot.user.id:
        return False
    if message.id == e['registration_post_id']:
        return True
    record = official_con.execute(
        'SELECT 1 FROM secondary_join_panels WHERE message_id=? AND channel_id=?',
        (message.id, message.channel.id),
    ).fetchone()
    return record is not None


class HarvestRegistrationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Join Harvest Hustle', emoji='🌱', style=discord.ButtonStyle.success,
                       custom_id='harvest_hustle:register:v1')
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with lock:
            e = event()
            if not registration_open(e) or not interaction.message or not registration_message_authorized(interaction.message, e):
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
    # Acknowledge Discord immediately; DB operations and channel posting can take time.
    await interaction.response.defer(ephemeral=True)
    if not is_host(interaction):
        await interaction.followup.send('Host only.', ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.followup.send('Run this in your test text channel.', ephemeral=True)
        return
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


@harvest.command(name='join-panel', description='Host only: add another Join button to the game channel without replacing the original')
async def join_panel(interaction: discord.Interaction):
    if not is_host(interaction):
        await interaction.response.send_message('Host only.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with lock:
        e = event()
        if not registration_open(e) or not e['registration_post_id']:
            await interaction.followup.send('The official event must be started with `/harvest start` before adding another Join panel.', ephemeral=True)
            return
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(CHANNEL_ID)
            except discord.HTTPException:
                await interaction.followup.send('Could not access the game channel.', ephemeral=True)
                return
        try:
            message = await channel.send(
                embed=registration_embed(live_test=False),
                view=HarvestRegistrationView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logging.exception('Unable to post additional Join panel')
            await interaction.followup.send('Could not post the additional Join panel. Check bot permissions.', ephemeral=True)
            return
        official_con.execute(
            'INSERT OR REPLACE INTO secondary_join_panels(message_id, channel_id) VALUES(?, ?)',
            (message.id, channel.id),
        )
        official_con.commit()
    await interaction.followup.send(
        f'🌱 Additional Join panel posted in {channel.mention}. The original announcement is unchanged; both buttons use the same registration.',
        ephemeral=True,
    )


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
    await interaction.response.send_message(embed=embed, ephemeral=True)

@harvest.command(name='rules',description='Learn the game rules')
async def rules(interaction:discord.Interaction):
    crops='\n'.join(f"{v['name']}: seed {v['cost']}c → sells {v['sale']}c · grows {v['hours']} hours" for v in CROPS.values())
    e=make_embed('📖 Harvest Hustle Rules',
        '**Join using the host-posted Join button; late joining is allowed while the event is active.** Seven days, fourteen 12-hour sessions. You get **one planting action per owned plot per session (10–20)**; unused actions do not roll over. Each player starts with **2,000 coins and 10 plots**.\n\n'
        '**Plant**: one planting action and the seed cost per plot. **Water Crops**: crops become thirsty halfway through growth and trigger a DM. Water thirsty crops for free to restore normal growth time; otherwise they take 25% longer. **Harvest**: free, collects ALL ready crops. **Sell**: free, sells ALL stored produce. **Upgrade**: free action (coins required).\n\n'
        '**Crops**\n'+crops+'\n\n'
        '**Upgrades**\n🌱 Extra Plot: 500c for plots 11–14, 900c for 15–17, 1,500c for 18–20; resale is 50% of each plot’s purchase price.\n🛒 Market Stall: 1,200c, up to 2, +6% sale price per level, resale value 600c each.\n\n'
        '**Winner**: coins + stored produce sale value + upgrade resale value. **Growing crops are worth 0 at closing.** Tie order: lifetime sales, total crops harvested, earlier join, then Discord user ID.\n\n'
        'For a full walkthrough, use `/harvest help`. Harvest milestones at 5, 15, 30 and 50 crops are recognition-only. Daily missions: plant 2 crops (+50c), harvest 2 crops (+75c), sell once (+100c), plus a +75c completion bonus; claim using the **Claim Rewards** button in Daily Missions each event day. No random crop failures, market price swings or speed bonuses. Players join using the host-posted button; there is no separate registration countdown. All progress is stored in SQLite.')
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
        '3. Select **Plant**, choose a crop and an empty plot. Seeds cost coins; each planting uses **1 action**. Crops become thirsty halfway through growth; the bot sends a DM. Use **Water Crops** to water affected crops for free. Unwatered crops take 25% longer to mature.\n'
        '4. Watch the live crop timer. Once ready, use **Harvest Ready** to collect all mature crops.\n'
        '5. Click **Sell Produce** to sell everything in your inventory for coins. Harvesting and selling are **free actions**.\n\n'
        '**Starting resources:** 2,000 in-game coins, 10 plots and **10 planting actions per 12-hour session**, increasing by one action for each extra plot purchased (up to 20). Unused actions do not carry over. Crops grow in real time, independent of action resets.\n\n'
        '**Private updates:** Farm actions appear privately in the game channel. Discord controls how long ephemeral messages stay visible. Use **Save Farm to DMs** for a lasting copy.\n\n'
        '**Crop readiness:** The bot displays relative ready times and attempts to DM you when crops mature. If DMs are disabled, check `/harvest farm` yourself. Ready crops remain available until harvested.')
    crops = '\n'.join(f"{v['name']} — **{v['hours']}h** · Seed **{v['cost']}c** · Sell **{v['sale']}c**" for v in CROPS.values())
    economy = make_embed('🌱 Crops, Upgrades & Daily Missions',
        '**Crop guide**\n' + crops + '\n\n'
        '**Expand your farm**\n'
        '🌱 Extra plots: **500c each** for plots 11–14, **900c each** for plots 15–17 and **1,500c each** for plots 18–20. Maximum **20 plots**. Each purchased plot adds one planting space **and one planting action per 12-hour session**. Resale value: **50% of purchase price**.\n'
        '🛒 Market Stall: **1,200c** per level, up to **2 levels**. Each level increases produce sale prices by **6%** (maximum +12%). Resale value: **600c** per level.\n\n'
        '**Three daily missions** (claim via the **Claim Rewards** button)\n'
        '• Plant 2 crops: **+50c**\n'
        '• Harvest 2 crops: **+75c**\n'
        '• Sell produce at least once: **+100c**\n'
        '• Complete all three: **+75c daily bonus**\n'
        'Missions reset each event day. Open objectives and press **Claim Rewards** to collect earned rewards **before the next day**. Maximum **300c per day** (2,100c over seven days). Harvest milestones (5/15/30/50) are recognition-only.\n\n'
        '**Livestock — up to 10 animals total**\n'
        '🐔 Chicken: **900c**, eggs worth **90c** every **4h**\n'
        '🐷 Pig: **1,600c**, truffles worth **240c** every **8h**\n'
        '🐄 Cow: **2,400c**, milk worth **420c** every **12h**\n'
        'Buy multiple animals of the same type via **Farm → Animals**. Each stores up to **2 batches** before production pauses. The bot attempts to DM you when produce is ready; collect and sell separately. Animals do not use crop plots or planting actions. Animal resale value is **50%**; purchases close in the final **12h**.')
    finish = make_embed('🏆 Winning, Prizes & Commands',
        '**Final farm value** = remaining coins + harvested crop and animal produce in inventory at sale value + plot, stall and animal resale values. **Unharvested crops and uncollected animal produce are worth 0 at event closing.**\n'
        'Ties: lifetime produce sales → crops harvested → earlier registration → Discord user ID.\n\n'
        '**Prizes**\n🥇 **200 Habbo credits + HC Box**\n🥈 **100 Habbo credits + HC Box**\n🥉 **75 Habbo credits + HC Box**\n\n'
        '**Player commands**\n'
        '`/harvest help` — This guide\n'
        '`/harvest rules` — Rules summary\n'
        'Registration — Click **Join Harvest Hustle** in the host announcement\n'
        '`/harvest farm` — Open your farm: **Plant**, **Harvest**, **Sell**, **Water**, **Remove Pests**, **Growth Potion**, **Deliveries**, **Animals**, **Upgrades** and **Refresh**\n'
        '`/harvest objectives` — View daily missions and press **Claim Rewards** before the next event day\n'
        '`/harvest leaderboard` — Rankings\n'
        '`/harvest stats` — Your farm statistics\n'
        '`/harvest status` — Event schedule\n\n'
        '**Six-hour events:** Weather and crop market prices update at **00:00, 06:00, 12:00 and 18:00 GMT**. Deliveries have limited deadlines; pest and irrigation events may affect crops.\n'
        '**Growth Potions:** **400c**, reduce remaining growth time by **20%** for up to **5 crops**, maximum **2 purchases daily** and **once per crop**.\n\n'
        '**Fair play:** No planting-action carryover. All coin amounts above are **in-game coins** unless explicitly described as Habbo credits.')
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
            for table in ('watering_alerts','harvest_alerts','transactions','objective_claims','inventory','plots','players','event','farm_cycles','delivery_orders','delivery_claims','pest_alerts','potions','pest_cycles','livestock','animal_inventory'):
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

async def show_objectives(interaction, claim=False):
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
        earned=0
        if claim:
            for key,label,action,target,reward in DAILY_MISSIONS:
                if counts[action]>=target:
                    cur=con.execute('INSERT OR IGNORE INTO objective_claims VALUES(?,?,?)',(uid,day,key))
                    if cur.rowcount:
                        con.execute('UPDATE players SET coins=coins+? WHERE user_id=?',(reward,uid))
                        log_action(uid,e,'mission',key,reward)
                        earned+=reward
            if all(counts[action]>=target for _,_,action,target,_ in DAILY_MISSIONS):
                cur=con.execute('INSERT OR IGNORE INTO objective_claims VALUES(?,?,?)',(uid,day,'daily_bonus'))
                if cur.rowcount:
                    con.execute('UPDATE players SET coins=coins+75 WHERE user_id=?',(uid,))
                    log_action(uid,e,'mission','daily_bonus',75)
                    earned+=75
            con.execute('UPDATE players SET objectives_claimed=(SELECT COUNT(*) FROM objective_claims WHERE user_id=?) WHERE user_id=?',(uid,uid))
            con.commit()
        lines=[]
        unclaimed=0
        for key,label,action,target,reward in DAILY_MISSIONS:
            done=counts[action]>=target
            claimed=con.execute('SELECT 1 FROM objective_claims WHERE user_id=? AND day=? AND objective=?',(uid,day,key)).fetchone() is not None
            if done and not claimed:
                unclaimed+=reward
            status=' (claimed)' if claimed else ' (ready to claim)' if done else ''
            lines.append(f'{"✅" if done else "⬜"} **{label}** — {min(counts[action],target)}/{target} · **+{reward}c**'+status)
        all_done=all(counts[action]>=target for _,_,action,target,_ in DAILY_MISSIONS)
        bonus_claimed=con.execute('SELECT 1 FROM objective_claims WHERE user_id=? AND day=? AND objective=?',(uid,day,'daily_bonus')).fetchone() is not None
        if all_done and not bonus_claimed:
            unclaimed+=75
        lines.append(('✅' if all_done else '⬜') + ' **Complete all three missions** — ' +
                     ('3/3' if all_done else f'{sum(counts[action]>=target for _,_,action,target,_ in DAILY_MISSIONS)}/3') +
                     ' · **+75c bonus**' + (' (claimed)' if bonus_claimed else ' (ready to claim)' if all_done else ''))
        total=con.execute("SELECT COUNT(*) FROM objective_claims WHERE user_id=? AND objective!='daily_bonus'",(uid,)).fetchone()[0]
    suffix=(f'\n\n🪙 **Claimed now: +{earned} coins**' if claim else f'\n\n🪙 **Ready to claim: +{unclaimed} coins**')
    message='\n'.join(lines)+f'\n\n🏅 **Missions completed:** {total}'+suffix+'\nMaximum **300c per day** including the completion bonus. Claim before the next event day.'
    await interaction.response.send_message(embed=make_embed(f'🎯 Day {day+1} Daily Missions',message),view=DailyMissionClaimView(uid),ephemeral=True)

class DailyMissionClaimView(discord.ui.View):
    def __init__(self, uid):
        super().__init__(timeout=600)
        self.uid=uid

    @discord.ui.button(label='Claim Rewards',emoji='🪙',style=discord.ButtonStyle.success)
    async def claim_rewards(self, interaction:discord.Interaction, button:discord.ui.Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message('These missions belong to another player.',ephemeral=True)
            return
        await show_objectives(interaction,claim=True)

    @discord.ui.button(label='Back to Farm',emoji='🚜',style=discord.ButtonStyle.secondary)
    async def back_to_farm(self, interaction:discord.Interaction, button:discord.ui.Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message('This farm belongs to another player.',ephemeral=True)
            return
        await show_farm(interaction)

@harvest.command(name='objectives',description='View daily missions and claim rewards with the button')
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

async def send_announcement_checked(message,ping=False,channel_id=None):
    target_channel_id = CHANNEL_ID if channel_id is None else channel_id
    if not target_channel_id: return False
    channel=bot.get_channel(target_channel_id)
    if channel is None:
        try: channel=await bot.fetch_channel(target_channel_id)
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
                     f'You have **one planting action per owned plot (10–20)** until {stamp(e["start_ts"]+(w+1)*WINDOW)}.\n'
                     'Open `/harvest farm` to plant, harvest, sell or upgrade. Only planting uses actions.\n'
                     'Use `/harvest objectives` and press **Claim Rewards** to collect today’s mission rewards.')
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

@tasks.loop(minutes=1)
async def animal_alert_watcher():
    async with lock:
        e=event()
        if not active(e) or e['live_test']:return
        due={}
        for a in con.execute('SELECT * FROM livestock WHERE notified=0'):
            if livestock_batches(a)>0:
                due.setdefault(a['user_id'],[]).append((a['id'],a['species']))
        # Claim notifications before sending, to avoid duplicate DMs on overlapping ticks.
        for items in due.values():
            for animal_id,_ in items:
                con.execute('UPDATE livestock SET notified=1 WHERE id=?',(animal_id,))
        con.commit()
    for uid,items in due.items():
        try:
            user=bot.get_user(uid) or await bot.fetch_user(uid)
            names=', '.join(f'{ANIMALS[species]["name"]} #{animal_id}' for animal_id,species in items)
            await user.send(f'🐾 **Harvest Hustle — Animal Produce Ready!**\n'
                            f'{names} have produce ready to collect.\n'
                            'Open **`/harvest farm` → Animals → Collect Produce**, '
                            'then **Sell Produce** to earn coins. Each animal stores up to 2 batches.')
        except (discord.Forbidden,discord.HTTPException) as exc:
            logging.info('Animal produce DM blocked for %s: %s',uid,exc)

@animal_alert_watcher.before_loop
async def before_animal_alerts():
    await bot.wait_until_ready()

@tasks.loop(minutes=1)
async def economy_watcher():
    async with lock:
        e=event()
        if not active(e) or e['live_test']:return
        now=int(time.time());n=cycle_number(e)
        row=ensure_cycle(e,n)
        ensure_delivery(e,n)
        ensure_delivery(e,n+1)  # six-hour advance forecast
        apply_automatic_irrigation(e,now)
        # Pests: rare per-player six-hour exposure, no crop death, no repeat on same planting.
        for p in con.execute('SELECT user_id FROM players'):
            uid=p['user_id']
            rng=random.Random(f'{e["start_ts"]}:pest:{n}:{uid}')
            if con.execute('SELECT 1 FROM pest_cycles WHERE user_id=? AND cycle=?',(uid,n)).fetchone():continue
            con.execute('INSERT INTO pest_cycles(user_id,cycle) VALUES(?,?)',(uid,n))
            if rng.random()>=0.10:continue
            eligible=[r for r in con.execute('SELECT * FROM plots WHERE user_id=?',(uid,)) if not r['pest_penalty'] and crop_ready_ts(r,e)>now and not con.execute('SELECT 1 FROM pest_alerts WHERE user_id=? AND slot=? AND planted_ts=?',(uid,r['slot'],r['planted_ts'])).fetchone()]
            for r in eligible[:3]:
                con.execute('UPDATE plots SET pest_penalty=1 WHERE user_id=? AND slot=?',(uid,r['slot']))
                con.execute('INSERT OR IGNORE INTO pest_alerts(user_id,slot,planted_ts) VALUES(?,?,?)',(uid,r['slot'],r['planted_ts']))
        announced=bool(row['announced'])
        if not announced:con.execute('UPDATE farm_cycles SET announced=1 WHERE cycle=?',(n,))
        alerts=list(con.execute('SELECT user_id,COUNT(*) AS qty FROM pest_alerts WHERE alerted=0 GROUP BY user_id'))
        con.execute('UPDATE pest_alerts SET alerted=1 WHERE alerted=0')
        forecast=con.execute('SELECT * FROM delivery_orders WHERE cycle=?',(n+1,)).fetchone()
        con.commit()
    if not announced:
        pct=f'{row["market_pct"]:+.0%}'
        msg=(f'🌦️ **HARVEST WEATHER — {row["weather"].upper()}** (6 hours)\n'
             f'📈 **MARKET:** {CROPS[row["market_crop"]]["name"]} {pct} sale price.\n'
             '☀️ Sunshine: new crops grow 10% faster. 🌧️ Rain/Irrigation: thirsty crops watered automatically. 🍃 Breeze: new crops get +5% harvest bonus.\n'
             f'🚚 **NEXT DELIVERY IN 6 HOURS:** {forecast["qty"]} × {CROPS[forecast["crop"]]["name"]} ({forecast["kind"]}).\n'
             'Open **/harvest farm** → **Harvest Deliveries** for currently active orders.')
        await send_announcement_checked(msg, ping=True, channel_id=ECONOMY_CHANNEL_ID)
    for r in alerts:
        try:
            user=bot.get_user(r['user_id']) or await bot.fetch_user(r['user_id'])
            await user.send(f'🐛 **Harvest Hustle — Pest Alert!** {r["qty"]} crop(s) are affected. Open `/harvest farm` and click **Remove Pests**. Untreated crops grow 15% slower. Treatment is free.')
        except (discord.Forbidden,discord.HTTPException):
            logging.info('Pest DM blocked for %s',r['user_id'])

@economy_watcher.before_loop
async def before_economy():
    await bot.wait_until_ready()

@tasks.loop(minutes=1)
async def watering_alert_watcher():
    async with lock:
        e = event()
        if not active(e):
            return
        now = int(time.time())
        ensure_cycle(e,cycle_number(e))
        apply_automatic_irrigation(e,now)
        con.commit()
        due = {}
        for r in con.execute('SELECT * FROM plots'):
            planted = int(r['planted_ts'] or e['start_ts'] + r['planted_window'] * WINDOW)
            if (not r['boost_used'] and crop_thirst_ts(r, e) <= now < crop_ready_ts(r, e)
                and not con.execute('SELECT 1 FROM watering_alerts WHERE user_id=? AND slot=? AND planted_ts=?',
                                    (r['user_id'], r['slot'], planted)).fetchone()):
                due.setdefault(r['user_id'], []).append((r['slot'], planted, r['crop']))
    for uid, crops in due.items():
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            names = ', '.join(f"plot {slot}: {CROPS[crop]['name']}" for slot, _, crop in crops)
            await user.send('💧 **Harvest Hustle — Your crops need watering!**\n'
                            f'{len(crops)} crop(s) are thirsty ({names}).\n'
                            'Open **`/harvest farm`** and click **💧 Water Crops** to water all affected crops. '
                            'Without watering, these crops take **25% longer** to mature. '
                            'Watering is free and uses no planting actions.')
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.info('Could not DM watering alert to %s: %s', uid, exc)
        finally:
            async with lock:
                for slot, planted, _ in crops:
                    con.execute('INSERT OR IGNORE INTO watering_alerts(user_id,slot,planted_ts) VALUES(?,?,?)',
                                (uid, slot, planted))
                con.commit()

@watering_alert_watcher.before_loop
async def before_watering_alerts():
    await bot.wait_until_ready()

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
        self.daily_counts = {}  # day -> {plant, harvest, sell}
        self.daily_claimed = {}  # day -> claimed mission keys
        self.missions_completed = 0
        self.action_log = []
        self.cared_plots = set()
        self.pest_plots = set()
        self.potion_plots = set()
        self.potion_savings = {}
        self.potions_by_day = {}
        self.test_weather = 'clear'
        self.test_market_crop = 'carrot'
        self.test_market_pct = 0.0
        self.test_deliveries = {}
        self.delivered = set()
        self.last_cycle = -1
        self.advance_economy(0)


    def advance_economy(self, cycle):
        rng=random.Random(f'test:{self.host_id}:{cycle}')
        self.test_weather=rng.choice(WEATHER)
        self.test_market_crop=rng.choice(list(CROPS))
        self.test_market_pct=rng.choice(MARKET_MODIFIERS)
        kind=('quick','standard','premium')[cycle%3]
        crop=rng.choice({'quick':('carrot','corn'),'standard':('apple','pumpkin'),'premium':('gourd','pumpkin')}[kind])
        qty={'quick':4,'standard':3,'premium':2}[kind]
        self.test_deliveries[cycle]=(crop,qty,int(round(qty*CROPS[crop]['sale']*1.18)),cycle*6+{'quick':12,'standard':18,'premium':24}[kind])
        self.last_cycle=cycle
        if self.test_weather in ('rain','irrigation'):
            self.cared_plots.update(slot for slot,(crop,planted) in self.plots.items() if self.test_hour-planted>=CROPS[crop]['hours']*.5)
        if rng.random()<.10:
            eligible=[slot for slot in self.plots if slot not in self.pest_plots and self.test_hour-self.plots[slot][1]<self.test_crop_hours(slot,self.plots[slot][0])]
            self.pest_plots.update(eligible[:3])

    def test_crop_remaining(self, slot, crop, planted):
        return max(0, self.test_crop_hours(slot,crop)-(self.test_hour-planted)-self.potion_savings.get(slot,0))

    def record_action(self, action, quantity=1):
        day = min(6, self.test_hour // 24)
        counts = self.daily_counts.setdefault(day, {'plant': 0, 'harvest': 0, 'sell': 0})
        counts[action] += quantity
        self.action_log.append((self.test_hour, action, quantity))

    def mission_status(self):
        day = min(6, self.test_hour // 24)
        counts = self.daily_counts.get(day, {})
        claimed = self.daily_claimed.get(day, set())
        missions = [('plant', 2, 50), ('harvest', 2, 75), ('sell', 1, 100)]
        lines = [f'{"✅" if key in claimed else "⬜"} {key.title()} {min(counts.get(key,0), target)}/{target} — +{reward}c' for key,target,reward in missions]
        lines.append(f'{"✅" if "bonus" in claimed else "⬜"} Complete all three — +75c')
        return '\n'.join(lines)

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

    def test_crop_hours(self, slot, crop):
        return CROPS[crop]['hours'] * (1.0 if slot in self.cared_plots else 1.25) * (1.15 if slot in self.pest_plots else 1.0)

    def summary(self, latest_action=None):
        lines = []
        for slot in range(1, PLOTS + self.extra_plot + 1):
            if slot not in self.plots:
                lines.append(f'{slot}. Empty')
            else:
                crop, planted_session = self.plots[slot]
                left = self.test_crop_remaining(slot,crop,planted_session)
                lines.append(f'{slot}. {CROPS[crop]["name"]} — ' + ('✅ Ready! Click Harvest Ready' if not left else f'⏳ {left:.1f} simulated hours remaining' + (' 🧪 Potion applied' if slot in self.potion_plots else '') + (' 💧 Watered' if slot in self.cared_plots else (' 💧 THIRSTY!' if self.test_hour - planted_session >= CROPS[crop]['hours'] * 0.5 else ' 🌱 Growing'))))
        inv = ', '.join(f'{CROPS[k]["name"]} x{qty}' for k, qty in self.inventory.items() if qty) or 'Empty'
        embed = make_embed('🧪 Harvest Hustle — PRIVATE TEST SANDBOX',
                           f'**Day {self.test_hour // 24 + 1} · Session {self.test_hour // 12 % 2 + 1}** (of 14) · Hour {self.test_hour}/168\n'
                           f'**Coins:** {self.coins:,} · **Farm value:** {self.value():,}\n'
                           f'**Planting actions:** {self.actions}/{PLOTS + self.extra_plot} used\n'
                           f'**Plots:** {PLOTS + self.extra_plot}/20 · **Market stalls:** {self.market}\n'
                           f'**Planted:** {self.planted} · **Harvested:** {self.harvested} · **Sales Revenue:** {self.sold:,}c\n'
                           f'**Missions completed:** {self.missions_completed}\n'
                           f'**Weather:** {self.test_weather.title()} · **Market:** {CROPS[self.test_market_crop]["name"]} {self.test_market_pct:+.0%} · **Pests:** {len(self.pest_plots)}\n\n'
                           '**Plots**\n' + '\n'.join(lines) + '\n\n**Inventory:** ' + inv + '\n\n**Today’s missions**\n' + self.mission_status() + '\n\n⏩ **Advance 2 Hours** grows crops; planting actions refresh every 12 hours. Once ready, click **Harvest Ready**, then **Sell Produce**.')
        deliveries=[f'{qty} × {CROPS[crop]["name"]} → {reward}c (ends hour {end})' for cycle,(crop,qty,reward,end) in self.test_deliveries.items() if cycle not in self.delivered and cycle*6<=self.test_hour<end]
        if deliveries:embed.add_field(name='🚚 Active Test Deliveries',value='\n'.join(deliveries[-3:]),inline=False)
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
        elif self.actions >= PLOTS + self.extra_plot:
            msg = 'No planting actions left; advance to the next session.'
        elif free is None:
            msg = 'All plots are occupied. Harvest mature crops first.'
        elif self.coins < cfg['cost']:
            msg = 'Not enough test coins.'
        else:
            self.cared_plots.discard(free)
            self.pest_plots.discard(free)
            self.potion_plots.discard(free)
            self.potion_savings.pop(free,None)
            self.plots[free] = (crop, self.test_hour)
            self.coins -= cfg['cost']
            self.actions += 1
            self.planted += 1
            self.record_action('plant')
            msg = f'Planted {cfg["name"]} in plot {free}.'
        await self.refresh(interaction, msg)

    @discord.ui.button(label='Water Crops', emoji='💧', style=discord.ButtonStyle.secondary, row=1)
    async def care_test(self, interaction, button):
        growing = [(slot, crop) for slot, (crop, planted) in self.plots.items()
                   if slot not in self.cared_plots and CROPS[crop]['hours'] * 0.5 <= self.test_hour - planted < CROPS[crop]['hours'] * 1.25]
        if self.test_hour >= 168:
            msg = 'Test has ended. Reset to start again.'
        elif not growing:
            msg = 'No thirsty growing crops. Advance simulated time until crops reach halfway through growth.'
        else:
            self.cared_plots.update(slot for slot, crop in growing)
            msg = f'💧 Watered {len(growing)} thirsty crops. Normal growth schedule restored; otherwise growth takes 25% longer. No actions or coins spent.'
        await self.refresh(interaction, msg)

    @discord.ui.button(label='Harvest Ready', emoji='🧺', style=discord.ButtonStyle.success, row=1)
    async def harvest_test(self, interaction, button):
        ready = [(slot, crop) for slot, (crop, planted) in self.plots.items()
                 if self.test_crop_remaining(slot,crop,planted)<=0]
        for slot, crop in ready:
            self.inventory[crop] += 1
            del self.plots[slot]
            self.cared_plots.discard(slot)
            self.pest_plots.discard(slot)
            self.potion_plots.discard(slot)
            self.potion_savings.pop(slot,None)
        self.harvested += len(ready)
        if ready: self.record_action('harvest', len(ready))
        await self.refresh(interaction, f'Harvested {len(ready)} ready crop(s).')

    @discord.ui.button(label='Sell Produce', emoji='🪙', style=discord.ButtonStyle.primary, row=1)
    async def sell_test(self, interaction, button):
        earned = sum(int(qty * CROPS[k]['sale'] * (1 + self.market * 0.06) * (1+self.test_market_pct if k==self.test_market_crop else 1))
                     for k, qty in self.inventory.items())
        self.coins += earned
        self.sold += earned
        self.inventory = {crop: 0 for crop in CROPS}
        if earned: self.record_action('sell')
        await self.refresh(interaction, f'Sold stored produce for {earned:,} test coins.')

    @discord.ui.button(label='Advance 2 Hours', emoji='⏩', style=discord.ButtonStyle.secondary, row=1)
    async def next_test(self, interaction, button):
        if self.test_hour < 168:
            previous=self.test_hour
            self.test_hour=min(168,self.test_hour+2)
            self.session=self.test_hour//12
            if self.test_hour//6 != previous//6:
                before_pests=set(self.pest_plots)
                self.advance_economy(self.test_hour//6)
                if self.pest_plots-before_pests:
                    try:
                        await interaction.user.send(f'🧪🐛 **Harvest Hustle TEST — Pest Alert!** {len(self.pest_plots-before_pests)} crops need treatment. Open `/harvest test` and click **Remove Pests (Test)**. Untreated crops grow 15% slower.')
                    except (discord.Forbidden,discord.HTTPException):pass
            if self.test_hour//12>previous//12 and self.test_hour<168:
                self.actions=0
            newly_thirsty=[(slot,crop) for slot,(crop,planted) in self.plots.items()
                          if slot not in self.cared_plots and previous < planted + CROPS[crop]['hours'] * 0.5 <= self.test_hour]
            newly_ready=[crop for slot,(crop,planted) in self.plots.items()
                         if previous < planted + self.test_crop_hours(slot, crop) - self.potion_savings.get(slot,0) <= self.test_hour]
            ready_count=sum(1 for slot,(crop, planted) in self.plots.items() if self.test_crop_remaining(slot,crop,planted)<=0)
            if newly_thirsty:
                try:
                    names=', '.join(f'plot {slot}: {CROPS[crop]["name"]}' for slot,crop in newly_thirsty)
                    await interaction.user.send(
                        f'🧪💧 **Harvest Hustle TEST — Your crops need watering!**\n'
                        f'{len(newly_thirsty)} crop(s) are thirsty ({names}).\n'
                        'Return to your private `/harvest test` dashboard and click **💧 Water Crops**. '
                        'Unwatered crops take 25% longer to mature.')
                except (discord.Forbidden, discord.HTTPException):
                    logging.info('Test watering DM blocked for user %s', self.host_id)
            msg=(f'Advanced to hour {self.test_hour}/168. '
                 + (f'💧 **SIMULATED WATERING DM:** {len(newly_thirsty)} crop(s) need water! Click **Water Crops**. ' if newly_thirsty else '')
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

    @discord.ui.button(label='Claim Daily Missions', emoji='🎯', style=discord.ButtonStyle.success, row=4)
    async def claim_test_missions(self, interaction, button):
        if self.test_hour >= 168:
            await self.refresh(interaction, 'The seven-day test is over; daily missions have closed.')
            return
        day = self.test_hour // 24
        counts = self.daily_counts.get(day, {})
        claimed = self.daily_claimed.setdefault(day, set())
        rewards = {'plant': (2, 50), 'harvest': (2, 75), 'sell': (1, 100)}
        earned = 0
        for key, (target, reward) in rewards.items():
            if counts.get(key, 0) >= target and key not in claimed:
                claimed.add(key)
                earned += reward
                self.missions_completed += 1
        if all(key in claimed for key in rewards) and 'bonus' not in claimed:
            claimed.add('bonus')
            earned += 75
        self.coins += earned
        await self.refresh(interaction, f'Claimed +{earned}c in Day {day+1} mission rewards.' if earned else 'No unclaimed completed missions today. Plant 2, harvest 2, and sell once.')

    @discord.ui.button(label='Advance 12 Hours', emoji='📅', style=discord.ButtonStyle.secondary, row=4)
    async def next_session_test(self, interaction, button):
        if self.test_hour >= 168:
            await self.refresh(interaction, f'Test complete! Final score: {self.value():,}c. Unharvested crops have zero value.')
            return
        previous = self.test_hour
        self.test_hour = min(168, ((self.test_hour // 12) + 1) * 12)
        self.advance_economy(self.test_hour//6)
        self.session = self.test_hour // 12
        if self.test_hour < 168:
            self.actions = 0
        ready = sum(1 for slot,(crop,planted) in self.plots.items() if self.test_crop_remaining(slot,crop,planted)<=0)
        await self.refresh(interaction, f'Advanced from hour {previous} to {self.test_hour}. ' + (f'{ready} crops ready to harvest. ' if ready else '') + ('New planting session: actions refreshed.' if self.test_hour < 168 else f'🏆 TEST FINISHED — final farm value {self.value():,}c. Unharvested crops excluded.'))

    @discord.ui.button(label='Final Score / Stats', emoji='🏆', style=discord.ButtonStyle.secondary, row=4)
    async def stats_test(self, interaction, button):
        inventory_total = sum(int(qty * CROPS[k]['sale'] * (1 + self.market * 0.06)) for k, qty in self.inventory.items())
        resale = plot_resale_value(self.extra_plot) + self.market * UPGRADES['market']['resale']
        await interaction.response.send_message(embed=make_embed('🧪 Test Farm — Score & Statistics',
            f'**Final score if the game ended now: {self.value():,}c**\n'
            f'Coins: {self.coins:,}c\nHarvested inventory value: {inventory_total:,}c\nUpgrade resale: {resale:,}c\n'
            f'Growing crops: {len(self.plots)} (excluded from final score)\n\n'
            f'Planted: {self.planted} · Harvested: {self.harvested} · Lifetime sales: {self.sold:,}c\n'
            f'Daily missions completed: {self.missions_completed}\n'
            f'Plots: {PLOTS+self.extra_plot} · Actions per session: {PLOTS+self.extra_plot}\n'
            f'Simulated time: hour {self.test_hour}/168\n\n'
            'Official leaderboard tie-breakers: sales revenue, harvest count, earliest join, Discord ID.'), ephemeral=True)

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

    @discord.ui.button(label='Animals', emoji='🐾', style=discord.ButtonStyle.success, row=2)
    async def animals(self, interaction, button):
        await show_animals(interaction, self.uid)

    @discord.ui.button(label='Close', emoji='✖️', style=discord.ButtonStyle.secondary, row=3)
    async def close_test(self, interaction, button):
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label='Remove Pests (Test)',emoji='🐛',style=discord.ButtonStyle.secondary,row=2)
    async def pests_test(self,interaction,button):
        count=len(self.pest_plots)
        self.pest_plots.clear()
        await self.refresh(interaction,f'Removed pests from {count} test crops for free.')

    @discord.ui.button(label='Growth Potion (Test)',emoji='🧪',style=discord.ButtonStyle.secondary,row=3)
    async def potion_test(self,interaction,button):
        day=min(6,self.test_hour//24)
        eligible=[(slot,crop,planted) for slot,(crop,planted) in self.plots.items() if slot not in self.potion_plots and self.test_crop_remaining(slot,crop,planted)>=1][:5]
        if self.potions_by_day.get(day,0)>=2:msg='Already used 2 potions this test day.'
        elif self.coins<400:msg='Not enough coins (400c required).'
        elif not eligible:msg='No eligible crops with at least 1 hour remaining.'
        else:
            for slot,crop,planted in eligible:
                self.potion_savings[slot]=self.potion_savings.get(slot,0)+self.test_crop_remaining(slot,crop,planted)*.20
                self.potion_plots.add(slot)
            self.coins-=400
            self.potions_by_day[day]=self.potions_by_day.get(day,0)+1
            msg=f'Used Growth Potion on {len(eligible)} crops for 400c; 20% time reduction.'
        await self.refresh(interaction,msg)

    @discord.ui.button(label='Deliver Crops (Test)',emoji='🚚',style=discord.ButtonStyle.secondary,row=4)
    async def deliveries_test(self,interaction,button):
        total=0;completed=[]
        for cycle,(crop,qty,reward,end) in self.test_deliveries.items():
            if cycle in self.delivered or not cycle*6<=self.test_hour<end:continue
            if self.inventory[crop]>=qty:
                self.inventory[crop]-=qty
                self.delivered.add(cycle)
                total+=reward
                completed.append(cycle)
        self.coins+=total;self.sold+=total
        await self.refresh(interaction,f'Delivered {len(completed)} orders for {total:,} coins.' if completed else 'No delivery can be fulfilled. Save harvested inventory for the active orders.')

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
    await interaction.response.send_message('Play through all seven simulated days using **Advance 2 Hours** or **Advance 12 Hours**. Plant, wait for simulated watering alerts, water thirsty crops, harvest, sell, upgrade, claim daily missions, check final scoring and test DMs. This private sandbox does not change the official event or leaderboard.',
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
        if not watering_alert_watcher.is_running():
            watering_alert_watcher.start()
        if not economy_watcher.is_running():
            economy_watcher.start()
        if not animal_alert_watcher.is_running():
            animal_alert_watcher.start()

        await bot.connect(reconnect=True)




# Surface command failures instead of leaving Discord showing a generic timeout.
@start_test.error
async def start_test_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logging.error('Harvest start-test failed', exc_info=(type(error), error, error.__traceback__))
    message = '❌ Could not start the test. Check the Railway deployment logs for “Harvest start-test failed”. Official farm data was not reset.'
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logging.exception('Unable to send start-test error notification')

if __name__ == "__main__":
    asyncio.run(main())
