"""Polymarket Temperature Dashboard (5 weather sources).

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
import unicodedata
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

# Agar yahan cities likh dein to dashboard hamesha shuru se sirf inhi ko dikhayega.
# Khali [] ka matlab: sidebar se choose karein (ya filter ke hisab se sab).
# Misal: DEFAULT_CITIES = ["London", "Karachi", "Seoul"]
DEFAULT_CITIES: list[str] = [
    "London", "Paris", "Wellington", "Munich", "Tokyo", "Hong Kong", "Shanghai",
    "Singapore", "Beijing", "Moscow", "Amsterdam", "Helsinki", "NYC", "Denver",
    "Sao Paulo", "Buenos Aires",
]

# Alag spelling wale naam ek jaise samjhe jayen (accents aur case ignore hote hain)
CITY_ALIASES = {"new york": "nyc", "new york city": "nyc", "sao paulo": "sao paulo"}
# Location dhoondne ke liye (geocoding) behtar naam
GEO_ALIASES = {"NYC": "New York", "nyc": "New York"}


# Har country/city ki national met (weather) service ki website. Naya add karna ho to bas ek line.
# City-specific override pehle check hota hai, phir country, aur phir WMO ka fallback.
MET_BY_CITY = {
    "nyc": ("NWS New York", "https://www.weather.gov/okx/"),
    "denver": ("NWS Boulder", "https://www.weather.gov/bou/"),
    "hong kong": ("Hong Kong Observatory", "https://www.hko.gov.hk/en/index.html"),
    "wellington": ("MetService", "https://www.metservice.com/"),
    "munich": ("DWD", "https://www.dwd.de/EN/Home/home_node.html"),
    "shanghai": ("CMA", "https://english.cma.gov.cn/"),
    "beijing": ("CMA", "https://english.cma.gov.cn/"),
    "moscow": ("Hydrometcenter", "https://meteoinfo.ru/en/"),
    "helsinki": ("FMI", "https://en.ilmatieteenlaitos.fi/"),
    "sao paulo": ("INMET", "https://portal.inmet.gov.br/"),
    "buenos aires": ("SMN Argentina", "https://www.smn.gob.ar/"),
}
MET_BY_COUNTRY = {
    "united kingdom": ("Met Office", "https://www.metoffice.gov.uk/"),
    "france": ("Meteo-France", "https://meteofrance.com/"),
    "new zealand": ("MetService", "https://www.metservice.com/"),
    "germany": ("DWD", "https://www.dwd.de/EN/Home/home_node.html"),
    "japan": ("JMA", "https://www.jma.go.jp/jma/indexe.html"),
    "china": ("CMA", "https://english.cma.gov.cn/"),
    "singapore": ("MSS", "https://www.weather.gov.sg/"),
    "russia": ("Hydrometcenter", "https://meteoinfo.ru/en/"),
    "netherlands": ("KNMI", "https://www.knmi.nl/home"),
    "finland": ("FMI", "https://en.ilmatieteenlaitos.fi/"),
    "united states": ("NWS", "https://www.weather.gov/"),
    "usa": ("NWS", "https://www.weather.gov/"),
    "brazil": ("INMET", "https://portal.inmet.gov.br/"),
    "argentina": ("SMN Argentina", "https://www.smn.gob.ar/"),
    "south korea": ("KMA", "https://www.kma.go.kr/eng/index.jsp"),
    "pakistan": ("PMD", "https://www.pmd.gov.pk/"),
    "india": ("IMD", "https://mausam.imd.gov.in/"),
    "turkey": ("MGM", "https://www.mgm.gov.tr/eng/forecast-cities.aspx"),
    "turkiye": ("MGM", "https://www.mgm.gov.tr/eng/forecast-cities.aspx"),
    "united arab emirates": ("NCM", "https://www.ncm.gov.ae/"),
    "uae": ("NCM", "https://www.ncm.gov.ae/"),
    "canada": ("Environment Canada", "https://weather.gc.ca/"),
    "australia": ("BoM", "http://www.bom.gov.au/"),
    "israel": ("IMS", "https://ims.gov.il/en"),
    "spain": ("AEMET", "https://www.aemet.es/en/portada"),
    "italy": ("Aeronautica Militare", "https://www.meteoam.it/"),
    "poland": ("IMGW", "https://www.imgw.pl/"),
    "mexico": ("SMN Mexico", "https://smn.conagua.gob.mx/"),
    "taiwan": ("CWA", "https://www.cwa.gov.tw/eng/"),
    "thailand": ("TMD", "https://www.tmd.go.th/en"),
    "malaysia": ("MetMalaysia", "https://www.met.gov.my/en/"),
    "philippines": ("PAGASA", "https://www.pagasa.dost.gov.ph/"),
    "south africa": ("SAWS", "https://www.weathersa.co.za/"),
    "saudi arabia": ("NCM", "https://ncm.gov.sa/"),
    "indonesia": ("BMKG", "https://www.bmkg.go.id/"),
    "chile": ("Meteo Chile", "https://www.meteochile.gob.cl/"),
    "peru": ("SENAMHI", "https://www.senamhi.gob.pe/"),
    "colombia": ("IDEAM", "http://www.ideam.gov.co/"),
}
MET_FALLBACK = ("WMO World Weather", "https://worldweather.wmo.int/en/home.html")


def met_site(city: str, country: str) -> tuple[str, str]:
    c = norm_city(city)
    if c in MET_BY_CITY:
        return MET_BY_CITY[c]
    return MET_BY_COUNTRY.get(norm_city(country or ""), MET_FALLBACK)


def norm_city(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().strip()
    return CITY_ALIASES.get(n, n)

# Column group -> Open-Meteo model.  These are the *models* closest to each
# site's data source; they are NOT the sites' own published numbers.
SOURCES = [
    ("Windy.com", "ECMWF", "ecmwf_ifs025"),
    ("Weather.gov", "NOAA GFS", "gfs_seamless"),
    ("Wunderground.com", "DWD ICON", "icon_seamless"),
    ("Weather.com", "CMA", "cma_grapes_global"),
    ("Open-Meteo.com", "Best match", "best_match"),
]
SITE_HOME = {
    "Windy.com": "https://www.windy.com/",
    "Weather.gov": "https://www.weather.gov/",
    "Wunderground.com": "https://www.wunderground.com/",
    "Weather.com": "https://en.weather.com.cn/",
    "Open-Meteo.com": "https://open-meteo.com/",
}
TITLE_RE = re.compile(r"^Highest temperature in (.+?) on (.+?)\?*$", re.I)


# ------------------------------------------------------------ helpers -----
def c_to_f(c):
    return None if c is None or pd.isna(c) else c * 9 / 5 + 32


def source_url(lat, lon, model: str) -> str:
    """Exact Open-Meteo API request that the numbers in this column came from."""
    return (f"{METEO_URL}?latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m&daily=temperature_2m_max,temperature_2m_min"
            f"&models={model}&forecast_days=3&timezone=auto")


def temp_cell(c, url: str = "", title: str = "Data source (Open-Meteo API)") -> str:
    if c is None or pd.isna(c):
        return "N/A"
    txt = f"{c:.1f}°C<br>{c_to_f(c):.1f}°F"
    if url:
        return f'<a href="{html.escape(url)}" target="_blank" rel="noopener" title="{html.escape(title)}">{txt}</a>'
    return txt


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
    city = GEO_ALIASES.get(city, city)
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
                "models": models, "forecast_days": 3, "timezone": "auto",
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
                    vals = hourly.get(f"temperature_2m_{model}") or hourly.get("temperature_2m") or []
                    cur, past = None, []
                    for t, v in zip(times, vals):
                        if v is None or not t.startswith(today) or t > now_h:
                            continue
                        past.append(v)
                        if t == now_h:
                            cur = v
                    if cur is None and past:
                        cur = past[-1]
                    mx = daily.get(f"temperature_2m_max_{model}") or daily.get("temperature_2m_max") or []
                    mn = daily.get(f"temperature_2m_min_{model}") or daily.get("temperature_2m_min") or []
                    nxt = dtimes.index(
                        (now_local + timedelta(days=1)).strftime("%Y-%m-%d")
                    ) if (now_local + timedelta(days=1)).strftime("%Y-%m-%d") in dtimes else None
                    def dmax(off):
                        k = (now_local + timedelta(days=off)).strftime("%Y-%m-%d")
                        i = dtimes.index(k) if k in dtimes else None
                        return mx[i] if i is not None and i < len(mx) else None
                    d0 = dmax(0)
                    if past:  # today's highest = max(forecast daily max, already recorded)
                        d0 = max(past) if d0 is None else max(d0, max(past))
                    per[name] = {
                        "high_d0": d0, "high_d1": dmax(1), "high_d2": dmax(2),
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
        dt = local_end.astimezone(PKT)
        return f"{dt:%d %b %Y}<br>{dt:%I:%M %p}"
    except Exception:  # noqa: BLE001
        return "N/A"


# ------------------------------------------------------------- table ------
# Light background per source for the "Current" columns
CUR_BG = ["#d6ecff", "#d9f5dc", "#fff4c2", "#ffdfe6", "#e8dcff"]
MET_CUR_BG = "#ffe8cc"  # light orange for Met Website "Current"

# Frozen (sticky) left columns: widths in px, left offsets are cumulative
FROZEN_W = [46, 125, 95, 115]
FROZEN_L = [0, 46, 171, 266]
# Header rows: heights in px, sticky top offsets are cumulative
ROW_H = [36, 54, 36, 36]
ROW_T = [0, 36, 90, 126]

BASE = ("box-sizing:border-box;border-right:1px solid #b8bec6;border-bottom:1px solid #b8bec6;"
        "padding:4px 8px;text-align:center;font-size:13px;color:#111;")
BODY_BG = "background:#ffffff;"
HEAD_BG = "background:#e3e7ec;font-weight:600;"


def _frozen_style(i: int, header: bool) -> str:
    w, left = FROZEN_W[i], FROZEN_L[i]
    st_ = (f"position:sticky;left:{left}px;top:{0 if header else 'auto'}{'px' if header else ''};"
           f"width:{w}px;min-width:{w}px;max-width:{w}px;z-index:{6 if header else 3};")
    return st_


def build_table(rows: list[dict]) -> str:
    n = len(SOURCES)
    total_w = sum(FROZEN_W) + n * 4 * 92
    h = [f'<div style="overflow:auto;max-height:78vh;border:1px solid #b8bec6;border-radius:4px;background:#fff">'
         f'<table style="border-collapse:separate;border-spacing:0;min-width:{total_w}px;width:100%">']
    h.append("<thead>")

    def th(txt, extra="", **kw):
        attrs = " ".join(f'{k}="{v}"' for k, v in kw.items())
        return f'<th {attrs} style="{BASE}{HEAD_BG}{extra}">{txt}</th>'

    # Row 1: 4 frozen rowspan headers + group header
    r1 = "".join(
        th(t, _frozen_style(i, True) + f"height:{sum(ROW_H)}px;", rowspan=4)
        for i, t in enumerate(["Sr.", "City with Country", "Day End Time<br>(PKT)", "Poly Market"]))
    r1 += th("Met Website", f"position:sticky;top:{ROW_T[0]}px;z-index:4;height:{ROW_H[0]}px;", colspan=4)
    r1 += th("Temperature °C &amp; °F",
             f"position:sticky;top:{ROW_T[0]}px;z-index:4;height:{ROW_H[0]}px;", colspan=4 * n)
    h.append(f"<tr>{r1}</tr>")

    # Row 2: 3 "highest temperature" headers (span rows 2-4), then site names
    hh = ROW_H[1] + ROW_H[2] + ROW_H[3]
    r2 = "".join(
        th(t, f"position:sticky;top:{ROW_T[1]}px;z-index:4;height:{hh}px;width:105px;min-width:105px;"
              f"{'background:' + MET_CUR_BG + ';' if i == 0 else ''}", rowspan=3)
        for i, t in enumerate(("Current", "Today<br>Max. Recd.<br>Temp.", "Tomorrow<br>Max. Recd.<br>Temp.",
                               "Day After<br>Tomorrow<br>Max. Recd. Temp.")))
    r2 += "".join(
        th(f'<a href="{SITE_HOME[nm]}" target="_blank" rel="noopener" style="color:#0b3d91">{html.escape(nm)}</a><br>'
           f'<span style="font-weight:400;font-size:11px">({html.escape(lbl)} model)</span>',
           f"position:sticky;top:{ROW_T[1]}px;z-index:4;height:{ROW_H[1]}px;", colspan=4)
        for nm, lbl, _ in SOURCES)
    h.append(f"<tr>{r2}</tr>")

    # Row 3: Current / Max. Recd. / Exp. Next Day
    r3 = ""
    for i, _ in enumerate(SOURCES):
        r3 += th("Current", f"position:sticky;top:{ROW_T[2]}px;z-index:4;height:{ROW_H[2] + ROW_H[3]}px;"
                            f"background:{CUR_BG[i % len(CUR_BG)]};", rowspan=2)
        r3 += th("Max. Recd.", f"position:sticky;top:{ROW_T[2]}px;z-index:4;height:{ROW_H[2] + ROW_H[3]}px;", rowspan=2)
        r3 += th("Exp. Next Day", f"position:sticky;top:{ROW_T[2]}px;z-index:4;height:{ROW_H[2]}px;", colspan=2)
    h.append(f"<tr>{r3}</tr>")

    # Row 4: Min / Max
    r4 = "".join(th(t, f"position:sticky;top:{ROW_T[3]}px;z-index:4;height:{ROW_H[3]}px;")
                  for _ in SOURCES for t in ("Min", "Max"))
    h.append(f"<tr>{r4}</tr>")
    h.append("</thead><tbody>")

    for i, r in enumerate(rows, 1):
        row_bg = "background:#ebebeb;" if i % 2 == 0 else BODY_BG  # even rows light gray, odd white

        def fz(idx, content, left_align=False):
            al = "text-align:left;" if left_align else ""
            return (f'<td style="{BASE}{row_bg}{_frozen_style(idx, False)}{al}'
                    f'{"white-space:nowrap;" if idx != 3 else "white-space:nowrap;"}">{content}</td>')
        cells = [
            fz(0, str(i)),
            fz(1, f'<b>{html.escape(r["city"])}</b><br>{html.escape(r["country"])}', True),
            fz(2, r["day_end"]),
            fz(3, f'<a href="{html.escape(r["link"])}" target="_blank" rel="noopener" style="color:#0b3d91">{r["poly_html"]}</a>', True),
        ]
        om = r["src"].get("Open-Meteo.com", {})
        for k in ("current", "max_rec", "high_d1", "high_d2"):
            cell = temp_cell(om.get(k), r["met_url"], f"{r['met_name']} (national met service) - click to open")
            cell = cell.replace("<a href", '<a style="color:#0b3d91" href')
            bg = f"background:{MET_CUR_BG};font-weight:600;" if k == "current" else row_bg
            cells.append(f'<td style="{BASE}{bg}white-space:nowrap;">{cell}</td>')
        for si, (name, _l, model) in enumerate(SOURCES):
            src = r["src"].get(name, {})
            url = source_url(r["lat"], r["lon"], model)
            for k in ("current", "max_rec", "next_min", "next_max"):
                bg = (f"background:{CUR_BG[si % len(CUR_BG)]};font-weight:600;" if k == "current" else row_bg)
                cell = temp_cell(src.get(k), url).replace("<a href", '<a style="color:#0b3d91" href')
                cells.append(f'<td style="{BASE}{bg}white-space:nowrap;">{cell}</td>')
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

# City picker: sirf choose ki hui cities ka data (khali = filter ke hisab se sab)
city_options = sorted(poly["city"].unique())
picked = st.sidebar.multiselect(
    "Cities select karein", city_options,
    default=[c for c in city_options if norm_city(c) in {norm_city(x) for x in DEFAULT_CITIES}],
    help="Jo cities yahan choose karenge sirf unka data dikhega (percentage filter ke bina). "
         "Khali chhodein to upar wale % filter ke hisab se cities dikhengi.")

# Filter: top-2 outcomes both inside [lo, hi]
poly["top2"] = poly["buckets"].apply(lambda b: b[:2])
poly["pass"] = poly["top2"].apply(lambda t: len(t) == 2 and all(lo <= p <= hi for _, p in t))

def keep(r) -> bool:
    return (r["city"] in picked) if picked else (show_all or bool(r["pass"]))


all_rows = []
for _, r in poly.iterrows():
    loc = locate(r["city"])
    all_rows.append((r, loc))

chosen = [(r, loc) for r, loc in all_rows if keep(r) and loc]
coords = tuple(sorted({(round(loc["lat"], 4), round(loc["lon"], 4)) for _, loc in chosen}))
weather, w_err, w_stamp = fetch_weather(coords) if coords else ({}, None, None)
if w_err:
    st.warning(w_err)

rows = []
for r, loc in chosen:
    wk = (round(loc["lat"], 4), round(loc["lon"], 4))
    w = weather.get(wk, {"tz": "UTC", "src": {}})
    poly_html = "<br>".join(f"<b>{p:.0f}%</b> ({html.escape(t)})" for t, p in r["top2"]) or "N/A"
    rows.append({"city": r["city"], "country": loc["country"],
                 "day_end": day_end_pkt(r["end"], w["tz"]), "link": r["link"],
                 "poly_html": poly_html, "src": w["src"], "lat": loc["lat"], "lon": loc["lon"],
                 "met_name": met_site(r["city"], loc["country"])[0],
                 "met_url": met_site(r["city"], loc["country"])[1]})

skipped = [r["city"] for r, loc in all_rows if keep(r) and not loc]
c1, c2, c3 = st.columns(3)
c1.metric("Polymarket cities", len(poly))
c2.metric(f"Filter pass ({lo}-{hi}%)", int(poly["pass"].sum()))
c3.metric("Weather fetched", w_stamp or "N/A")

tab_t, tab_m, tab_p = st.tabs(["📋 Table", "🗺️ Map", "📈 All Polymarket cities"])
with tab_t:
    if not rows:
        st.info("Choose ki hui cities ka market abhi nahi mila." if picked else
                f"Abhi koi city nahi jis ke top 2 outcomes {lo}% se {hi}% ke beech hon.")
    else:
        st.html(build_table(rows))
        flat = pd.DataFrame([{
            "city": r["city"], "country": r["country"],
            "met_website": r["met_url"],
            "met_current_c": r["src"].get("Open-Meteo.com", {}).get("current"),
            "met_today_max_recd_c": r["src"].get("Open-Meteo.com", {}).get("max_rec"),
            "met_tomorrow_max_c": r["src"].get("Open-Meteo.com", {}).get("high_d1"),
            "met_day_after_max_c": r["src"].get("Open-Meteo.com", {}).get("high_d2"),
            "polymarket": re.sub("<[^>]+>", "", r["poly_html"].replace("<br>", " | ")),
            "day_end_pkt": re.sub("<[^>]+>", " ", r["day_end"]),
            **{f"{n}_{k}_c": r["src"].get(n, {}).get(k) for n, _, _ in SOURCES
               for k in ("current", "max_rec", "next_min", "next_max")}} for r in rows])
        st.download_button("Download CSV", flat.to_csv(index=False).encode(), "temperature_dashboard.csv", "text/csv")
    if skipped:
        st.caption("Location nahi mili (skip): " + ", ".join(skipped))
    st.caption("Met Website ke 4 columns (Current, Today / Tomorrow / Day After Tomorrow Max. Recd.) Open-Meteo ke Best-match data se hain: "
               "Today = aaj ka ab tak ka recorded max, Tomorrow aur Day After abhi recorded nahi hue to unki forecast max dikhti hai; "
               "temperature par click karne se us city/country ki national met website khulti hai (cross-check ke liye). "
               "Baqi sab weather columns Open-Meteo API ke alag models se hain (ECMWF, NOAA GFS, DWD ICON, CMA, Best match); "
               "ye Windy/Weather.gov/Wunderground/Weather.com ke apne published numbers nahi hain. "
               "Har temperature par click karein to wohi Open-Meteo API page khulega jahan se number liya gaya. "
               "Header mein site ke naam par click se us site ka homepage khulta hai. "
               "Max. Recd. = aaj ka ab tak ka model-based maximum (city ke local din ka).")

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
