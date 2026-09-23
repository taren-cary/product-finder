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
from features.run import (amazon_velocity, google_metrics, growth, mention_velocity,
                          saturation, shop_stats, video_stats, weighted_velocity)
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
assert weighted_velocity({"google": 0.5, "amazon": None, "reddit": 5.0}, F["outside_weights"], 2.0) == (0.5 + 0.5 * 2.0) / 1.5

# Scores: rising on 3 sources + empty TikTok Shop should beat
# the same demand on a crowded TikTok Shop, and a spike should be penalized.
base = {"velocity_google": 0.4, "velocity_amazon": 0.3, "velocity_reddit": 0.5,
        "velocity_tiktok": None, "velocity_tiktokshop": None,
        "sustained_factor": 1.0, "spike_risk": False, "margin_factor": 1.0}
gap_score, b = opportunity({**base, "tiktok_saturation": 0.0}, S)
crowded_score, _ = opportunity({**base, "tiktok_saturation": 1.0}, S)
spike_score, _ = opportunity({**base, "tiktok_saturation": 0.0, "spike_risk": True}, S)
one_source, _ = opportunity({**base, "velocity_amazon": None, "velocity_reddit": None, "tiktok_saturation": 0.0}, S)
expected = 100 * (0.4 + 0.3 + 0.5 * 0.5) * (1 + 0.25 * 2)
assert abs(gap_score - expected) < 0.01, (gap_score, expected)
assert abs(crowded_score - expected / 5) < 0.01
assert abs(spike_score - expected / 2) < 0.01
assert one_source < gap_score / 2
falling, _ = opportunity({**base, "velocity_google": -0.5, "velocity_amazon": -0.2, "velocity_reddit": None, "tiktok_saturation": 0.0}, S)
assert falling == 0
print("all metric checks passed")
print(f"example scores: gap={gap_score}  crowded={crowded_score}  spike={spike_score}  one-source={one_source}")
