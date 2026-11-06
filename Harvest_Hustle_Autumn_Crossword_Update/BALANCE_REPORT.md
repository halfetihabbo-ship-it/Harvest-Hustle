# Harvest Hustle — balance simulation

## Method

Deterministic simulation of 14 twelve-hour sessions. Each simulated player begins with 500 coins, six plots and six actions per session. The simulator harvests all mature crops for one action, periodically sells produce for one action, plants crops individually, and optionally purchases up to two market upgrades and four plot upgrades. It compares six planting policies (five crop-focused and a profit-rate heuristic) against 36 upgrade-timing combinations, for 216 policy scenarios. These are **representative policies, not an exhaustive search of every legal action sequence**. No player behaviour or live Discord interactions were simulated.

## Before / after: crop-focused policies without upgrades

| Crop focus | Original final farm value | Balanced final farm value |
|---|---:|---:|
| Carrot | 1,400 | 2,300 |
| Corn | 1,820 | 2,240 |
| Apple | 2,120 | 2,300 |
| Pumpkin | 2,610 | 2,295 |
| Gourd | 2,605 | 2,315 |

The original crop-focused policies ranged from 1,400 to 2,610 (86% gap relative to the lowest). After the change they range from 2,240 to 2,315 (3.3% gap relative to the lowest). These are **not maximum achievable scores**.

## Revised crop economy

| Crop | Seed | Sale | Growth sessions |
|---|---:|---:|---:|
| Carrot | 20 | 50 | 1 |
| Corn | 40 | 85 | 2 |
| Apple | 70 | 140 | 3 |
| Pumpkin | 110 | 205 | 4 |
| Gourd | 180 | 305 | 6 |

Extra Plot: 250 coins, 125 final resale value, max 4. Market Stall: 425 coins, 212 final resale value, max 2; each level adds 10% to sale value.

## Best sampled strategies

| Rank | Strategy | Market upgrade first available | Extra plot first available | Final value |
|---|---|---|---|---:|
| 1 | pumpkin | 0 | never | 2,488 |
| 2 | gourd | 2 | never | 2,357 |
| 3 | gourd | 4 | never | 2,357 |
| 4 | gourd | 6 | never | 2,357 |
| 5 | pumpkin | 2 | never | 2,337 |
| 6 | pumpkin | 4 | never | 2,337 |
| 7 | apple | 4 | never | 2,322 |
| 8 | apple | 2 | never | 2,318 |
| 9 | gourd | never | never | 2,315 |
| 10 | carrot | never | never | 2,300 |

## Important limitations

- Upgrade strategies remain uneven; some market-upgrade schedules outperform crop-only strategies. This is a meaningful strategic reward but is **not proof of perfect fairness**.
- The simulator uses simple planting and selling heuristics; clever mixed-crop sequences may score higher.
- It assumes players use each available session; missed sessions and late joiners are disadvantaged by the current rules.
- Final growing crops score zero; players must plan their last planting.
- The game remains a deterministic optimisation competition. Players could share an optimal strategy.
- Do not modify crop economics after the competition starts. Run a test with a fresh database and consider publishing the entire ruleset before launch.
