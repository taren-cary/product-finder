"""Checks the metric and score math with made-up data whose answers are known.

Run from the project folder:  .venv/Scripts/python tests/test_metrics.py
Note: the expected numbers assume the default weights in config.yaml. If you
change the scoring weights, the score checks at the bottom will fail; that's
expected.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import settings
from features.run import (amazon_velocity, crowding_parts, google_metrics, growth, latest_growth,
                          margin_factor, mention_velocity, percentile, profit_per_unit,
                          relative_saturation,
                          saturation, shop_stats, sustained_from_series, video_stats,
                          weekly_growth, weighted_velocity)
from scoring.run import opportunity

F, S = settings["features"], settings["scoring"]

def trends(values):
    return {"interestOverTime": [{"value": v, "isPartial": False} for v in values]}

# Steady climb: flat 20 for 44 weeks, then rising 8 weeks
steady = google_metrics(trends([20] * 44 + [24, 26, 28, 30, 32, 34, 36, 38]), F)
assert abs(steady["velocity"] - ((32 + 34 + 36 + 38) / 4 / ((24 + 26 + 28 + 30) / 4) - 1)) < 1e-9
assert steady["sustained_weeks"] == 8 and steady["spike_risk"] is False, steady

# One-week spike: flat 20, one week at 60, back to 20
spike = google_metrics(trends([20] * 50 + [60, 20]), F)
assert spike["spike_risk"] is True and spike["sustained_weeks"] == 1, spike

# Tiny numbers are ignored (noise)
tiny = google_metrics(trends([1] * 48 + [2, 3, 3, 4]), F)
assert tiny["velocity"] is None

# Too little history
assert google_metrics(trends([10] * 10), F)["velocity"] is None

assert growth(150, 100) == 0.5 and growth(5, 0) is None and growth(None, 3) is None
assert amazon_velocity([(10, 20), (5, None)], 0.5) == 0.5          # (+0.5 + 0.5) / 2
assert amazon_velocity([], 0.5) is None
assert mention_velocity(1, 0, 2) is None and mention_velocity(4, 2, 2) == 1.0

shop = shop_stats([
    {"product_id": "a", "seller_id": "s1", "revenue": 600, "revenue_growth_rate": 50},
    {"product_id": "b", "seller_id": "s1", "revenue": 200, "revenue_growth_rate": 0},
    {"product_id": "c", "seller_id": "s2", "revenue": 100, "revenue_growth_rate": -100},
    {"product_id": "d", "seller_id": "s3", "revenue": 60},
    {"product_id": "e", "seller_id": "s4", "revenue": 40},
])
assert shop["sellers"] == 4 and shop["revenue"] == 1000
assert shop["top3_share"] == 0.96                                     # (800 + 100 + 60) / 1000
assert abs(shop["kalodata_growth"] - (50 * 600 + 0 - 100 * 100) / 900 / 100) < 1e-9

vids = video_stats([{"belonged_creator_id": "x", "views": 300, "ad_view_ratio": 100},
                    {"belonged_creator_id": "y", "views": 100, "ad_view_ratio": 0}])
assert vids["creators"] == 2 and vids["paid_share"] == 0.75

empty = saturation({"sellers": 0, "creators": 0, "revenue": 0, "top3_share": None, "hashtag_views": 0}, F)
crowded = saturation({"sellers": 100, "creators": 100, "revenue": 1e6, "top3_share": 1.0, "hashtag_views": 1e10}, F)
assert empty == 0.0 and crowded == 1.0, (empty, crowded)
assert saturation({}, F) is None
assert weighted_velocity({"google": 0.5, "amazon": None, "reddit": 5.0},
                         {"google": 1.0, "amazon": 1.0, "reddit": 0.5}, 2.0) == (0.5 + 0.5 * 2.0) / 1.5

# Growth between checks, converted to a weekly rate
from datetime import date
assert abs(weekly_growth(121, date(2026, 10, 12), 100, date(2026, 9, 28)) - 0.10) < 1e-9   # +21% over 2 weeks
assert weekly_growth(120, date(2026, 10, 1), 100, date(2026, 9, 28)) is None               # checks too close together
assert abs(latest_growth([(date(2026, 9, 14), 100), (date(2026, 9, 21), 90), (date(2026, 9, 28), 110)]) - (110 / 90 - 1)) < 1e-9
assert latest_growth([(date(2026, 9, 28), 100)]) is None
assert sustained_from_series([10, 12, 15, 20]) == 1.0 and sustained_from_series([10, 9, 8]) == 0.5
assert sustained_from_series([10, 12]) is None

# Active sellers, revenue per active seller, new products' share
from datetime import date as _d
stats = shop_stats([
    {"product_id": "a", "seller_id": "s1", "revenue": 5000, "launch_date": "2026-09-01"},   # new, active
    {"product_id": "b", "seller_id": "s2", "revenue": 1000, "launch_date": "2025-01-01"},   # old, active
    {"product_id": "c", "seller_id": "s3", "revenue": 100, "launch_date": "2026-09-10"},    # new, not active
], checked_on=_d(2026, 9, 24), active_min_revenue=300, new_product_days=60)
assert stats["sellers"] == 3 and stats["active_sellers"] == 2
assert stats["revenue_per_active_seller"] == 3000.0
assert abs(stats["new_product_share"] - 5100 / 6100) < 0.001 and stats["maxed"] is False

# Percentile ranking and relative saturation
assert percentile(5, [1, 5, 9]) == 0.5 and percentile(1, [1, 5, 9]) == 1 / 6
W = F["crowding_weights"]
quiet = {"active_sellers": 3, "creators": 5, "top3_share": 0.9, "hashtag_views": 1e5,
         "revenue_per_active_seller": 9000, "new_product_share": 0.8}
busy = {"active_sellers": 90, "creators": 95, "top3_share": 0.3, "hashtag_views": 5e9,
        "revenue_per_active_seller": 500, "new_product_share": 0.05}
middle = {k: (quiet[k] + busy[k]) / 2 for k in quiet}
sats = relative_saturation({"quiet": quiet, "busy": busy, "middle": middle,
                            "unchecked": {k: None for k in quiet}}, W)
assert sats["unchecked"] is None
assert sats["quiet"] < sats["middle"] < sats["busy"], sats
assert sats["quiet"] < 0.35 and sats["busy"] > 0.65, sats      # spread out, not squeezed together

# Price and profit
P = settings["pricing"]
assert profit_per_unit(12.99, P) == round(12.99 * (1 - 0.08 - 0.15 - 0.25) - 4, 2)   # ~ $2.75
assert profit_per_unit(2.99, P) < 0 and profit_per_unit(None, P) is None
assert margin_factor(-1.0, P) == 0.1 and margin_factor(3.0, P) == 0.1
assert margin_factor(10.0, P) == 1.0 and margin_factor(None, P) == 1.0
assert 0.1 < margin_factor(6.5, P) < 1.0
assert margin_factor(20.0, P) == 1.25 and margin_factor(100.0, P) == 1.25
priced = shop_stats([{"product_id": "a", "seller_id": "s1", "revenue": 3000, "sales_volumn": 300, "unit_price": 9.99},
                     {"product_id": "b", "seller_id": "s2", "revenue": 900, "sales_volumn": 300, "unit_price": 2.99}],
                    active_min_revenue=300)
assert priced["typical_price"] == 6.5 and priced["price_floor"] == 2.99 and priced["units"] == 600

# TikTok-first scores (growth counted on a log scale: ln(1 + g))
from math import log1p as L


def row(**kw):
    base = {f"velocity_{s}": None for s in ("tiktokshop", "shopvideos", "tiktok", "google", "amazon", "reddit")}
    base.update({"sustained_factor": 1.0, "spike_risk": False, "margin_factor": 1.0, "tiktok_saturation": 0.0})
    base.update(kw)
    return base


tiktok_only, _ = opportunity(row(velocity_tiktokshop=0.4, velocity_shopvideos=0.2), S)
assert abs(tiktok_only - 100 * (L(0.4) + 0.5 * L(0.2))) < 0.01, tiktok_only

# Confirmations add a little on their own and multiply the score
confirmed, b = opportunity(row(velocity_tiktokshop=0.4, velocity_shopvideos=0.2,
                               velocity_google=0.3, velocity_amazon=0.2), S)
expected = 100 * (L(0.4) + 0.5 * L(0.2) + 0.25 * L(0.3) + 0.25 * L(0.2)) * (1 + 0.15 * 2)
assert abs(confirmed - expected) < 0.01 and b["confirmations"] == 2, (confirmed, expected)

# The same outside demand without TikTok momentum scores far lower (arbitrage candidate)
outside_only, _ = opportunity(row(velocity_google=0.3, velocity_amazon=0.2), S)
assert 0 < outside_only < tiktok_only / 2, (outside_only, tiktok_only)

# Bigger growth ranks higher, but a 10x bigger number isn't 10x the score
g3, _ = opportunity(row(velocity_tiktokshop=3.0), S)
g30, _ = opportunity(row(velocity_tiktokshop=30.0), S)
g300, _ = opportunity(row(velocity_tiktokshop=300.0), S)
assert g3 < g30 < 3 * g3 and g300 == g30, (g3, g30, g300)   # capped at +3,000%

# Headroom: a crowded TikTok Shop cuts the score to 1/5; unchecked counts as 0.7; a spike halves it
crowded, _ = opportunity(row(velocity_tiktokshop=0.4, velocity_shopvideos=0.2, tiktok_saturation=1.0), S)
unchecked, _ = opportunity(row(velocity_tiktokshop=0.4, velocity_shopvideos=0.2, tiktok_saturation=None), S)
open_shop, _ = opportunity(row(velocity_tiktokshop=0.4, velocity_shopvideos=0.2, tiktok_saturation=0.3), S)
spiky, _ = opportunity(row(velocity_tiktokshop=0.4, velocity_shopvideos=0.2, spike_risk=True), S)
assert abs(crowded - tiktok_only / 5) < 0.01 and abs(spiky - tiktok_only / 2) < 0.01
assert abs(unchecked - tiktok_only / (1 + 4 * 0.7)) < 0.01 and open_shop > unchecked

# Tiny markets are scaled down in proportion; big or unchecked ones aren't
tiny, b_tiny = opportunity(row(velocity_tiktokshop=0.4, velocity_shopvideos=0.2, shop_revenue_7d=500), S)
big, _ = opportunity(row(velocity_tiktokshop=0.4, velocity_shopvideos=0.2, shop_revenue_7d=80000), S)
unknown_size, _ = opportunity(row(velocity_tiktokshop=0.4, velocity_shopvideos=0.2, shop_revenue_7d=None), S)
assert abs(tiny - tiktok_only * 500 / 5000) < 0.01 and b_tiny["market_size_factor"] == 0.1
assert big == tiktok_only == unknown_size

falling, _ = opportunity(row(velocity_tiktokshop=-0.3, velocity_google=-0.2), S)
assert falling == 0
print("all metric checks passed")
print(f"example scores: TikTok only={tiktok_only}  +2 confirmations={confirmed}  outside only={outside_only}  "
      f"growth +300%={g3} +3000%={g30}  unchecked saturation={unchecked}  crowded={crowded}")
