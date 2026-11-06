# Higher-reward economy — November 2026

All currency values were multiplied by exactly **4×**, including starting coins, seed prices, sale prices, upgrade costs and upgrade resale values. This makes farming feel more rewarding **without changing the mathematical profitability or relative balance** of the previous version. The earlier illustrative suggestion of increasing only sale prices would have strongly favoured short-duration crops and was **not** used.

| Crop | Seed | Sale | Net profit | Growth |
|---|---:|---:|---:|---|
| Carrots | 80 | 200 | 120 | 12h |
| Corn | 160 | 340 | 180 | 24h |
| Apples | 280 | 560 | 280 | 36h |
| Pumpkins | 440 | 820 | 380 | 48h |
| Golden Gourds | 720 | 1,220 | 500 | 72h |

Starting funds: **2,000 coins**. Extra plot: **1,000**, resale **500**. Market stall: **1,700**, resale **848**.

The same 216 sampled policies were rerun under the 4× scaled rules (see `reward_simulation.py`).

new_economy no upgrades: [('carrot', 9200), ('corn', 8960), ('apple', 9200), ('pumpkin', 9180), ('gourd', 9260)]
new_economy best sampled: [(9960, 'pumpkin', 0, None), (9430, 'gourd', 2, None), (9430, 'gourd', 4, None), (9430, 'gourd', 6, None), (9356, 'pumpkin', 2, None)]

The five no-upgrade strategies finish between **8,960 and 9,260 coins**. Highest sampled upgrade strategy: **9,960 coins**. These figures are simulated, not a guarantee of maximum achievable value.

Added non-economic harvest milestone badges (5/15/30/50 harvested crops) and exact next-action-reset display. Milestones have **no coin rewards**, avoiding an incentive that disproportionately favours fast crops.

**Existing games:** Start this version on a fresh database. Do not replace the economy during a live event. SQLite data from an older version is not automatically converted to the new coin scale.
