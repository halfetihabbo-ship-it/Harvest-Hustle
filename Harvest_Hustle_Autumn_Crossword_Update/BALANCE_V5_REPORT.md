# Harvest Hustle v5 — Upgrade-price simulation

## Changes
- Extra Plot: 500 coins per purchase, max 4, final scoring credit 250 per purchase.
- Market Stall: 800 coins per purchase, max 2, +6% sale value per level, final scoring credit 400 per purchase.
- Starting farm: 8 plots; max 12. 10 planting actions per 12-hour session; 14 sessions; starting coins 2,000.
- Crop prices and growth times unchanged from v4.

## Simulation
800 deterministic scenarios (20 crop rotations × 10 upgrade bundles × 4 purchase sessions), with harvest/sell before upgrades and planting, growth-time restrictions, cash constraints, and final scoring credits. Buying an upgrade bundle requires sufficient cash for the whole bundle. This is **not** an exhaustive optimization and does not model missed sessions.

| Scenario (best sampled crop rotation) | v4 value | v5 value |
|---|---:|---:|
| No upgrades (carrot+pumpkin) | 15,740 | 15,740 |
| Four plots, no stall (carrot+pumpkin) | 19,180 | 19,480 |
| One stall, no plots (carrot+pumpkin) | 16,100 | 16,518 |
| Two stalls, no plots (carrot+pumpkin) | 16,460 | 16,860 |
| Four plots + one stall (v4 carrot+apple; v5 carrot+pumpkin) | 19,484 | **20,472** |
| Two plots + two stalls (v4 carrot+apple; v5 carrot+pumpkin) | 18,126 | 19,162 |

Top sampled v5 strategy: carrot+pumpkin, four extra plots and one Market Stall purchased at session 4; final value **20,472 coins**. Second highest: carrot+apple, four plots and one stall, **19,984 coins**.

## Break-even and implications
One Market Stall costs 800 coins, and its 400-coin final scoring credit reduces effective cost to 400. Its 6% sale bonus therefore recovers its effective final-value cost after roughly **6,667 coins in future base-price sales**. This is not the same as recovering 800 coins in liquid cash: **13,334 coins** in future base-price sales are needed to recover the full purchase price from bonus revenue alone. A 500-coin Extra Plot retains 250 coins in final scoring; it needs to generate at least 250 additional coins in net crop profit to increase final value, and is useful only when growing space constrains planting.

## Recommendation
The revised prices make upgrades more attainable. Both can improve final scores, but extra plots have stronger leverage in sampled mixed strategies. A second stall is not automatically optimal. This is a reasonable candidate for a small test event, **not proof of perfect balance**. Test varied login patterns, optimize adaptive strategies, and run a fresh database for the live competition.
