"""Live Airport Weather + Polymarket Dashboard.

Run:  pip install -r requirements.txt && streamlit run app.py

Data sources (both free, no API key, read-only):
  - Weather    : Open-Meteo      https://open-meteo.com
  - Prediction : Polymarket Gamma https://gamma-api.polymarket.com
"""
import json
import time
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="Live Airport Weather & Polymarket", layout="wide")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
POLYMARKET_URL = "https://gamma-api.polymarket.com/markets"
REQUEST_TIMEOUT = 15
WEATHER_TTL = 900      # seconds: Open-Meteo updates roughly every 15 min
MARKET_TTL = 60        # seconds

# WMO weather codes -> short text
WMO = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 80: "Rain showers",
    81: "Rain showers", 82: "Violent showers", 95: "Thunderstorm",
    96: "Thunderstorm + hail", 99: "Thunderstorm + hail",
}


# ---------------------------------------------------------------- data ----
@st.cache_data
def load_airports(path: str = "airports.csv") -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8")
    df.columns = [c.strip().lower() for c in df.columns]
    required = {"code", "name", "city", "country", "latitude", "longitude"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"airports.csv mein ye columns missing hain: {sorted(missing)}")
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df = df.dropna(subset=["latitude", "longitude"])
    df = df[df["latitude"].between(-90, 90) & df["longitude"].between(-180, 180)]
    return df.reset_index(drop=True)


@st.cache_resource
def _weather_store() -> dict:
    """Last successful weather result (survives cache expiry / API errors)."""
    return {}


def _get_with_retry(url: str, params: dict, tries: int = 3) -> requests.Response:
    """GET with backoff on 429/5xx (honours Retry-After)."""
    last = None
    for attempt in range(tries):
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code not in (429, 500, 502, 503, 504):
            r.raise_for_status()
            return r
        last = r
        try:
            wait = min(float(r.headers.get("Retry-After", 2 ** (attempt + 1))), 10)
        except ValueError:
            wait = 2 ** (attempt + 1)
        time.sleep(wait)
    last.raise_for_status()
    return last


@st.cache_data(ttl=WEATHER_TTL, show_spinner=False)
def fetch_weather(lats: tuple, lons: tuple) -> tuple[pd.DataFrame, str | None, str | None]:
    """Current weather for ALL airports in one call.

    Returns (df, error, fetched_at). On failure the last good data is reused.
    """
    key = (lats, lons)
    store = _weather_store()
    try:
        rows = []
        chunk = 50
        for i in range(0, len(lats), chunk):
            r = _get_with_retry(OPEN_METEO_URL, {
                "latitude": ",".join(map(str, lats[i:i + chunk])),
                "longitude": ",".join(map(str, lons[i:i + chunk])),
                "current": "temperature_2m,relative_humidity_2m,"
                           "wind_speed_10m,precipitation,weather_code",
                "timezone": "UTC",
            })
            data = r.json()
            data = data if isinstance(data, list) else [data]
            for d in data:
                c = d.get("current", {})
                rows.append({
                    "temp_c": c.get("temperature_2m"),
                    "humidity": c.get("relative_humidity_2m"),
                    "wind_kmh": c.get("wind_speed_10m"),
                    "precip_mm": c.get("precipitation"),
                    "condition": WMO.get(c.get("weather_code"), "N/A"),
                })
        df = pd.DataFrame(rows)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        store[key] = (df, stamp)
        return df, None, stamp
    except (requests.RequestException, ValueError) as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        reason = "rate limit (429), thodi der baad dobara try hoga" if code == 429 else type(e).__name__
        if key in store:
            df, stamp = store[key]
            return df, f"Weather API abhi available nahi ({reason}). Purana data dikha raha hn.", stamp
        return pd.DataFrame(), f"Weather API error: {reason}", None


def _parse_json_list(v):
    if isinstance(v, list):
        return v
    try:
        return json.loads(v) if v else []
    except (TypeError, ValueError):
        return []


@st.cache_data(ttl=MARKET_TTL, show_spinner=False)
def fetch_markets(limit: int = 500) -> tuple[pd.DataFrame, str | None]:
    """Active Polymarket markets, sorted by 24h volume (read-only)."""
    try:
        r = requests.get(
            POLYMARKET_URL,
            params={"active": "true", "closed": "false", "limit": limit,
                    "order": "volume24hr", "ascending": "false"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        out = []
        for m in r.json():
            outcomes = _parse_json_list(m.get("outcomes"))
            prices = _parse_json_list(m.get("outcomePrices"))
            yes = None
            try:
                yes = float(prices[0]) if prices else None
            except (TypeError, ValueError):
                pass
            out.append({
                "question": m.get("question", ""),
                "outcome_1": outcomes[0] if outcomes else "",
                "price_1": yes,
                "volume_24h": float(m.get("volume24hr") or 0),
                "liquidity": float(m.get("liquidity") or 0),
                "ends": (m.get("endDate") or "")[:10],
                "link": f"https://polymarket.com/market/{m.get('slug', '')}",
            })
        return pd.DataFrame(out), None
    except (requests.RequestException, ValueError) as e:
        return pd.DataFrame(), f"Polymarket API error: {e}"


# ---------------------------------------------------------------- UI ------
st.title("🌍 Live Airport Weather & Polymarket Dashboard")

# Sidebar
st.sidebar.header("Settings")
auto = st.sidebar.toggle("Auto refresh", value=True)
interval = st.sidebar.select_slider("Refresh interval (sec)", [30, 60, 120, 300], value=60)
if auto:
    st_autorefresh(interval=interval * 1000, key="autorefresh")
if st.sidebar.button("Refresh markets now"):
    fetch_markets.clear()
    st.rerun()

try:
    airports = load_airports()
except Exception as e:  # noqa: BLE001
    st.error(f"airports.csv load nahi hui: {e}")
    st.stop()

countries = ["All"] + sorted(airports["country"].dropna().unique())
country = st.sidebar.selectbox("Country", countries)
view = airports if country == "All" else airports[airports["country"] == country]
cities = ["All"] + sorted(view["city"].dropna().unique())
city = st.sidebar.selectbox("City", cities)
if city != "All":
    view = view[view["city"] == city]
view_idx = view.index

# Weather: ek hi call sab airports ke liye (filters sirf display badalte hain)
all_weather, w_err, w_stamp = fetch_weather(
    tuple(airports["latitude"]), tuple(airports["longitude"]))
airports_w = airports.copy()
for col in ("temp_c", "humidity", "wind_kmh", "precip_mm", "condition"):
    airports_w[col] = all_weather[col].values if len(all_weather) == len(airports) else None
view = airports_w.loc[view_idx].reset_index(drop=True)

st.caption(f"Weather last fetched: {w_stamp or 'N/A'} (cache {WEATHER_TTL // 60} min)")
if w_err:
    st.warning(w_err)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Airports shown", len(view))
temps = pd.to_numeric(view["temp_c"], errors="coerce")
c2.metric("Avg temp", f"{temps.mean():.1f} °C" if temps.notna().any() else "N/A")
c3.metric("Hottest", f"{temps.max():.1f} °C" if temps.notna().any() else "N/A")
c4.metric("Coldest", f"{temps.min():.1f} °C" if temps.notna().any() else "N/A")

tab_map, tab_table, tab_poly = st.tabs(["🗺️ Map", "📋 Table", "📈 Polymarket"])

with tab_map:
    if view.empty:
        st.warning("Is filter ke liye koi airport nahi mila.")
    else:
        plot_df = view.copy()
        plot_df["temp_label"] = plot_df["temp_c"].apply(
            lambda t: "N/A" if pd.isna(t) else f"{t:.1f} °C")
        fig = px.scatter_geo(
            plot_df, lat="latitude", lon="longitude",
            color=pd.to_numeric(plot_df["temp_c"], errors="coerce"),
            color_continuous_scale="RdYlBu_r",
            hover_name="name",
            hover_data={"code": True, "city": True, "temp_label": True,
                        "condition": True, "latitude": False, "longitude": False},
            projection="natural earth",
        )
        fig.update_traces(marker={"size": 11})
        fig.update_layout(margin={"r": 0, "t": 0, "l": 0, "b": 0},
                          coloraxis_colorbar={"title": "°C"})
        st.plotly_chart(fig, width="stretch")

with tab_table:
    show = view[["code", "name", "city", "country", "temp_c", "humidity",
                 "wind_kmh", "precip_mm", "condition"]]
    st.dataframe(show, width="stretch", hide_index=True)
    st.download_button("Download CSV", show.to_csv(index=False).encode("utf-8"),
                       "airport_weather.csv", "text/csv")

with tab_poly:
    st.write("Polymarket ke active markets (read-only, trading nahi hoti).")
    kw = st.text_input("Keyword filter", value="weather",
                       help="Question mein ye word dhoondega, jaise: temperature, hurricane, rain")
    min_vol = st.number_input("Min 24h volume ($)", min_value=0, value=0, step=1000)
    markets, m_err = fetch_markets()
    if m_err:
        st.error(m_err)
    elif markets.empty:
        st.info("Koi market nahi mili.")
    else:
        m = markets
        if kw:
            m = m[m["question"].str.contains(kw, case=False, na=False)]
        m = m[m["volume_24h"] >= min_vol]
        st.caption(f"{len(m)} markets")
        st.dataframe(
            m, width="stretch", hide_index=True,
            column_config={
                "price_1": st.column_config.ProgressColumn(
                    "Price (1st outcome)", min_value=0.0, max_value=1.0, format="%.2f"),
                "volume_24h": st.column_config.NumberColumn("24h Vol ($)", format="%.0f"),
                "liquidity": st.column_config.NumberColumn("Liquidity ($)", format="%.0f"),
                "link": st.column_config.LinkColumn("Open", display_text="Polymarket"),
            },
        )
