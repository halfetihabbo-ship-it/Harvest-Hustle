import itertools,csv
from pathlib import Path
COST=[80,130,200,250,200]; GROW=[1,2,3,4,6]
# Per-session opportunity cost approx 120, with longer crops earning modest premiums.
SALE=[200,380,600,800,1000]
PLOT_COST=500; PLOT_RESALE=250; MARKET_COST=800; MARKET_RESALE=400; MARKET_BONUS=.06

def run(policy,plots=0,markets=0,upgrade_at=0):
    coins=2000; growing=[]; bought=False; sold=0; planted=0
    for w in range(14):
        mature=[c for t,c in growing if t<=w];growing=[(t,c) for t,c in growing if t>w]
        income=sum(int(SALE[c]*(1+MARKET_BONUS*(markets if bought else 0))) for c in mature)
        coins+=income;sold+=income
        if w==upgrade_at and not bought and coins>=plots*PLOT_COST+markets*MARKET_COST:
            coins-=plots*PLOT_COST+markets*MARKET_COST;bought=True
        for _ in range(min(10,8+(plots if bought else 0)-len(growing))):
            choices=[c for c in policy if w+GROW[c]<14 and coins>=COST[c]]
            if not choices:break
            c=choices[planted%len(choices)]
            coins-=COST[c];growing.append((w+GROW[c],c));planted+=1
    return coins+(plots*PLOT_RESALE+markets*MARKET_RESALE if bought else 0),sold,planted,bought
policies=[(i,) for i in range(5)]+list(itertools.combinations(range(5),2))+[(0,1,2),(1,2,3),(2,3,4),(0,2,4),(0,1,2,3,4)]
rows=[]
for policy in policies:
 for plots,markets in [(0,0),(1,0),(2,0),(4,0),(0,1),(0,2),(1,1),(2,1),(4,1),(2,2)]:
  for when in [0,2,4,6]:
   value,sold,planted,bought=run(policy,plots,markets,when)
   rows.append(dict(strategy='+'.join(['carrot','corn','apple','pumpkin','gourd'][i] for i in policy),extra_plots=plots,market_stalls=markets,upgrade_session=when,upgrade_bought=bought,final_value=value,sales=sold,planted=planted))
for i in range(5):
 baseline=run((i,))[0];best=max((r for r in rows if r['strategy']==['carrot','corn','apple','pumpkin','gourd'][i]),key=lambda r:r['final_value'])
 print(best['strategy'],baseline,best['final_value'],best['extra_plots'],best['market_stalls'],best['upgrade_session'])
print('top 10',[(r['strategy'],r['final_value'],r['extra_plots'],r['market_stalls']) for r in sorted(rows,key=lambda r:-r['final_value'])[:10]])
with open(Path(__file__).with_name('balance_v5_results.csv'),'w',newline='') as f:
 wr=csv.DictWriter(f,fieldnames=list(rows[0]));wr.writeheader();wr.writerows(rows)
