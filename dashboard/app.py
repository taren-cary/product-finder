"""Product Gap Finder dashboard.

Start it with "Open Dashboard.bat" in the project folder, or:
    .venv\\Scripts\\streamlit run dashboard\\app.py

Pages:
  Opportunities     ranked concepts, filters, bulk status changes
  Concept details   every source's history side by side, the items behind the
                    concept, and manual fixes (merge, split, edit keywords)
  Pipeline health   recent runs, failures, spend, credits left
"""

import sys
from datetime import date, timedelta
from pathlib import Path

# Make the project's modules (core, collectors...) importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
import requests  # noqa: E402
import streamlit as st  # noqa: E402

from collectors.keywords import merge_keywords, to_hashtag  # noqa: E402
from core.config import require_env, settings  # noqa: E402
from dashboard import charts, data  # noqa: E402

st.set_page_config(page_title="Product Gap Finder", layout="wide")

STATUSES = ["new", "reviewed", "shortlisted", "rejected"]
SOURCE_LABELS = {"tiktokshop": "TikTok Shop revenue", "shopvideos": "Shoppable video views",
                 "tiktok": "TikTok hashtag views", "google": "Google", "amazon": "Amazon", "reddit": "Reddit"}
TIKTOK_KEYS = ("tiktokshop", "shopvideos", "tiktok")


def open_concept(concept_id: int) -> None:
    st.session_state["concept_id"] = int(concept_id)
    st.switch_page(concept_page)


def pct(value) -> str:
    return "–" if value is None or pd.isna(value) else f"{value * 100:+.0f}%"


# =============================================================================
# Page 1: Opportunities
# =============================================================================

def opportunities() -> None:
    st.title("Opportunities")
    all_weeks = data.weeks()
    if not all_weeks:
        st.info("No scores yet. They appear after the pipeline's features and scoring steps run.")
        return

    # --- Filters, in one row above the table ---
    f = st.columns([1.1, 1, 1.2, 1.2, 1.8, 1.6])
    week = f[0].selectbox("Week of", all_weeks, format_func=lambda d: d.strftime("%b %d, %Y"))
    min_confirm = f[1].selectbox("Min. confirmations", [0, 1, 2, 3], index=0,
                                 help="How many of Google, Amazon and Reddit also show demand up 10%+.")
    max_sat = f[2].slider("Max. TikTok saturation", 0.0, 1.0, 1.0, 0.05,
                          help="0 = nobody selling it on TikTok Shop, 1 = crowded.")
    age = f[3].selectbox("First spotted", ["Any time", "Last 7 days", "Last 30 days", "Last 90 days"])
    statuses = f[4].multiselect("Status", STATUSES, default=["new", "reviewed", "shortlisted"])
    search = f[5].text_input("Search", placeholder="e.g. lamp, pets")

    df = data.ranking(week)
    g = st.columns([1.2, 1.2, 1.6, 1.5, 1.5])
    only_data = g[0].toggle("Only concepts with data", value=True,
                            help="Concepts not looked up yet have no score.")
    only_fit = g[1].toggle("Only TikTok-fit", value=True,
                           help="Hide concepts judged not sellable on TikTok Shop.")
    only_lists = g[2].toggle("On TikTok Shop lists this week", value=False,
                             help="Only concepts that showed up in this week's rising / new / viral TikTok Shop lists.")
    only_checked = g[3].toggle("Only saturation-checked", value=True,
                               help="Only concepts whose TikTok Shop sellers and creators have been checked.")
    min_market = g[4].selectbox("Min. TikTok Shop sales / week", [0, 1000, 5000, 25000, 100000], index=2,
                                format_func=lambda v: "Any" if v == 0 else f"${v:,}",
                                help="Total TikTok Shop revenue for the keyword over the last 7 days.")

    view = df[df["review_status"].isin(statuses)]
    if only_fit:
        view = view[view["tiktok_fit"].fillna(False).astype(bool)]
    if only_data:
        view = view[view["has_data"].fillna(False)]
    if only_lists:
        view = view[view["on_tiktok_lists"].fillna(False).astype(bool)]
    if only_checked:
        view = view[view["saturation_checked"].fillna(False).astype(bool)]
    if min_market:
        view = view[view["shop_revenue_7d"].notna() & (view["shop_revenue_7d"] >= min_market)]
    if min_confirm:
        view = view[view["confirmations"].fillna(0) >= min_confirm]
    if max_sat < 1.0:
        view = view[view["tiktok_saturation"].notna() & (view["tiktok_saturation"] <= max_sat)]
    if age != "Any time":
        days = int(age.split()[1])
        view = view[pd.to_datetime(view["first_spotted"]) >= pd.Timestamp(date.today() - timedelta(days=days))]
    if search:
        s = search.lower()
        view = view[view["name"].str.contains(s, case=False) | view["category"].fillna("").str.contains(s, case=False)]

    # --- Headline tiles ---
    t = st.columns(4)
    t[0].metric("TikTok-fit concepts", int(df["tiktok_fit"].fillna(False).astype(bool).sum()),
                help=f"Out of {len(df)} concepts in total.")
    t[1].metric("With data this week", int(df["has_data"].fillna(False).sum()))
    t[2].metric("Shortlisted", int((df["review_status"] == "shortlisted").sum()))
    top = df.dropna(subset=["opportunity_score"]).head(1)
    t[3].metric("Top score", f"{top['opportunity_score'].iloc[0]:.1f}" if len(top) else "–",
                help=top["name"].iloc[0] if len(top) else None)

    # --- Sparklines: Google interest (last 26 weeks) and score by week ---
    google = data.google_series()
    history = data.score_history()
    view = view.copy()
    view["google_trend"] = [
        [v for _, v in google.get(data.keyword_for(r), [])][-26:] or None for _, r in view.iterrows()
    ]
    view["score_trend"] = [
        list(history.loc[history["concept_id"] == cid, "opportunity_score"].fillna(0)) or None
        for cid in view["id"]
    ]

    st.caption(f"{len(view)} concepts shown. Select rows to open one or change their status. "
               "Scores firm up after 2–4 weeks of history.")
    columns = ["rank", "name", "opportunity_score", "tiktok_momentum", "velocity_tiktokshop",
               "velocity_shopvideos", "tiktok_saturation", "sellers", "creators", "new_product_share",
               "shop_revenue_7d", "revenue_per_seller", "confirmations",
               "google_trend", "score_trend", "on_tiktok_lists", "lead_lag_gap", "paid_share", "spike_risk",
               "category", "review_status", "first_spotted", "items"]
    event = st.dataframe(
        view[columns],
        hide_index=True,
        width="stretch",
        height=560,
        on_select="rerun",
        selection_mode="multi-row",
        key="ranking_table",
        column_config={
            "rank": st.column_config.NumberColumn("Rank", width="small"),
            "name": st.column_config.TextColumn("Concept", width="medium"),
            "opportunity_score": st.column_config.NumberColumn("Score", format="%.1f",
                help="TikTok momentum (+ outside demand) x confirmations x TikTok headroom x steadiness."),
            "tiktok_momentum": st.column_config.NumberColumn("TikTok momentum", format="percent",
                help="Weekly growth on TikTok: shop revenue, shoppable-video views, hashtag views."),
            "velocity_shopvideos": st.column_config.NumberColumn("Shop video views", format="percent"),
            "confirmations": st.column_config.NumberColumn("Confirms", width="small",
                help="How many of Google, Amazon and Reddit are also rising (0-3)."),
            "on_tiktok_lists": st.column_config.CheckboxColumn("On TikTok lists", width="small",
                help="Showed up in this week's rising / new / viral TikTok Shop lists."),
            "google_trend": st.column_config.LineChartColumn("Google, 6 mo", y_min=0, y_max=100),
            "score_trend": st.column_config.LineChartColumn("Score by week"),
            "velocity_tiktokshop": st.column_config.NumberColumn("TikTok Shop growth", format="percent"),
            "tiktok_saturation": st.column_config.ProgressColumn("TikTok saturation", min_value=0,
                max_value=1, format="%.2f",
                help="Compared with every other checked concept: 0.1 = among the least crowded, 0.9 = among the most."),
            "sellers": st.column_config.NumberColumn("Active sellers", width="small",
                help="Sellers with $300+ of sales in the last 7 days for this keyword."),
            "new_product_share": st.column_config.NumberColumn("New products' share", format="percent",
                help="Share of TikTok Shop revenue going to products launched in the last 60 days. High = market still open."),
            "shop_revenue_7d": st.column_config.NumberColumn("TikTok Shop sales / week", format="dollar",
                help="Total revenue of the top matching TikTok Shop products, last 7 days (market size)."),
            "revenue_per_seller": st.column_config.NumberColumn("Revenue / active seller", format="dollar",
                help="Last 7 days. High with few sellers = room for another."),
            "creators": st.column_config.NumberColumn("Creators", width="small"),
            "lead_lag_gap": st.column_config.NumberColumn("Lead-lag gap", format="percent",
                help="Outside demand growth minus TikTok Shop supply growth. Positive = the gap you want."),
            "paid_share": st.column_config.NumberColumn("Views from ads", format="percent"),
            "spike_risk": st.column_config.CheckboxColumn("Spike?", width="small",
                help="Recent rise looks like a one-off spike rather than a steady climb."),
            "category": st.column_config.TextColumn("Category"),
            "review_status": st.column_config.TextColumn("Status", width="small"),
            "first_spotted": st.column_config.DateColumn("First spotted", format="MMM D"),
            "items": st.column_config.NumberColumn("Items", width="small"),
        },
    )

    selected = view.iloc[event.selection.rows] if event and event.selection.rows else view.iloc[0:0]
    a = st.columns([1.2, 1, 1, 1, 1, 3])
    if a[0].button("Open details", disabled=len(selected) != 1, type="primary"):
        open_concept(selected["id"].iloc[0])
    for col, (label, status) in zip(a[1:5], [("Shortlist", "shortlisted"), ("Mark reviewed", "reviewed"),
                                               ("Reject", "rejected"), ("Reset to new", "new")]):
        if col.button(label, disabled=len(selected) == 0):
            data.set_status(list(selected["id"].astype(int)), status)
            st.toast(f"{len(selected)} concept(s) set to {status}")
            st.rerun()
    if len(selected):
        a[5].caption(f"{len(selected)} selected")


# =============================================================================
# Page 2: Concept details
# =============================================================================

def concept_details() -> None:
    options = data.concept_options()
    if not len(options):
        st.info("No concepts yet.")
        return
    ids = list(options["id"].astype(int))
    labels = dict(zip(ids, options["name"]))
    current = st.session_state.get("concept_id", ids[0])
    if current not in labels:
        current = ids[0]
    concept_id = st.selectbox("Concept", ids, index=ids.index(current), format_func=lambda i: labels[i])
    st.session_state["concept_id"] = concept_id

    c = data.concept(concept_id)
    weeks_df = data.concept_weeks(concept_id)
    latest = weeks_df.iloc[-1].to_dict() if len(weeks_df) else {}
    keyword = data.keyword_for(c)

    st.title(c["name"])
    st.caption(f"{c['category'] or 'No category'} · searched as “{keyword}” · "
               f"first spotted {pd.to_datetime(c['created_at']).strftime('%b %d, %Y')} · status: **{c['review_status']}**")
    if c.get("description"):
        st.write(c["description"])
    fit = c.get("tiktok_fit")
    fit = None if fit is None or pd.isna(fit) else bool(fit)
    fit_label = {True: "✅ TikTok fit", False: "🚫 Not for TikTok", None: "⏳ Not judged yet"}[fit]
    fc = st.columns([3, 1.2])
    fc[0].markdown(f"**{fit_label}** — {c.get('tiktok_fit_reason') or ''}"
                   + (" *(set by hand)*" if c.get("tiktok_fit_by") == "manual" else ""))
    if fit is True:
        if fc[1].button("Mark not for TikTok"):
            data.set_fit([concept_id], False)
            st.rerun()
    elif fc[1].button("Mark as TikTok fit"):
        data.set_fit([concept_id], True)
        st.rerun()

    # Status buttons
    b = st.columns(5)
    for col, (label, status) in zip(b[:4], [("Shortlist", "shortlisted"), ("Mark reviewed", "reviewed"),
                                             ("Reject", "rejected"), ("Reset to new", "new")]):
        if col.button(label, disabled=c["review_status"] == status, key=f"status_{status}"):
            data.set_status([concept_id], status)
            st.rerun()

    # Headline numbers
    prev_score = weeks_df["opportunity_score"].iloc[-2] if len(weeks_df) >= 2 else None
    prev_score = None if prev_score is None or pd.isna(prev_score) else prev_score
    m = st.columns(6)
    score = latest.get("opportunity_score")
    score = None if score is None or pd.isna(score) else score
    m[0].metric("Score" + (f" (rank {int(latest['rank'])})" if latest.get("rank") else ""),
                f"{score:.1f}" if score is not None and not pd.isna(score) else "–",
                delta=None if prev_score is None or score is None else f"{score - prev_score:+.1f} vs last week")
    m[1].metric("TikTok momentum", pct(latest.get("tiktok_momentum")))
    confirms = latest.get("confirmations")
    m[2].metric("Confirmations", "–" if confirms is None or pd.isna(confirms) else f"{int(confirms)} of 3",
                help="Google, Amazon and Reddit also rising")
    sat = latest.get("tiktok_saturation")
    m[3].metric("TikTok saturation", f"{sat:.2f}" if sat is not None and not pd.isna(sat) else "–",
                help="0 = empty, 1 = crowded")
    m[4].metric("Lead-lag gap", pct(latest.get("lead_lag_gap")))
    m[5].metric("Views from ads", "–" if latest.get("paid_share") is None or pd.isna(latest.get("paid_share"))
                else f"{latest['paid_share'] * 100:.0f}%")
    if latest.get("spike_risk"):
        st.warning("⚠ Spike risk: the recent Google rise looks like a one-off spike, not a steady climb.")

    with st.expander("Why this score"):
        details = latest.get("details") or {}
        breakdown = details.get("score_breakdown") or {}
        rising = {**(breakdown.get("tiktok_rising") or {}), **(breakdown.get("outside_rising") or {})}
        rows = [{"Source": SOURCE_LABELS[s], "Role": "TikTok (core)" if s in TIKTOK_KEYS else "Confirmation",
                 "Weekly growth": pct(latest.get(f"velocity_{s}")),
                 "Counts toward score": "yes" if s in rising else "no"}
                for s in SOURCE_LABELS]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        shop = details.get("shop") or {}
        st.markdown(
            f"- TikTok demand: **{breakdown.get('tiktok_demand', '–')}**, outside demand: "
            f"**{breakdown.get('outside_demand', '–')}**, confirmation bonus "
            f"x**{breakdown.get('confirmation_multiplier', '–')}**\n"
            f"- Saturation used: **{breakdown.get('saturation_used', '–')}**, so the score is multiplied by "
            f"**{breakdown.get('headroom_factor', '–')}** (TikTok headroom)\n"
            f"- Steadiness used: **{breakdown.get('sustained_used', '–')}** (0.5 = brief rise, 1.0 = steady "
            f"climb; based on {details.get('sustained_basis') or 'no history yet'})\n"
            f"- TikTok Shop (checked {shop.get('checked') or 'not yet'}): {shop.get('sellers', '–')} sellers, "
            f"${shop.get('revenue') or 0:,.0f} revenue last 7 days, top 3 sellers take "
            f"{(shop.get('top3_share') or 0) * 100:.0f}%; growth from {shop.get('growth_basis') or '–'}\n"
            f"- Listings on this week's TikTok Shop lists: {details.get('tiktok_listings_this_week', 0)}"
        )

    # Every source's history, side by side (one measure per chart)
    st.subheader("History by source")
    google = pd.DataFrame(data.google_series().get(keyword, []), columns=["week_start", "interest"])
    shop_series = data.weekly_source_series(keyword, to_hashtag(keyword))
    grid = st.columns(2)
    with grid[0]:
        charts.line(google, "week_start", "interest", "Google search interest (12 months)", "Interest (0–100)")
    with grid[1]:
        charts.line(weeks_df, "week_start", "opportunity_score", "Opportunity score by week", "Score", ",.1f")
    with grid[0]:
        charts.line(shop_series, "week_start", "shop_revenue", "TikTok Shop revenue, top 100 products (7 days)",
                    "Revenue (USD)", "$,.0f")
    with grid[1]:
        charts.line(shop_series, "week_start", "sellers", "TikTok Shop sellers", "Sellers")
    with grid[0]:
        charts.line(shop_series, "week_start", "hashtag_views", f"TikTok #{to_hashtag(keyword)} total views",
                    "Views", "~s")
    with grid[1]:
        charts.line(data.amazon_ranks(concept_id), "week_start", "best_rank",
                    "Best Amazon Best Sellers rank (lower is better)", "Rank", reverse_y=True, zero=False)
    with grid[0]:
        charts.line(data.reddit_mentions(concept_id), "week_start", "mentions",
                    "Reddit buy-intent posts per week", "Posts")

    # The TikTok Shop sellers behind the saturation numbers
    st.subheader("TikTok Shop sellers")
    min_rev = settings["features"]["active_seller_min_revenue_7d"]
    # The same search(es) the score used for this concept.
    used = (latest.get("details") or {}).get("keywords_merged") or merge_keywords(c)[:1]
    sellers, searches = data.shop_sellers(tuple(used), min_rev)
    if searches:
        st.caption("Searched: " + "; ".join(
            f"“{k}” on {d.strftime('%b %d') if hasattr(d, 'strftime') else d} ({n} products)" for k, d, n in searches)
            + f". A seller counts as active with ${min_rev:,.0f}+ of sales in the last 7 days. "
            "Check for products that don't belong: keyword matching isn't perfect.")
    if not len(sellers):
        st.caption("Not checked on TikTok Shop yet.")
    else:
        st.dataframe(sellers, hide_index=True, width="stretch", column_config={
            "active": st.column_config.CheckboxColumn("Active", width="small"),
            "seller": "Seller",
            "revenue_7d": st.column_config.NumberColumn("Sales, 7 days", format="dollar"),
            "units_7d": st.column_config.NumberColumn("Units, 7 days"),
            "products": st.column_config.NumberColumn("Products", width="small"),
            "top_product": st.column_config.TextColumn("Best-selling product", width="large"),
            "newest_launch": st.column_config.TextColumn("Newest launch", width="small"),
        })

    # The items behind the concept
    st.subheader("Items in this concept")
    items = data.concept_items(concept_id)
    if not len(items):
        st.caption("No items.")
    else:
        event = st.dataframe(
            items[["source", "title", "normalized_description", "price", "rank", "category",
                   "first_seen", "last_seen", "assigned_by", "url"]],
            hide_index=True, width="stretch", on_select="rerun", selection_mode="multi-row",
            key=f"items_{concept_id}",
            column_config={
                "source": "Source", "title": st.column_config.TextColumn("Title", width="large"),
                "normalized_description": "Description",
                "price": st.column_config.NumberColumn("Price", format="dollar"),
                "rank": "Rank", "category": "Listed under",
                "first_seen": st.column_config.DateColumn("First seen", format="MMM D"),
                "last_seen": st.column_config.DateColumn("Last seen", format="MMM D"),
                "assigned_by": "Grouped by",
                "url": st.column_config.LinkColumn("Link", display_text="open"),
            },
        )
        picked = items.iloc[event.selection.rows] if event and event.selection.rows else items.iloc[0:0]
        if len(picked):
            st.markdown(f"**{len(picked)} item(s) selected** — move them (split) or mark as not a product:")
            s = st.columns([2, 2, 1, 1.2])
            others = [i for i in ids if i != concept_id]
            target = s[0].selectbox("Move to existing concept", [None] + others,
                                    format_func=lambda i: "—" if i is None else labels[i])
            new_name = s[1].text_input("…or to a new concept named")
            if s[2].button("Move", type="primary", disabled=target is None and not new_name.strip()):
                try:
                    moved_to = data.move_items(list(picked["id"].astype(int)), target, new_name, c["category"])
                    st.toast(f"Moved {len(picked)} item(s)")
                    if target is None:
                        st.session_state["concept_id"] = moved_to
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
            if s[3].button("Not a product"):
                data.mark_not_product(list(picked["id"].astype(int)))
                st.rerun()

    # Manual fixes
    with st.expander("Edit name, category or search keywords"):
        with st.form(f"edit_{concept_id}"):
            name = st.text_input("Name", c["name"])
            category = st.text_input("Category", c["category"] or "")
            kw_text = st.text_input("Search keywords (comma-separated, most important first)",
                                    ", ".join(c["keywords"] or []),
                                    help="The first keyword is what Google Trends, TikTok and Kalodata look up.")
            if st.form_submit_button("Save"):
                keywords = [k.strip().lower() for k in kw_text.split(",") if k.strip()]
                try:
                    data.update_concept(concept_id, name, category, keywords or [name])
                    st.rerun()
                except Exception as e:
                    st.error(f"Couldn't save: {e}")

    with st.expander("Merge into another concept"):
        st.caption("Use this when two concepts are really the same product. This concept's items and keywords "
                   "move into the one you pick, and this concept disappears from the lists.")
        others = [i for i in ids if i != concept_id]
        target = st.selectbox("Merge into", others, format_func=lambda i: labels[i], key=f"merge_{concept_id}")
        sure = st.checkbox(f"Yes, merge “{c['name']}” into “{labels.get(target, '')}”")
        if st.button("Merge", disabled=not sure, type="primary"):
            data.merge(concept_id, target)
            st.session_state["concept_id"] = target
            st.rerun()


# =============================================================================
# Page 3: Pipeline health
# =============================================================================

def kalodata_balance() -> str:
    r = requests.get("https://www.kalodata.com/openapi/v1/credit/balance",
                     headers={"X-API-Key": require_env("KALODATA_API_KEY")}, timeout=20)
    return f"{r.json()['data']['totalRemain']:.1f} credits"


def apify_usage() -> str:
    r = requests.get("https://api.apify.com/v2/users/me/limits",
                     headers={"Authorization": "Bearer " + require_env("APIFY_API_TOKEN")}, timeout=20)
    d = r.json()["data"]
    return f"${d['current']['monthlyUsageUsd']:.2f} of ${d['limits']['maxMonthlyUsageUsd']:.0f}"


def pipeline_health() -> None:
    st.title("Pipeline health")
    stats = data.database_stats()
    t = st.columns(4)
    t[0].metric("Database size", f"{stats['db_bytes'] / 1e6:.0f} MB",
                help="Supabase's free plan allows about 500 MB.")
    t[1].metric("Items collected", f"{int(stats['items']):,}")
    t[2].metric("Waiting to be grouped", f"{int(stats['unclassified']):,}")
    t[3].metric("Concepts", f"{int(stats['concepts']):,}")

    st.subheader("Credits and usage")
    if st.button("Check live balances"):
        b = st.columns(2)
        for col, label, fn in [(b[0], "Kalodata", kalodata_balance), (b[1], "Apify this month", apify_usage)]:
            try:
                col.metric(label, fn())
            except Exception as e:
                col.error(f"{label}: couldn't check ({e})")

    month_start = date.today().replace(day=1)
    spend = data.spend_by_source(month_start)
    st.markdown(f"**Estimated spend since {month_start.strftime('%b %d')}:** "
                f"${spend['usd'].sum() if len(spend) else 0:,.2f} (Apify and Claude; Kalodata is in credits)")
    if len(spend):
        st.dataframe(spend, hide_index=True, width="stretch", column_config={
            "source": "Step", "usd": st.column_config.NumberColumn("USD", format="dollar"),
            "runs": "Runs", "failed": "Failed"})

    st.subheader("Recent runs")
    runs = data.recent_runs(14)
    only_problems = st.toggle("Only failures and partial runs")
    if only_problems:
        runs = runs[runs["status"].isin(["failed", "partial", "running"])]
    if not len(runs):
        st.success("Nothing to show.")
        return
    runs = runs.assign(status=runs["status"].map(
        {"success": "✅ success", "partial": "⚠️ partial", "failed": "❌ failed", "running": "⏳ running"}))
    st.dataframe(runs, hide_index=True, width="stretch", column_config={
        "started_at": st.column_config.DatetimeColumn("Started", format="MMM D, h:mm a"),
        "source": "Step", "snapshot_date": None, "status": "Status",
        "records_saved": "Records", "estimated_cost_usd": st.column_config.NumberColumn("Cost", format="dollar"),
        "seconds": "Seconds", "error_message": st.column_config.TextColumn("Error", width="large"),
    })


# =============================================================================

if st.sidebar.button("🔄 Refresh data", help="Reload everything from the database now."):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption("Data refreshes on its own every minute.")

opportunities_page = st.Page(opportunities, title="Opportunities", icon="📈", default=True)
concept_page = st.Page(concept_details, title="Concept details", icon="🔎", url_path="concept")
health_page = st.Page(pipeline_health, title="Pipeline health", icon="🩺", url_path="health")
st.navigation([opportunities_page, concept_page, health_page]).run()
