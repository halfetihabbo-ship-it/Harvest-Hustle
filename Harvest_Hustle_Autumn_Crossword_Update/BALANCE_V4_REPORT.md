# Harvest Hustle v4 — Economy balancing report

**Rules:** 7 days, 14 twelve-hour sessions, 10 planting actions per session, 8 starting plots, 2,000 starting coins, free harvesting and selling. At the end, immature crops have no value. The code is updated to match the prices below.

| Crop | Seed | Sale | Growth sessions | Profit |
|---|---:|---:|---:|---:|
| Carrot | 80 | 200 | 1 | 120 |
| Corn | 130 | 380 | 2 | 250 |
| Apple | 200 | 600 | 3 | 400 |
| Pumpkin | 250 | 800 | 4 | 550 |
| Golden Gourd | 200 | 1,000 | 6 | 800 |

**Upgrades:** Extra Plot 650 coins (325 resale), max 4; Market Stall 1,200 coins (600 resale), max 2, adds 6% selling value per level. All prices and bonuses are fixed and published in the bot.

## Simulation methodology

The included `balance_v4_sim.py` simulates 14 sessions with deterministic growth, buying upgrades at sessions 0/2/4/6, planting as much as allowed, harvesting mature crops and selling immediately. It compares five single-crop strategies and fifteen mixed-crop rotations, with ten upgrade configurations: **800 scenario runs**. These are representative fixed policies, not an exhaustive search or proof of fairness. It does not model missed sessions, mid-session trading, or adaptive planning.

## Results

No-upgrade single-crop final values: carrots **14,480**, corn **14,000**, apples **14,800**, pumpkins **15,200**, gourds **14,800**. Spread between highest and lowest is **8.6% of the lowest**.

Best sampled mixed-crop strategy: carrot+apple, 4 extra plots and 1 market stall, final farm value **19,484**. Carrot+pumpkin with 4 plots and no market stall returned **19,180**. Both market and plot upgrades can be worthwhile in some strategies, but not all strategies benefit equally.

## Limitations and launch advice

Long crops have fewer opportunities to mature before the deadline, so final-day planting is crucial. A market upgrade purchased after inventory accumulates can change its valuation. A full optimizer and a live Discord integration test are still recommended before a real-prize event. Start a fresh event/database; do not modify a competition already in progress.
