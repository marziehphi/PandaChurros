# app.py
import io
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pytz
import streamlit as st

# ==================== Page setup ====================
st.set_page_config(page_title="Sales Explorer PandaChurros AB", layout="wide")

# Show a local logo automatically (place 'logo.png' next to this file)
logo_path = Path("logo.png")
if logo_path.exists():
    st.image(str(logo_path), width=140)
st.title("Sales Explorer PandaChurros AB")

TZ = pytz.timezone("Europe/Stockholm")

# ==================== ID → Friendly name mappings ====================
DEVICE_MAP = {
    "FOR1000785001002": "Ale Torg",
    "FOR1000785001001": "Backaplan",
}

# Normalize “sub-articles” to canonical names (substring, case-insensitive)
ARTICLE_NORMALIZATION = {
    # Churros family
    "churros": "Churros",
    "churros:": "Churros",
    "dubai churros": "Churros",
    "churros med": "Churros",
    "churros nutella": "Churros",
    "churros: nutella": "Churros",
    "churros &": "Churros",
    # Donuts
    "donut": "Donuts",
    "donuts": "Donuts",
    # Fruits / glass examples
    "dubai frukt": "Dubai Frukt",
    "strut stor glass": "Glass",
    "glass": "Glass",
}

# ==================== Robust file reader ====================
def _is_xlsx(b: bytes) -> bool:
    return b.startswith(b"PK\x03\x04")

def _is_xls(b: bytes) -> bool:
    return b.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1")

def read_any_table(file, sheet_name=None) -> pd.DataFrame:
    raw_bytes = file.getvalue() if hasattr(file, "getvalue") else file.read()
    buf = io.BytesIO(raw_bytes)
    try_excel_first = _is_xlsx(raw_bytes) or _is_xls(raw_bytes) \
        or (getattr(file, "name", "").lower().endswith((".xlsx", ".xls")))
    if try_excel_first:
        try:
            buf.seek(0)
            return pd.read_excel(buf, sheet_name=sheet_name, engine="openpyxl")
        except Exception:
            pass
        try:
            buf.seek(0)
            xls = pd.ExcelFile(buf)
            return pd.read_excel(xls, sheet_name=sheet_name)
        except Exception:
            pass
    for enc in ("utf-8", "cp1252", "latin-1", "utf-16"):
        try:
            buf.seek(0)
            return pd.read_csv(buf, sep=None, engine="python", encoding=enc)
        except Exception:
            continue
    raise ValueError("Could not read file as Excel or CSV. For .xlsx install 'openpyxl'. For legacy .xls install 'xlrd==1.2.0'.")

# ==================== Cleaning & helpers ====================
def _normalize_article(name: str) -> str:
    if not isinstance(name, str):
        return name
    raw = name.strip()
    n = raw.lower()
    for key, target in ARTICLE_NORMALIZATION.items():
        if key in n:
            return target
    return raw.title()

def load_data(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).strip() for c in df.columns]
    if "Amount in SEK" in df.columns:
        df = df.rename(columns={"Amount in SEK": "Amount"})
    required = {"Date", "Article", "Amount", "Register Device"}
    missing = [c for c in required if c not in df.columns]
    if missing:
        st.error(f"Missing required columns: {', '.join(missing)}")
        st.stop()

    # Build DateTime
    if "Timestamp" in df.columns:
        date_parsed = pd.to_datetime(df["Date"], errors="coerce")
        ts = pd.to_datetime(df["Timestamp"].astype(str), errors="coerce").dt.strftime("%H:%M:%S")
        dt_combined = pd.to_datetime(date_parsed.dt.date.astype(str) + " " + ts.astype(str), errors="coerce")
        use_date_time = pd.to_datetime(df["Date"], errors="coerce")
        has_time = (use_date_time.dt.normalize() != use_date_time.dt.floor("S"))
        dt = dt_combined.where(~has_time, use_date_time)
    else:
        dt = pd.to_datetime(df["Date"], errors="coerce")

    df = df.loc[~dt.isna()].copy()
    df["DateTime"] = dt.loc[~dt.isna()]

    # Localize/convert timezone
    if df["DateTime"].dt.tz is None:
        df["DateTime"] = df["DateTime"].dt.tz_localize(TZ)
    else:
        df["DateTime"] = df["DateTime"].dt.tz_convert(TZ)

    # Amount
    df["Amount"] = df["Amount"].apply(lambda x: str(x).replace(",", ".") if isinstance(x, str) else x)
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce")
    df = df.dropna(subset=["Amount"]).copy()

    # Normalize article labels
    df["Article"] = df["Article"].apply(_normalize_article)

    # Tidy device name & map to friendly store names
    df = df.rename(columns={"Register Device": "RegisterDevice"})
    df["RegisterDevice"] = df["RegisterDevice"].replace(DEVICE_MAP)

    return df

def interval_to_timedelta(freq: str) -> timedelta:
    if freq.endswith("min"):
        return timedelta(minutes=int(freq.replace("min", "")))
    if freq.endswith("H"):
        return timedelta(hours=int(freq.replace("H", "")))
    return timedelta(0)

# ==================== Sidebar ====================
with st.sidebar:
    st.subheader("Upload Data")
    file = st.file_uploader("Excel (.xlsx/.xls) or CSV", type=["xlsx", "xls", "csv"])
    sheet_name = None
    if file and getattr(file, "name", "").lower().endswith((".xlsx", ".xls")):
        file_bytes = file.getvalue() if hasattr(file, "getvalue") else file.read()
        try:
            xls = pd.ExcelFile(io.BytesIO(file_bytes))
            sheet_name = st.selectbox("Sheet", xls.sheet_names, index=0)
            file = io.BytesIO(file_bytes)
        except Exception:
            file = io.BytesIO(file_bytes)

    st.divider()
    st.subheader("Filters")
    interval_label = st.selectbox("Time interval (for peaks)", ["15 minutes", "30 minutes", "1 hour", "2 hours"], index=3)
    interval_map = {"15 minutes": "15min", "30 minutes": "30min", "1 hour": "1H", "2 hours": "2H"}
    interval = interval_map[interval_label]

    race_top_n = st.slider("Bar race: top N articles per month", 3, 20, 8)
    race_min_months = st.slider("Bar race: appear in ≥ N months", 1, 12, 1)

if not file:
    st.stop()

# ==================== Load & filter ====================
raw = read_any_table(file, sheet_name=sheet_name)
df = load_data(raw)

min_dt, max_dt = df["DateTime"].min(), df["DateTime"].max()
with st.sidebar:
    date_range = st.date_input(
        "Date range", value=(min_dt.date(), max_dt.date()),
        min_value=min_dt.date(), max_value=max_dt.date()
    )
    devices = sorted(df["RegisterDevice"].unique().tolist())
    device_sel = st.multiselect("Register Device", devices, default=devices)
    articles = sorted(df["Article"].unique().tolist())
    article_sel = st.multiselect("Article", articles, default=articles)

if isinstance(date_range, tuple):
    start_date, end_date = date_range
else:
    start_date, end_date = date_range, date_range

mask = (
    (df["DateTime"].dt.date >= start_date) &
    (df["DateTime"].dt.date <= end_date) &
    (df["RegisterDevice"].isin(device_sel)) &
    (df["Article"].isin(article_sel))
)
dff = df.loc[mask].copy()
if dff.empty:
    st.warning("No data after filters.")
    st.stop()

# ==================== 1) Animated Bar Race: Top Articles by Month ====================
st.header("Top Articles by Month")
race = dff.copy()
race["YearMonth"] = race["DateTime"].dt.to_period("M").astype(str)
monthly = race.groupby(["YearMonth", "Article"], as_index=False)["Amount"].sum()

appearances = monthly.groupby("Article")["YearMonth"].nunique()
valid_articles = appearances[appearances >= race_min_months].index
monthly = monthly[monthly["Article"].isin(valid_articles)]

monthly["Rank"] = monthly.groupby("YearMonth")["Amount"].rank(method="first", ascending=False)
monthly_top = monthly[monthly["Rank"] <= race_top_n].copy()
monthly_top = monthly_top.sort_values(["YearMonth", "Amount"], ascending=[True, False])

if monthly_top.empty:
    st.info("Not enough data for the bar race with current filters.")
else:
    x_max = max(1.0, monthly_top["Amount"].max()) * 1.15
    fig_race = px.bar(
        monthly_top,
        x="Amount",
        y="Article",
        color="Article",
        animation_frame="YearMonth",
        orientation="h",
        range_x=[0, x_max],
        text="Amount",
        height=780,
    )
    if fig_race.layout.updatemenus and fig_race.layout.sliders:
        fig_race.layout.updatemenus[0].buttons[0].args[1]["frame"]["duration"] = 1300
        fig_race.layout.updatemenus[0].buttons[0].args[1]["transition"]["duration"] = 800
        fig_race.layout.sliders[0]["currentvalue"]["prefix"] = "Month: "
    fig_race.update_layout(
        xaxis_title="Total Amount (SEK) this month",
        yaxis_title="Article",
        legend_title="Article",
    )
    fig_race.update_traces(texttemplate="%{text:.0f}", textposition="outside", cliponaxis=False)
    st.plotly_chart(fig_race, use_container_width=True)

# ==================== 2) Peak Times by Interval (Overall Transactions) ====================
st.header("Peak Times by Number of Customers")
dff_sorted = dff.sort_values("DateTime").set_index("DateTime")
overall = dff_sorted.resample(interval).size().rename("Transactions").reset_index()
overall["Month"] = overall["DateTime"].dt.to_period("M").astype(str)

# monthly peak intervals (by transactions)
idxmax = overall.groupby("Month")["Transactions"].idxmax()
monthly_peaks = overall.loc[idxmax].copy()
monthly_peaks["Label"] = "Txns: " + monthly_peaks["Transactions"].astype(int).astype(str)

fig_overall = go.Figure()
fig_overall.add_trace(go.Scatter(
    x=overall["DateTime"], y=overall["Transactions"],
    mode="lines", fill="tozeroy",
    name="Overall Transactions",
    hovertemplate="%{y} transactions<br>%{x}<extra></extra>"
))
fig_overall.add_trace(go.Scatter(
    x=monthly_peaks["DateTime"], y=monthly_peaks["Transactions"],
    mode="markers+text",
    name="Monthly Peak",
    marker=dict(size=14, symbol="diamond-open", line=dict(width=2)),
    text=monthly_peaks["Label"],
    textposition="top center",
    hovertemplate="Monthly Peak<br>%{x}<br>%{y} transactions<extra></extra>"
))
fig_overall.update_layout(
    xaxis_title=f"Time ({interval_label})",
    yaxis_title="Transactions",
    legend_title="",
    height=520
)
st.plotly_chart(fig_overall, use_container_width=True)

# ==================== 4) Sales by Hour of Day (Bar + Line) ====================
st.header("Sales by Hour of Day")
tf = dff_sorted.copy()
tf.index = tf.index.tz_convert(TZ)
tf["Hour"] = tf.index.hour
hour_sales = tf.groupby("Hour")["Amount"].sum().reset_index(name="Total Amount (SEK)")

fig_hours = go.Figure()
fig_hours.add_trace(go.Bar(
    x=hour_sales["Hour"], y=hour_sales["Total Amount (SEK)"],
    name="SEK", hovertemplate="Hour %{x}<br>%{y:.0f} SEK<extra></extra>"
))
fig_hours.add_trace(go.Scatter(
    x=hour_sales["Hour"], y=hour_sales["Total Amount (SEK)"],
    mode="lines+markers", name="Trend",
    hovertemplate="Hour %{x}<br>%{y:.0f} SEK<extra></extra>"
))
fig_hours.update_layout(
    xaxis_title="Hour of Day",
    yaxis_title="Total Amount (SEK)",
    showlegend=True,
    height=460
)
st.plotly_chart(fig_hours, use_container_width=True)

# ==================== 6) Calendar Overview: Top 3 Days per Month (Positive “pyramid”) ====================
st.header("Calendar Overview: Top 3 Days per Month")

tf["DateOnly"] = pd.to_datetime(tf.index.date)
daily = tf.groupby("DateOnly")["Amount"].sum().reset_index()
daily["Month"] = daily["DateOnly"].dt.to_period("M")
months_sorted = sorted(daily["Month"].unique().astype(str))
sel_month = st.selectbox("Choose month", months_sorted, index=len(months_sorted)-1)

this_month = pd.Period(sel_month)
m_df = daily[daily["Month"] == this_month].copy()
top3 = m_df.sort_values("Amount", ascending=False).head(3).copy().reset_index(drop=True)

# Positive-only funnel (pyramid-like)
fig_funnel = px.funnel(
    top3,
    y=top3["DateOnly"].dt.strftime("%b %d"),
    x="Amount",
)
labels = top3.apply(lambda r: f"{r['DateOnly'].strftime('%b %d')} | {r['Amount']:.0f} SEK", axis=1)
fig_funnel.update_traces(text=labels, textposition="inside")
fig_funnel.update_layout(
    xaxis_title="Total Amount (SEK)",
    yaxis_title="Top Days",
    height=420,
    showlegend=False,
)
st.plotly_chart(fig_funnel, use_container_width=True)

# ==================== 7) Top Articles During Peak Hours (Bubble Plot) ====================
st.header("Top Articles During Peak Hours")
hour_strength = tf.groupby("Hour")["Amount"].sum().sort_values(ascending=False)
peak_hours = hour_strength.head(3).index.tolist()
st.caption(f"Peak hours (local time): {', '.join(map(str, peak_hours))}")

peak_subset = tf[tf["Hour"].isin(peak_hours)].copy()
if peak_subset.empty:
    st.info("No data in the detected peak hours.")
else:
    bubbles = peak_subset.groupby(["Hour", "Article"])["Amount"].sum().reset_index()
    fig_bubbles = px.scatter(
        bubbles,
        x="Hour", y="Article", size="Amount", color="Article",
        size_max=60, labels={"Amount": "Total Amount (SEK)"},
        hover_data={"Amount":":.0f"}
    )
    fig_bubbles.update_layout(
        xaxis_title="Hour (peak hours)",
        yaxis_title="Article",
        legend_title="Article",
        height=520
    )
    st.plotly_chart(fig_bubbles, use_container_width=True)

# ==================== Footer ====================
st.caption(
    "Tips: • Adjust date range, devices, and articles in the sidebar. "
    "• The bar race animation speed can be controlled by the play button."
)
