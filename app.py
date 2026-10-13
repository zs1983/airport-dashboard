"""Polymarket Temperature Dashboard.

Run:  pip install -r requirements.txt && streamlit run app.py

Data (free, no API key):
  - Polymarket Gamma API   -> "Highest temperature in <city> on <date>?" markets
  - Open-Meteo forecast    -> 4 weather models (one per column group)
  - Open-Meteo geocoding   -> lat/lon/timezone for cities not in airports.csv
"""
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="Polymarket Temperature Dashboard", layout="wide")

POLY_EVENTS_URL = "https://gamma-api.polymarket.com/events"
METEO_URL = "https://api.open-meteo.com/v1/forecast"
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
TIMEOUT = 15
POLY_TTL = 10          # seconds
WEATHER_TTL = 300      # seconds (Open-Meteo data itself updates ~every 15 min)
PKT = ZoneInfo("Asia/Karachi")

# Column group -> Open-Meteo model.  These are the *models* closest to each
# site's data source; they are NOT the sites' own published numbers.
SOURCES = [
    ("Windy.com", "ECMWF", "ecmwf_ifs025"),
    ("Weather.gov", "NOAA GFS", "gfs_seamless"),
    ("Wunderground.com", "DWD ICON", "icon_seamless"),
    ("Weather.com", "CMA", "cma_grapes_global"),
]
TITLE_RE = re.compile(r"^Highest temperature in (.+?) on (.+?)\?*$", re.I)


# ------------------------------------------------------------ helpers -----
def c_to_f(c):
    return None if c is None or pd.isna(c) else c * 9 / 5 + 32


def temp_cell(c) -> str:
    if c is None or pd.isna(c):
        return "N/A"
    return f"{c:.1f}°C<br>{c_to_f(c):.1f}°F"


def temp_txt(c) -> str:
    return "N/A" if c is None or pd.isna(c) else f"{c:.1f}°C / {c_to_f(c):.1f}°F"


def _json_list(v):
    if isinstance(v, list):
        return v
    try:
        return json.loads(v) if v else []
    except (TypeError, ValueError):
        return []


def get_retry(url, params, tries=3):
    """GET with backoff on 429/5xx (honours Retry-After)."""
    last = None
    for attempt in range(tries):
        r = requests.get(url, params=params, timeout=TIMEOUT)
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


@st.cache_resource
def _store() -> dict:
    """Last good results, reused when an API is down / rate limited."""
    return {}


# ------------------------------------------------------------- data -------
@st.cache_data
def load_airports(path: str = "airports.csv") -> pd.DataFrame:
    try:
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        return df.dropna(subset=["city", "latitude", "longitude"])
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["city", "country", "latitude", "longitude"])


@st.cache_data(show_spinner=False)
def locate(city: str) -> dict | None:
    """City -> {country, lat, lon}. airports.csv first, then Open-Meteo geocoding."""
    ap = load_airports()
    m = ap[ap["city"].str.lower() == city.lower()]
    if not m.empty:
        r = m.iloc[0]
        return {"country": r.get("country", ""), "lat": float(r["latitude"]),
                "lon": float(r["longitude"])}
    try:
        r = get_retry(GEO_URL, {"name": city, "count": 1, "language": "en"})
        res = (r.json().get("results") or [None])[0]
        if res:
            return {"country": res.get("country", ""), "lat": res["latitude"],
                    "lon": res["longitude"]}
    except (requests.RequestException, ValueError):
        pass
    return None


@st.cache_data(ttl=POLY_TTL, show_spinner=False)
def fetch_polymarket() -> tuple[pd.DataFrame, str | None]:
    """Next 'Highest temperature in <city>' event per city, with top outcomes."""
    key = "poly"
    now = datetime.now(timezone.utc)
    params = {
        "tag_slug": "weather", "active": "true", "closed": "false", "limit": 100,
        "order": "endDate", "ascending": "true",
        "end_date_min": now.strftime("%Y-%m-%dT00:00:00Z"),
        "end_date_max": (now + timedelta(days=3)).strftime("%Y-%m-%dT00:00:00Z"),
    }
    try:
        events = []
        for page in range(6):
            r = get_retry(POLY_EVENTS_URL, {**params, "offset": page * 100})
            batch = r.json()
            events += batch
            if len(batch) < 100:
                break
        best = {}
        for ev in events:
            m = TITLE_RE.match(ev.get("title", ""))
            if not m:
                continue
            city = m.group(1).strip()
            end = ev.get("endDate") or ""
            if city in best and best[city]["end"] <= end:
                continue
            buckets = []
            for mk in ev.get("markets", []):
                prices = _json_list(mk.get("outcomePrices"))
                try:
                    p = float(prices[0]) * 100
                except (IndexError, TypeError, ValueError):
                    continue
                buckets.append((mk.get("groupItemTitle") or "?", p))
            buckets.sort(key=lambda x: x[1], reverse=True)
            best[city] = {"city": city, "end": end, "buckets": buckets,
                          "link": f"https://polymarket.com/event/{ev.get('slug', '')}"}
        df = pd.DataFrame(best.values())
        _store()[key] = df
        return df, None
    except (requests.RequestException, ValueError) as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        msg = f"Polymarket API error ({code or type(e).__name__})"
        if key in _store():
            return _store()[key], msg + " - purana data dikha raha hn."
        return pd.DataFrame(), msg


@st.cache_data(ttl=WEATHER_TTL, show_spinner=False)
def fetch_weather(coords: tuple) -> tuple[dict, str | None, str | None]:
    """coords = ((lat, lon), ...). Returns ({(lat,lon): info}, error, stamp)."""
    key = ("w", coords)
    models = ",".join(s[2] for s in SOURCES)
    out, err = {}, None
    try:
        for i in range(0, len(coords), 25):
            chunk = coords[i:i + 25]
            r = get_retry(METEO_URL, {
                "latitude": ",".join(str(c[0]) for c in chunk),
                "longitude": ",".join(str(c[1]) for c in chunk),
                "hourly": "temperature_2m", "daily": "temperature_2m_max,temperature_2m_min",
                "models": models, "forecast_days": 2, "timezone": "auto",
            })
            data = r.json()
            data = data if isinstance(data, list) else [data]
            for c, d in zip(chunk, data):
                off = d.get("utc_offset_seconds", 0)
                tzname = d.get("timezone", "UTC")
                now_local = datetime.now(timezone.utc) + timedelta(seconds=off)
                today = now_local.strftime("%Y-%m-%d")
                now_h = now_local.strftime("%Y-%m-%dT%H:00")
                hourly, daily = d.get("hourly", {}), d.get("daily", {})
                times = hourly.get("time", [])
                dtimes = daily.get("time", [])
                per = {}
                for name, _lbl, model in SOURCES:
                    vals = hourly.get(f"temperature_2m_{model}") or []
                    cur, past = None, []
                    for t, v in zip(times, vals):
                        if v is None or not t.startswith(today) or t > now_h:
                            continue
                        past.append(v)
                        if t == now_h:
                            cur = v
                    if cur is None and past:
                        cur = past[-1]
                    mx = daily.get(f"temperature_2m_max_{model}") or []
                    mn = daily.get(f"temperature_2m_min_{model}") or []
                    nxt = dtimes.index(
                        (now_local + timedelta(days=1)).strftime("%Y-%m-%d")
                    ) if (now_local + timedelta(days=1)).strftime("%Y-%m-%d") in dtimes else None
                    per[name] = {
                        "current": cur,
                        "max_rec": max(past) if past else None,
                        "next_min": mn[nxt] if nxt is not None and nxt < len(mn) else None,
                        "next_max": mx[nxt] if nxt is not None and nxt < len(mx) else None,
                    }
                out[c] = {"tz": tzname, "src": per}
        stamp = datetime.now(PKT).strftime("%d %b %Y, %I:%M:%S %p PKT")
        _store()[key] = (out, stamp)
        return out, None, stamp
    except (requests.RequestException, ValueError) as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        reason = "rate limit (429)" if code == 429 else type(e).__name__
        if key in _store():
            o, stamp = _store()[key]
            return o, f"Weather API abhi available nahi ({reason}). Purana data dikha raha hn.", stamp
        return {}, f"Weather API error: {reason}", None


def day_end_pkt(end_iso: str, tzname: str) -> str:
    """End of the market's local day (00:00 next day in the city) shown in PKT."""
    try:
        d = datetime.fromisoformat(end_iso.replace("Z", "+00:00")).date()
        local_end = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(tzname)) + timedelta(days=1)
        return local_end.astimezone(PKT).strftime("%d %b, %I:%M %p")
    except Exception:  # noqa: BLE001
        return "N/A"


# ------------------------------------------------------------- table ------
TD = "border:1px solid rgba(128,128,128,.35);padding:6px 8px;text-align:center;font-size:13px;white-space:nowrap;"
TH = TD + "background:rgba(128,128,128,.18);font-weight:600;"


def build_table(rows: list[dict]) -> str:
    h = [f'<div style="overflow-x:auto"><table style="border-collapse:collapse;width:100%">']
    h.append("<thead>")
    h.append(
        f'<tr><th style="{TH}" rowspan="4">Sr.</th>'
        f'<th style="{TH}" rowspan="4">City with Country</th>'
        f'<th style="{TH}" rowspan="4">Day End Time<br>(PKT)</th>'
        f'<th style="{TH}" rowspan="4">Poly Market</th>'
        f'<th style="{TH}" colspan="{4 * len(SOURCES)}">Temperature °C &amp; °F</th></tr>'
    )
    h.append("<tr>" + "".join(
        f'<th style="{TH}" colspan="4">{html.escape(n)}<br>'
        f'<span style="font-weight:400;font-size:11px">({html.escape(lbl)} model)</span></th>'
        for n, lbl, _ in SOURCES) + "</tr>")
    h.append("<tr>" + "".join(
        f'<th style="{TH}" rowspan="2">Current</th><th style="{TH}" rowspan="2">Max. Recd.</th>'
        f'<th style="{TH}" colspan="2">Exp. Next Day</th>' for _ in SOURCES) + "</tr>")
    h.append("<tr>" + "".join(f'<th style="{TH}">Min</th><th style="{TH}">Max</th>' for _ in SOURCES) + "</tr>")
    h.append("</thead><tbody>")
    for i, r in enumerate(rows, 1):
        cells = [f'<td style="{TD}">{i}</td>',
                 f'<td style="{TD}text-align:left"><b>{html.escape(r["city"])}</b>, {html.escape(r["country"])}</td>',
                 f'<td style="{TD}">{html.escape(r["day_end"])}</td>',
                 f'<td style="{TD}text-align:left"><a href="{html.escape(r["link"])}" target="_blank">{r["poly_html"]}</a></td>']
        for name, _l, _m in SOURCES:
            s = r["src"].get(name, {})
            for k in ("current", "max_rec", "next_min", "next_max"):
                cells.append(f'<td style="{TD}">{temp_cell(s.get(k))}</td>')
        h.append("<tr>" + "".join(cells) + "</tr>")
    h.append("</tbody></table></div>")
    return "".join(h)


# --------------------------------------------------------------- UI --------
st.title("🌡️ Polymarket Temperature Dashboard")

st.sidebar.header("Settings")
auto = st.sidebar.toggle("Auto refresh", value=True)
interval = st.sidebar.select_slider("Refresh interval (sec)", [5, 10, 15, 30, 60], value=10)
if auto:
    st_autorefresh(interval=interval * 1000, key="autorefresh")
lo, hi = st.sidebar.slider("Poly Market filter (%)", 0, 100, (20, 40),
                           help="Sab se zyada probability wale 2 outcomes dono is range mein hon.")
show_all = st.sidebar.checkbox("Filter band karo (sab cities dikhao)", value=False)
if st.sidebar.button("Refresh now"):
    fetch_polymarket.clear()
    st.rerun()

poly, p_err = fetch_polymarket()
if p_err:
    st.warning(p_err)
if poly.empty:
    st.info("Abhi koi Highest-temperature Polymarket market nahi mili.")
    st.stop()

# Filter: top-2 outcomes both inside [lo, hi]
poly["top2"] = poly["buckets"].apply(lambda b: b[:2])
poly["pass"] = poly["top2"].apply(lambda t: len(t) == 2 and all(lo <= p <= hi for _, p in t))
sel = poly if show_all else poly[poly["pass"]]

all_rows = []
for _, r in poly.iterrows():
    loc = locate(r["city"])
    all_rows.append((r, loc))

chosen = [(r, loc) for r, loc in all_rows if (show_all or r["pass"]) and loc]
coords = tuple(sorted({(round(loc["lat"], 4), round(loc["lon"], 4)) for _, loc in chosen}))
weather, w_err, w_stamp = fetch_weather(coords) if coords else ({}, None, None)
if w_err:
    st.warning(w_err)

rows = []
for r, loc in chosen:
    wk = (round(loc["lat"], 4), round(loc["lon"], 4))
    w = weather.get(wk, {"tz": "UTC", "src": {}})
    poly_html = " &middot; ".join(f"<b>{p:.0f}%</b> ({html.escape(t)})" for t, p in r["top2"]) or "N/A"
    rows.append({"city": r["city"], "country": loc["country"],
                 "day_end": day_end_pkt(r["end"], w["tz"]), "link": r["link"],
                 "poly_html": poly_html, "src": w["src"], "lat": loc["lat"], "lon": loc["lon"]})

skipped = [r["city"] for r, loc in all_rows if (show_all or r["pass"]) and not loc]
c1, c2, c3 = st.columns(3)
c1.metric("Polymarket cities", len(poly))
c2.metric(f"Filter pass ({lo}-{hi}%)", int(poly["pass"].sum()))
c3.metric("Weather fetched", w_stamp or "N/A")

tab_t, tab_m, tab_p = st.tabs(["📋 Table", "🗺️ Map", "📈 All Polymarket cities"])
with tab_t:
    if not rows:
        st.info(f"Abhi koi city nahi jis ke top 2 outcomes {lo}% se {hi}% ke beech hon.")
    else:
        st.html(build_table(rows))
        flat = pd.DataFrame([{
            "city": r["city"], "country": r["country"], "day_end_pkt": r["day_end"],
            "polymarket": re.sub("<[^>]+>", "", r["poly_html"].replace("&middot;", "|")),
            **{f"{n}_{k}_c": r["src"].get(n, {}).get(k) for n, _, _ in SOURCES
               for k in ("current", "max_rec", "next_min", "next_max")}} for r in rows])
        st.download_button("Download CSV", flat.to_csv(index=False).encode(), "temperature_dashboard.csv", "text/csv")
    if skipped:
        st.caption("Location nahi mili (skip): " + ", ".join(skipped))
    st.caption("Weather columns Open-Meteo ke 4 alag models se hain (ECMWF, NOAA GFS, DWD ICON, CMA); "
               "ye un websites ke apne published numbers nahi hain. Max. Recd. = aaj ka ab tak ka "
               "model-based maximum (city ke local din ka).")

with tab_m:
    if rows:
        mdf = pd.DataFrame([{"city": r["city"], "lat": r["lat"], "lon": r["lon"],
                             "temp": (r["src"].get(SOURCES[0][0], {}) or {}).get("current")} for r in rows])
        fig = px.scatter_geo(mdf, lat="lat", lon="lon", hover_name="city", color="temp",
                             color_continuous_scale="RdYlBu_r", projection="natural earth")
        fig.update_traces(marker={"size": 11})
        fig.update_layout(margin={"r": 0, "t": 0, "l": 0, "b": 0}, coloraxis_colorbar={"title": "°C"})
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("Map ke liye koi city nahi.")

with tab_p:
    show = pd.DataFrame({
        "city": poly["city"], "ends (UTC)": poly["end"].str[:16],
        "top 1": poly["top2"].apply(lambda t: f"{t[0][0]}: {t[0][1]:.0f}%" if len(t) > 0 else ""),
        "top 2": poly["top2"].apply(lambda t: f"{t[1][0]}: {t[1][1]:.0f}%" if len(t) > 1 else ""),
        "filter pass": poly["pass"], "link": poly["link"]})
    st.dataframe(show, width="stretch", hide_index=True,
                 column_config={"link": st.column_config.LinkColumn("Open", display_text="Polymarket")})
