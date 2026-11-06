# Harvest Hustle deployment update
import os
import sqlite3
import asyncio
import time
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ.get('DISCORD_TOKEN')
GUILD_ID = int(os.environ.get('GUILD_ID', '0'))
HOST_IDS = {int(x.strip()) for x in os.environ.get('HOST_IDS', '1501587787294703657').split(',') if x.strip()}
CHANNEL_ID = int(os.environ.get('EVENT_CHANNEL_ID', '1567980502965551144'))
ROLE_ID = int(os.environ.get('EVENT_ROLE_ID', '1567982021106602065'))
DB_PATH = os.environ.get('DATABASE_PATH', 'harvest.sqlite3')
WINDOW = 12 * 3600
DURATION = 14 * WINDOW
REGISTRATION_HOURS = int(os.environ.get("REGISTRATION_HOURS", "24"))
ACTIONS = 10
START_COINS = 2000
PLOTS = 8
CROPS = {
    'carrot': {'name':'🥕 Carrots', 'cost':80, 'sale':200, 'windows':1},
    'corn': {'name':'🌽 Corn', 'cost':130, 'sale':380, 'windows':2},
    'apple': {'name':'🍎 Apples', 'cost':200, 'sale':600, 'windows':3},
    'pumpkin': {'name':'🎃 Pumpkins', 'cost':250, 'sale':800, 'windows':4},
    'gourd': {'name':'🍂 Golden Gourds', 'cost':200, 'sale':1000, 'windows':6},
}
UPGRADES = {'extra_plot': {'name':'🌱 Extra Plot', 'cost':500, 'max':4, 'resale':250}, 'market': {'name':'🛒 Market Stall', 'cost':800, 'max':2, 'resale':400}}
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
add_column('event','last_session_notice','INTEGER NOT NULL DEFAULT -1')
add_column('event','midweek_sent','INTEGER NOT NULL DEFAULT 0')
add_column('event','final_sent','INTEGER NOT NULL DEFAULT 0')
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
con.commit()

def event():
    return con.execute('SELECT * FROM event WHERE id=1').fetchone()

def current_window(e):
    return max(0, min(13, (int(time.time()) - e['start_ts']) // WINDOW))

def registration_open(e):
    return e is not None and not e['ended'] and time.time() < e['start_ts'] and time.time() >= (e['registration_ts'] or 0)

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
    return p['coins'] + inventory_value(uid,p) + p['extra_plot'] * UPGRADES['extra_plot']['resale'] + p['market'] * UPGRADES['market']['resale']

def board(e):
    rows=[]
    for p in con.execute('SELECT * FROM players'):
        rows.append((farm_value(p['user_id'],p,e),p['sold'],p['harvested'],p['user_id']))
    # Tie rule: farm value, lifetime sales, lifetime harvests, then earliest join.
    rows.sort(key=lambda x:(-x[0],-x[1],-x[2],player(x[3])['joined_ts'],x[3]))
    return rows

def stamp(ts):
    return f'<t:{int(ts)}:F> (<t:{int(ts)}:R>)'

def make_embed(title, description='', color=0xC57B34):
    return discord.Embed(title=title,description=description,color=color)

def is_host(interaction):
    return interaction.user.id in HOST_IDS

class FarmView(discord.ui.View):
    def __init__(self, uid):
        super().__init__(timeout=300)
        self.uid=uid

    async def interaction_check(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message('This is someone else’s private farm menu. Use `/harvest` for yours.',ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Plant',emoji='🌱',style=discord.ButtonStyle.success,row=0)
    async def plant(self,interaction,button):
        await interaction.response.send_message('Choose a crop to plant (one action per seed):',view=CropView(self.uid),ephemeral=True)

    @discord.ui.button(label='Harvest Ready',emoji='🧺',style=discord.ButtonStyle.primary,row=0)
    async def harvest(self,interaction,button):
        await do_harvest(interaction,self.uid)

    @discord.ui.button(label='Sell Produce',emoji='🪙',style=discord.ButtonStyle.primary,row=0)
    async def sell(self,interaction,button):
        await do_sell(interaction,self.uid)

    @discord.ui.button(label='Upgrades',emoji='⬆️',style=discord.ButtonStyle.secondary,row=1)
    async def upgrades(self,interaction,button):
        await interaction.response.send_message('Purchase a permanent upgrade (no planting action required):',view=UpgradeView(self.uid),ephemeral=True)

    @discord.ui.button(label='Daily Objectives',emoji='🎯',style=discord.ButtonStyle.secondary,row=2)
    async def objectives(self,interaction,button):
        await show_objectives(interaction)

    @discord.ui.button(label='Refresh Farm',emoji='🔄',style=discord.ButtonStyle.secondary,row=1)
    async def refresh(self,interaction,button):
        await show_farm(interaction)

class CropSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='Select a crop',options=[discord.SelectOption(label=f"{v['name']} — {v['cost']}c",value=k,description=f"Ready in {v['windows']} session(s); sells for {v['sale']}c") for k,v in CROPS.items()])
    async def callback(self,interaction):
        uid=interaction.user.id
        crop=self.values[0]
        async with lock:
            e=event()
            if not active(e):
                msg='The event is not currently active.'
            elif not player(uid):
                msg='Join the event first with `/harvest join`.'
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
                    con.execute('INSERT INTO plots VALUES(?,?,?,?)',(uid,free,crop,current_window(e)))
                    con.execute('UPDATE players SET coins=coins-? WHERE user_id=?',(c['cost'],uid))
                    use_action(uid)
                    con.execute('UPDATE players SET planted=planted+1 WHERE user_id=?',(uid,))
                    log_action(uid,e,'plant',crop,-c['cost'])
                    msg=f"Planted {c['name']} in plot {free} for {c['cost']} coins. Ready at {stamp(e['start_ts']+(current_window(e)+c['windows'])*WINDOW)}, if the event is still running."
                con.commit()
        await interaction.response.send_message(msg,ephemeral=True)

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
    @discord.ui.button(label='Extra Plot · 500c',emoji='🌱',style=discord.ButtonStyle.success)
    async def plot(self,interaction,button):
        await buy_upgrade(interaction,'extra_plot')
    @discord.ui.button(label='Market Stall · 800c',emoji='🛒',style=discord.ButtonStyle.primary)
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
            if p[kind]>=cfg['max']: msg='You have reached the maximum level for this upgrade.'
            elif p['coins']<cfg['cost']: msg='Not enough coins.'
            else:
                con.execute(f'UPDATE players SET {kind}={kind}+1,coins=coins-? WHERE user_id=?',(cfg['cost'],uid))
                con.execute('UPDATE players SET upgrades_bought=upgrades_bought+1 WHERE user_id=?',(uid,))
                log_action(uid,e,'upgrade',kind,-cfg['cost'])
                msg=f"Purchased {cfg['name']} for {cfg['cost']} coins!" + (f' Plots Owned: {available_plots(player(uid))}/{PLOTS + UPGRADES["extra_plot"]["max"]}.' if kind=='extra_plot' else ' Your stored produce sells for 6% more per level.')
            con.commit()
    await interaction.response.send_message(msg,ephemeral=True)

async def do_harvest(interaction,uid):
    async with lock:
        e=event()
        if not active(e) or not player(uid): msg='The event is not active or you have not joined.'
        else:
            sync_actions(uid,e)
            p=player(uid)
            w=current_window(e)
            ready=[r for r in con.execute('SELECT * FROM plots WHERE user_id=?',(uid,)) if w-r['planted_window']>=CROPS[r['crop']]['windows']]
            if not ready: msg='No crops are ready yet. Crops mature by farming session, not by repeated checking.'
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
    await interaction.response.send_message(msg,ephemeral=True)

async def do_sell(interaction,uid):
    async with lock:
        e=event()
        if not active(e) or not player(uid): msg='The event is not active or you have not joined.'
        else:
            sync_actions(uid,e)
            p=player(uid)
            items=list(con.execute('SELECT * FROM inventory WHERE user_id=? AND qty>0',(uid,)))
            if not items: msg='No harvested produce in your inventory.'
            else:
                earned=inventory_value(uid,p)
                con.execute('DELETE FROM inventory WHERE user_id=?',(uid,))
                con.execute('UPDATE players SET coins=coins+?,sold=sold+? WHERE user_id=?',(earned,earned,uid))
                log_action(uid,e,'sell',f'{sum(x["qty"] for x in items)} crops',earned)
                msg=f'🪙 Sold {sum(x["qty"] for x in items)} crop(s) for {earned} coins (free).'
            con.commit()
    await interaction.response.send_message(msg,ephemeral=True)

async def show_farm(interaction):
    uid=interaction.user.id
    async with lock:
        e=event()
        if not e:
            msg='No Harvest Hustle event has been scheduled. A host must use `/harvest start`.'
            await interaction.response.send_message(msg,ephemeral=True)
            return
        if not player(uid):
            await interaction.response.send_message('Join first with `/harvest join`.',ephemeral=True)
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
                remaining=max(0,c['windows']-(w-r['planted_window']))
                lines.append(f'**{slot}.** {c["name"]} — '+('✅ Ready to harvest' if remaining==0 else f'{remaining} session(s) remaining'))
        inv=', '.join(f"{CROPS[r['crop']]['name']} ×{r['qty']}" for r in con.execute('SELECT * FROM inventory WHERE user_id=? AND qty>0',(uid,))) or 'Empty'
        embed=make_embed('🍂 Your Harvest Hustle Farm',f'**Day {w//2+1} / 7 · Session {w%2+1} / 2**\n**Ends:** {stamp(e["start_ts"]+DURATION)}')
        embed.add_field(name='💰 Coins',value=str(p['coins']))
        embed.add_field(name='🌱 Planting actions left',value=f'{max(0,ACTIONS-p["actions_used"])}/{ACTIONS}')
        embed.add_field(name='🏆 Farm value',value=f'{farm_value(uid,p,e):,}')
        next_reset=e['start_ts']+(w+1)*WINDOW
        if active(e): embed.add_field(name='⏳ Next planting reset',value=stamp(next_reset),inline=False)
        milestone=max((n for n in (5,15,30,50) if p['harvested']>=n),default=0)
        embed.add_field(name='🏅 Harvest milestone',value=f'{milestone} crops' if milestone else 'Not yet unlocked',inline=True)
        embed.add_field(name='🌱 Plots Owned',value=f'{available_plots(p)}/{PLOTS + UPGRADES["extra_plot"]["max"]} · {len(plots)} occupied · {available_plots(p)-len(plots)} empty',inline=False)
        embed.add_field(name='🌾 Plot Details',value='\n'.join(lines),inline=False)
        embed.add_field(name='🧺 Inventory',value=inv,inline=False)
        embed.add_field(name='📊 Career stats',value=f'Planted: {p["planted"]} · Harvested: {p["harvested"]} · Lifetime sales: {p["sold"]:,}c',inline=False)
        embed.add_field(name='⬆️ Upgrades',value=f'Extra plots: {p["extra_plot"]}/4 · Market stalls: {p["market"]}/2',inline=False)
        embed.set_footer(text='Only planting costs actions. 10 planting actions refresh every 12 hours; harvesting, selling and upgrades are free actions.')
    await interaction.response.send_message(embed=embed,view=FarmView(uid) if active(e) else None,ephemeral=True)

# Role panel is a permanent Discord button, restored after bot restarts.
# Its message ID is saved so a host can update the existing panel rather than spam duplicates.
con.execute('CREATE TABLE IF NOT EXISTS role_panel(id INTEGER PRIMARY KEY CHECK(id=1), message_id INTEGER NOT NULL)')
con.commit()

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
                await interaction.response.send_message('🍂 You have left the Harvest Hustle notification role. You can rejoin anytime.', ephemeral=True)
            else:
                await member.add_roles(role, reason='Harvest Hustle self-service role panel')
                await interaction.response.send_message('🌾 You now have the Harvest Hustle role! You will receive event announcement pings. Register for the game separately using `/harvest join`.', ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message('I cannot manage that role. Please give me **Manage Roles** and move my highest role **above** the Harvest Hustle role.', ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message('Discord could not update your role. Please try again.', ephemeral=True)


def role_panel_embed():
    embed = discord.Embed(
        title='🍁 HARVEST HUSTLE | A COSY AUTUMN GAME',
        description=(
            'Autumn is here, so settle in for a little farming fun! 🍂\n\n'
            'Grow some crops, build up your farm and see how your harvest turns out '
            'over seven relaxed days. Pop in when you have time, try different crops '
            'and enjoy some friendly competition along the way. 🌾\n\n'
            '🌱 **Your starting farm:** 8 plots and 2,000 in-game coins\n'
            '🕰️ **Farming sessions:** Every 12 hours for 7 days\n'
            '🏡 **Make it your own:** Plant, harvest, sell and upgrade your farm\n\n'
            '**🏆 A LITTLE SOMETHING FOR THE TOP THREE**\n'
            '🥇 **1st:** 150c + HC Box\n'
            '🥈 **2nd:** 100c + HC Box\n'
            '🥉 **3rd:** 75c + HC Box\n\n'
            '**Want to join the autumn fun?** Tap the button below to get the '
            'Harvest Hustle notification role and hear about session updates. '
            'Tap it again anytime to leave.\n\n'
            '🍂 **Note:** The role is for notifications. When registration opens, '
            'use `/harvest join` to enter the game.'
        ),
        color=discord.Color.from_rgb(160, 104, 49)
    )
    embed.set_footer(text='A cosy week of crops, coins and friendly competition 🍁')
    return embed

intents=discord.Intents.default()
bot=commands.Bot(command_prefix='!',intents=intents)
harvest=app_commands.Group(name='harvest',description='Harvest Hustle farming competition')

@harvest.command(name='role-panel', description='Host only: post or refresh the permanent Harvest Hustle role panel')
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
    channel = interaction.guild.get_channel(CHANNEL_ID) or bot.get_channel(CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message('The configured event channel is unavailable or is not a text channel.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        row = con.execute('SELECT message_id FROM role_panel WHERE id=1').fetchone()
        if row:
            try:
                message = await channel.fetch_message(row['message_id'])
                await message.edit(embed=role_panel_embed(), view=HarvestRolePanel())
                await interaction.followup.send(f'🍂 Existing role panel refreshed in {channel.mention}.', ephemeral=True)
                return
            except discord.NotFound:
                pass
        message = await channel.send(embed=role_panel_embed(), view=HarvestRolePanel())
        con.execute('INSERT INTO role_panel(id,message_id) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET message_id=excluded.message_id', (message.id,))
        con.commit()
        await interaction.followup.send(f'🍂 Role panel posted in {channel.mention}.', ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send('I need View Channel, Send Messages and Embed Links in the event channel.', ephemeral=True)
    except discord.HTTPException:
        await interaction.followup.send('Discord could not post or update the role panel. Please retry.', ephemeral=True)

@harvest.command(name='start',description='Host only: open registration and schedule a seven-day championship')
async def start(interaction:discord.Interaction):
    if not is_host(interaction):
        await interaction.response.send_message('Host only.',ephemeral=True); return
    async with lock:
        e=event()
        if e:
            await interaction.response.send_message('An event already exists. Use `/harvest reset` only after the old event ends.',ephemeral=True); return
        now=int(time.time())
        ts=now+REGISTRATION_HOURS*3600
        con.execute('INSERT INTO event(id,start_ts,ended,registration_ts) VALUES(1,?,0,?)',(ts,now))
        con.commit()
    msg=f'🍂 **HARVEST HUSTLE REGISTRATION IS OPEN!**\nRegister with `/harvest join` now.\n**Farming begins:** {stamp(ts)}\n**Ends:** {stamp(ts+DURATION)}\nSeven days · two sessions daily · 10 planting actions per session.\nEveryone begins with 2,000 coins and 8 plots.'
    await interaction.response.send_message(f'🍂 Registration opened. Farming starts {stamp(ts)}. Announcement queued.',ephemeral=True)
    await send_announcement(msg,ping=True)

@harvest.command(name='join',description='Join Harvest Hustle')
async def join(interaction:discord.Interaction):
    async with lock:
        e=event()
        if not registration_open(e): msg='Registration is closed. Join during the pre-event registration period.'
        elif player(interaction.user.id): msg='You are already registered. Use `/harvest farm`.'
        else:
            con.execute('INSERT INTO players(user_id,joined_ts,action_window) VALUES(?,?,?)',(interaction.user.id,int(time.time()),current_window(e)))
            con.commit()
            msg=f'🍁 You are registered! Farming starts {stamp(e["start_ts"])}. You will begin with 2,000 coins, eight plots and 10 planting actions per session.'
    await interaction.response.send_message(msg,ephemeral=True)

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
        embed=make_embed('🏆 Harvest Hustle Leaderboard',desc)
        embed.set_footer(text='Ties: farm value → total sales → harvest count → earlier registration → Discord ID.')
    await interaction.response.send_message(embed=embed)

@harvest.command(name='rules',description='Learn the game rules')
async def rules(interaction:discord.Interaction):
    crops='\n'.join(f"{v['name']}: seed {v['cost']}c → sells {v['sale']}c · grows {v['windows']} sessions" for v in CROPS.values())
    e=make_embed('📖 Harvest Hustle Rules',
        '**Registration opens 24 hours before farming begins. No late joins.** Seven days, fourteen 12-hour sessions. You get **10 planting actions per session**; unused actions do not roll over. Each player starts with **2,000 coins and 8 plots**.\n\n'
        '**Plant**: one planting action and the seed cost per plot. **Harvest**: free, collects ALL ready crops. **Sell**: free, sells ALL stored produce. **Upgrade**: free action (coins required).\n\n'
        '**Crops**\n'+crops+'\n\n'
        '**Upgrades**\n🌱 Extra Plot: 500c, up to 4, resale value 250c each.\n🛒 Market Stall: 800c, up to 2, +6% sale price per level, resale value 400c each.\n\n'
        '**Winner**: coins + stored produce sale value + upgrade resale value. **Growing crops are worth 0 at closing.** Tie order: lifetime sales, total crops harvested, earlier join, then Discord user ID.\n\n'
        'Harvest milestones at 5, 15, 30 and 50 crops are recognition-only. Daily objectives award cosmetic badges only, never coins. No random crop failures, market price swings or speed bonuses. Registration closes at the start. All progress is stored in SQLite.')
    await interaction.response.send_message(embed=e,ephemeral=True)

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
            for table in ('transactions','objective_claims','inventory','plots','players','event'):
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

# Each day has two farming sessions. Objectives are achievement-only to avoid
# creating an incentive to spam crops or exploit daily timing.
OBJECTIVES = (
    ('first_seed','Plant at least one crop today'),
    ('harvest','Harvest at least one crop today'),
    ('trade','Sell produce today'),
)

async def show_objectives(interaction):
    uid=interaction.user.id
    async with lock:
        e=event()
        if not active(e) or not player(uid):
            await interaction.response.send_message('Objectives are available while you are farming.',ephemeral=True);return
        day=current_window(e)//2
        logs=list(con.execute('SELECT action FROM transactions WHERE user_id=? AND session BETWEEN ? AND ?', (uid,day*2,day*2+1)))
        acts={r['action'] for r in logs}
        achieved=[]
        for key,label in OBJECTIVES:
            target={'first_seed':'plant','harvest':'harvest','trade':'sell'}[key]
            if target in acts:
                con.execute('INSERT OR IGNORE INTO objective_claims VALUES(?,?,?)',(uid,day,key))
                achieved.append(f'✅ {label}')
            else: achieved.append(f'⬜ {label}')
        con.execute('UPDATE players SET objectives_claimed=(SELECT COUNT(*) FROM objective_claims WHERE user_id=?) WHERE user_id=?',(uid,uid))
        total=con.execute('SELECT COUNT(*) FROM objective_claims WHERE user_id=?',(uid,)).fetchone()[0]
        con.commit()
    await interaction.response.send_message(embed=make_embed(f'🎯 Day {day+1} Objectives','\n'.join(achieved)+f'\n\n🏅 **Achievement stamps earned:** {total}\nCosmetic recognition only; no bonus coins.'),ephemeral=True)

@harvest.command(name='objectives',description='View daily farming achievements')
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
                f'**Upgrades purchased:** {p["upgrades_bought"]}\n**Daily objective stamps:** {p["objectives_claimed"]}')
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
        if e['ended'] and e['final_sent']:return
        now=int(time.time())
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
                     'Use `/harvest objectives` for today’s achievements.')
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
@event_watcher.before_loop
async def before_watcher():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    logging.info('Logged in as %s',bot.user)

async def main():
    async with bot:
        await bot.login(TOKEN)

        guild = discord.Object(id=GUILD_ID)

        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)

        print(f"Successfully synced {len(synced)} slash commands.")

        await bot.connect(reconnect=True)


if __name__ == "__main__":
    asyncio.run(main())

asyncio.run(main())
if __name__=='__main__':
    asyncio.run(main())
