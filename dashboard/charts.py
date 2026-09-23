"""Small, consistent line charts for the dashboard.

Each chart shows ONE measure on its own axis (never two scales on one chart),
as a thin line in a single accent color, with a hover crosshair + tooltip.
Colors come from the validated reference palette (series slot 1), stepped
separately for light and dark mode.
"""

import altair as alt
import pandas as pd
import streamlit as st

ACCENT = {"light": "#2a78d6", "dark": "#3987e5"}


def _accent() -> str:
    try:
        return ACCENT.get(st.context.theme.type or "light", ACCENT["light"])
    except Exception:
        return ACCENT["light"]


def line(df: pd.DataFrame, x: str, y: str, title: str, y_title: str,
         y_format: str = ",.0f", reverse_y: bool = False, zero: bool = True,
         height: int = 180) -> None:
    """Draw a single-series line chart, or a short note if there's too little data."""
    st.markdown(f"**{title}**")
    data = df.dropna(subset=[y]) if len(df) else df
    if len(data) == 0:
        st.caption("No data yet.")
        return
    if len(data) == 1:
        value = float(data[y].iloc[0])
        shown = f"{value:,.1f}" if abs(value) < 100 else f"{value:,.0f}"
        st.caption(f"One data point so far ({shown}, week of {data[x].iloc[0]}). "
                   "A line appears once there are two weeks of data.")
        return

    color = _accent()
    x_enc = alt.X(f"{x}:T", title=None, axis=alt.Axis(format="%b %d", labelOverlap=True, grid=False))
    y_enc = alt.Y(f"{y}:Q", title=y_title,
                  scale=alt.Scale(reverse=reverse_y, zero=zero and not reverse_y),
                  axis=alt.Axis(format=y_format, tickCount=4))
    tooltip = [alt.Tooltip(f"{x}:T", title="Week", format="%b %d, %Y"),
               alt.Tooltip(f"{y}:Q", title=y_title, format=y_format)]

    hover = alt.selection_point(fields=[x], nearest=True, on="pointerover", empty=False)
    base = alt.Chart(data).encode(x=x_enc, y=y_enc)
    line_mark = base.mark_line(color=color, strokeWidth=2, interpolate="monotone")
    # Invisible wide targets so hovering anywhere near a week picks it up.
    targets = base.mark_point(opacity=0, size=400).add_params(hover).encode(tooltip=tooltip)
    dot = base.mark_point(color=color, filled=True, size=70).transform_filter(hover)
    rule = alt.Chart(data).mark_rule(opacity=0.35).encode(x=f"{x}:T").transform_filter(hover)

    chart = (line_mark + targets + rule + dot).properties(height=height)
    st.altair_chart(chart, width="stretch")
