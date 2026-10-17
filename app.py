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
METAR_URL = "https://aviationweather.gov/api/data/metar"   # NOAA: asli airport observations
METAR_TTL = 120        # seconds
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


# Agar Polymarket event mein station na likha ho to ye airport (ICAO) use hoga.
# Polymarket ka apna station hamesha pehle aata hai.
FALLBACK_ICAO = {
    "london": "EGLC", "paris": "LFPG", "wellington": "NZWN", "munich": "EDDM", "tokyo": "RJTT",
    "hong kong": "VHHH", "shanghai": "ZSPD", "singapore": "WSSS", "beijing": "ZBAA",
    "moscow": "UUWW", "amsterdam": "EHAM", "helsinki": "EFHK", "nyc": "KLGA",
    "denver": "KBKF", "sao paulo": "SBGR", "buenos aires": "SAEZ",
}
STATION_RE = re.compile(r"site=([A-Za-z0-9]{4})\b")


def norm_city(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().strip()
    return CITY_ALIASES.get(n, n)

# Sirf Met Website ke tomorrow / day-after forecast ke liye (Open-Meteo Best match, airport ki coordinates par).
OM_SOURCES = [("Open-Meteo.com", "Best match", "best_match")]
# Table mein dikhne wali 2 websites
SITES = [
    ("wunderground.com", "https://www.wunderground.com/"),
]
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
            try:   # khatam ho chuka (endDate guzar gaya) market chhod do: uska top outcome ~95%+ hota hai, filter fail
                if datetime.fromisoformat(end.replace("Z", "+00:00")) <= now:
                    continue
            except ValueError:
                pass
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
            src_txt = (ev.get("resolutionSource") or "") + " " + (ev.get("description") or "")
            mm = STATION_RE.search(src_txt)
            best[city] = {"city": city, "end": end, "buckets": buckets,
                          "icao": mm.group(1).upper() if mm else "",
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


@st.cache_data(ttl=METAR_TTL, show_spinner=False)
def fetch_metar(icaos: tuple) -> tuple[dict, str | None]:
    """Asli airport readings (NOAA METAR). {ICAO: {name, lat, lon, obs:[(epoch, tempC)]}}"""
    key = ("metar", icaos)
    try:
        r = get_retry(METAR_URL, {"ids": ",".join(icaos), "format": "json", "hours": 36})
        data = r.json() if r.content else []
        out = {}
        for rec in data:
            ic, t, ot = rec.get("icaoId"), rec.get("temp"), rec.get("obsTime")
            if not ic or t is None or ot is None:
                continue
            d = out.setdefault(ic, {"name": rec.get("name", ""), "lat": rec.get("lat"),
                                    "lon": rec.get("lon"), "obs": []})
            d["obs"].append((int(ot), float(t)))
        for d in out.values():
            d["obs"].sort()
        _store()[key] = out
        return out, None
    except (requests.RequestException, ValueError) as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        msg = f"Airport (METAR) API error ({code or type(e).__name__})"
        if key in _store():
            return _store()[key], msg + " - purana data dikha raha hn."
        return {}, msg


@st.cache_data(ttl=WEATHER_TTL, show_spinner=False)
def fetch_weather(coords: tuple) -> tuple[dict, str | None, str | None]:
    """coords = ((lat, lon), ...). Returns ({(lat,lon): info}, error, stamp)."""
    key = ("w", coords)
    models = ",".join(s[2] for s in OM_SOURCES)
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
                for name, _lbl, model in OM_SOURCES:
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
CUR_BG = ["#d6ecff", "#d9f5dc", "#fff4c2"]
MET_CUR_BG = "#ffe8cc"  # light orange for Met Website "Current"

# Frozen (sticky) left columns: widths in px, left offsets are cumulative
FROZEN_W = [46, 150, 95, 115]
FROZEN_L = [0, 46, 196, 291]
# Header rows: heights in px, sticky top offsets are cumulative
ROW_H = [36, 54, 58]
ROW_T = [0, 36, 90]

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
    total_w = sum(FROZEN_W) + 4 * 105 + len(SITES) * 4 * 100
    h = [f'<div style="overflow:auto;max-height:78vh;border:1px solid #b8bec6;border-radius:4px;background:#fff">'
         f'<table style="border-collapse:separate;border-spacing:0;min-width:{total_w}px;width:100%">']
    h.append("<thead>")

    def th(txt, extra="", **kw):
        attrs = " ".join(f'{k}="{v}"' for k, v in kw.items())
        return f'<th {attrs} style="{BASE}{HEAD_BG}{extra}">{txt}</th>'

    sticky = lambda row: f"position:sticky;top:{ROW_T[row]}px;z-index:4;"

    # Row 1: 4 frozen headers (span 3 rows) + 2 group headers
    r1 = "".join(
        th(t, _frozen_style(i, True) + f"height:{sum(ROW_H)}px;", rowspan=3)
        for i, t in enumerate(["Sr.", "City with Country", "Day End Time<br>(PKT)", "Poly Market"]))
    r1 += th("Met Website", sticky(0) + f"height:{ROW_H[0]}px;", colspan=4)
    r1 += th("Temperature °C &amp; °F", sticky(0) + f"height:{ROW_H[0]}px;", colspan=len(SITES) * 4)
    h.append(f"<tr>{r1}</tr>")

    # Row 2: Met Website sub-headers (span rows 2-3) + the 2 site names
    hh = ROW_H[1] + ROW_H[2]
    r2 = "".join(
        th(t, sticky(1) + f"height:{hh}px;width:105px;min-width:105px;"
              f"{'background:' + MET_CUR_BG + ';' if i == 0 else ''}", rowspan=2)
        for i, t in enumerate(("Current", "Today<br>Max. Recd.<br>Temp.", "Tomorrow<br>Max. Recd.<br>Temp.",
                               "Day After<br>Tomorrow<br>Max. Recd. Temp.")))
    r2 += "".join(
        th(f'<a href="{url}" target="_blank" rel="noopener" style="color:#0b3d91">{html.escape(nm)}</a><br>'
           f'<span style="font-weight:400;font-size:11px">'
           f'(apna data nahi: sirf link)</span>',
           sticky(1) + f"height:{ROW_H[1]}px;", colspan=4)
        for nm, url in SITES)
    h.append(f"<tr>{r2}</tr>")

    # Row 3: Current / Max. Recd. under each website
    r3 = ""
    for i, _ in enumerate(SITES):
        r3 += th("Current", sticky(2) + f"height:{ROW_H[2]}px;width:100px;min-width:100px;background:{CUR_BG[i % len(CUR_BG)]};")
        r3 += th("Max. Recd.", sticky(2) + f"height:{ROW_H[2]}px;width:100px;min-width:100px;")
        r3 += th("Tomorrow<br>Max. Recd.<br>Temp.", sticky(2) + f"height:{ROW_H[2]}px;width:100px;min-width:100px;")
        r3 += th("Day After<br>Tomorrow<br>Max. Recd. Temp.", sticky(2) + f"height:{ROW_H[2]}px;width:110px;min-width:110px;")
    h.append(f"<tr>{r3}</tr>")
    h.append("</thead><tbody>")

    for i, r in enumerate(rows, 1):
        row_bg = "background:#ebebeb;" if i % 2 == 0 else BODY_BG  # even rows light gray, odd white

        def fz(idx, content, left_align=False):
            al = "text-align:left;" if left_align else ""
            return (f'<td style="{BASE}{row_bg}{_frozen_style(idx, False)}{al}white-space:nowrap;">{content}</td>')
        cells = [
            fz(0, str(i)),
            fz(1, f'<b>{html.escape(r["city"])}</b><br>{html.escape(r["country"])}'
                  + (f'<br><span style="font-size:11px;color:#444">{html.escape(r["icao"])}'
                     f'{" · " + html.escape(r["stn_name"][:16]) if r["stn_name"] else ""}</span>' if r["icao"] else ""), True),
            fz(2, r["day_end"]),
            fz(3, f'<a href="{html.escape(r["link"])}" target="_blank" rel="noopener" style="color:#0b3d91">{r["poly_html"]}</a>', True),
        ]
        om = r["src"].get("Open-Meteo.com", {})
        metar_url = f"https://aviationweather.gov/data/metar/?ids={r['icao']}&hours=24"
        wunderground_url = f"https://www.wunderground.com/history/daily/{r['icao']}"

        def observed(val, url, note):
            """Airport METAR reading; METAR na mile to model value + '(model)'."""
            if r["is_metar"]:
                return temp_cell(val, url, note)
            return temp_cell(val, r["met_url"], "METAR available nahi: Open-Meteo model value") \
                + ('<br><span style="font-size:10px;color:#555">(model)</span>' if val is not None else "")

        # Met Website: Current, Today Max. Recd. (METAR), Tomorrow, Day After (forecast)
        for k in ("current", "max_rec", "high_d1", "high_d2"):
            if k in ("current", "max_rec"):
                cell = observed(r["cur_v"] if k == "current" else r["max_v"], metar_url,
                                f"{r['icao']} airport METAR (NOAA) - last obs {r['obs_note']}")
            else:
                cell = temp_cell(om.get(k), source_url(r["lat"], r["lon"], "best_match"),
                                 "Forecast max: Open-Meteo Best match model, airport coordinates (met service ka apna forecast nahi)")
            cell = cell.replace("<a href", '<a style="color:#0b3d91" href')
            bg = f"background:{MET_CUR_BG};font-weight:600;" if k == "current" else row_bg
            cells.append(f'<td style="{BASE}{bg}white-space:nowrap;">{cell}</td>')

        # wunderground.com: apna data hamare paas nahi -> N/A + station page ka link
        for si, (nm, _home) in enumerate(SITES):
            na = (f'<a style="color:#0b3d91" href="{html.escape(wunderground_url)}" target="_blank" rel="noopener" '
                  f'title="Weather Underground page kholein (data dashboard tak nahi aata)">N/A</a>'
                  f'<br><span style="font-size:10px;color:#555">(page kholein)</span>')
            for k in ("current", "max_rec", "high_d1", "high_d2"):
                bg = f"background:{CUR_BG[si % len(CUR_BG)]};font-weight:600;" if k == "current" else row_bg
                cells.append(f'<td style="{BASE}{bg}white-space:nowrap;">{na}</td>')
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
show_all = st.sidebar.checkbox("% filter off karo (chuni hui sab cities dikhao)", value=False)
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
    help="Sirf yehi cities dikhengi, aur un par upar wala % filter bhi lagega. "
         "Khali chhodein to Polymarket ki sab cities par % filter lagega.")

# Filter: top-2 outcomes both inside [lo, hi]
poly["top2"] = poly["buckets"].apply(lambda b: b[:2])
poly["pass"] = poly["top2"].apply(lambda t: len(t) == 2 and all(lo <= p <= hi for _, p in t))

def keep(r) -> bool:
    in_list = (r["city"] in picked) if picked else True       # city list
    in_band = show_all or bool(r["pass"])                       # % filter
    return in_list and in_band


all_rows = []
for _, r in poly.iterrows():
    all_rows.append((r, locate(r["city"])))


def icao_for(r) -> str:
    return (str(r.get("icao") or "")).upper() or FALLBACK_ICAO.get(norm_city(r["city"]), "")


selected = [(r, loc) for r, loc in all_rows if keep(r)]
icaos = tuple(sorted({icao_for(r) for r, _ in selected if icao_for(r)}))
metar, m_err = fetch_metar(icaos) if icaos else ({}, None)
if m_err:
    st.warning(m_err)

chosen = []   # (poly row, location, METAR station or None, icao)
for r, loc in selected:
    ic = icao_for(r)
    stn = metar.get(ic)
    if stn and stn.get("lat") is not None:   # forecast bhi airport ki coordinates par
        cc = stn["name"].split(",")[-1].strip() if stn.get("name") else ""
        loc = {"country": (loc or {}).get("country") or cc, "lat": stn["lat"], "lon": stn["lon"]}
    if loc:
        chosen.append((r, loc, stn, ic))

coords = tuple(sorted({(round(loc["lat"], 4), round(loc["lon"], 4)) for _, loc, _, _ in chosen}))
weather, w_err, w_stamp = fetch_weather(coords) if coords else ({}, None, None)
if w_err:
    st.warning(w_err)

rows = []
for r, loc, stn, ic in chosen:
    wk = (round(loc["lat"], 4), round(loc["lon"], 4))
    w = weather.get(wk, {"tz": "UTC", "src": {}})
    poly_html = "<br>".join(f"<b>{p:.0f}%</b> ({html.escape(t)})" for t, p in r["top2"]) or "N/A"
    om = w["src"].get("Open-Meteo.com", {})
    # Asli airport reading (METAR): current + local-day max so far
    cur_v, max_v, obs_note = om.get("current"), om.get("max_rec"), ""
    is_metar = bool(stn and stn["obs"])
    if is_metar:
        try:
            zi = ZoneInfo(w["tz"])
        except Exception:  # noqa: BLE001
            zi = timezone.utc
        today_local = datetime.now(zi).date()
        todays = [t for ep, t in stn["obs"] if datetime.fromtimestamp(ep, zi).date() == today_local]
        cur_v = stn["obs"][-1][1]
        max_v = max(todays) if todays else None
        obs_note = datetime.fromtimestamp(stn["obs"][-1][0], PKT).strftime("%d %b %I:%M %p PKT")
    rows.append({"city": r["city"], "country": loc["country"],
                 "day_end": day_end_pkt(r["end"], w["tz"]), "link": r["link"],
                 "poly_html": poly_html, "src": w["src"], "lat": loc["lat"], "lon": loc["lon"],
                 "icao": ic, "stn_name": (stn["name"].split(",")[0] if stn else ""),
                 "is_metar": is_metar, "obs_note": obs_note, "cur_v": cur_v, "max_v": max_v,
                 "met_name": met_site(r["city"], loc["country"])[0],
                 "met_url": met_site(r["city"], loc["country"])[1]})

skipped = [r["city"] for r, loc in all_rows if keep(r) and not loc and not metar.get(icao_for(r))]
c1, c2, c3 = st.columns(3)
c1.metric("Polymarket cities", len(poly))
pool = poly[poly["city"].isin(picked)] if picked else poly
c2.metric(f"Filter pass ({lo}-{hi}%)", f"{int(pool['pass'].sum())} / {len(pool)}")
c3.metric("Weather fetched", w_stamp or "N/A")

tab_t, tab_m, tab_p = st.tabs(["📋 Table", "🗺️ Map", "📈 All Polymarket cities"])
with tab_t:
    if not rows:
        st.info(f"Chuni hui cities mein abhi koi aisi nahi jis ke top 2 outcomes {lo}% se {hi}% ke beech hon. "
                "Slider ki range badhayein ya sidebar mein '% filter off' tick karein." if picked else
                f"Abhi koi city nahi jis ke top 2 outcomes {lo}% se {hi}% ke beech hon.")
    else:
        st.html(build_table(rows))
        flat = pd.DataFrame([{
            "city": r["city"], "country": r["country"],
            "met_website": r["met_url"],
            "airport_icao": r["icao"], "airport_reading_is_metar": r["is_metar"],
            "airport_current_c": r["cur_v"], "airport_today_max_recd_c": r["max_v"],
            "met_tomorrow_max_c": r["src"].get("Open-Meteo.com", {}).get("high_d1"),
            "met_day_after_max_c": r["src"].get("Open-Meteo.com", {}).get("high_d2"),
            "polymarket": re.sub("<[^>]+>", "", r["poly_html"].replace("<br>", " | ")),
            "day_end_pkt": re.sub("<[^>]+>", " ", r["day_end"])} for r in rows])
        st.download_button("Download CSV", flat.to_csv(index=False).encode(), "temperature_dashboard.csv", "text/csv")
    if skipped:
        st.caption("Location nahi mili (skip): " + ", ".join(skipped))
    st.caption("DATA SOURCE (Met Website columns): Current aur Today Max. Recd. = us city ke Polymarket resolution airport (ICAO code city ke neeche likha hai) "
               "ki NOAA METAR reading (aviationweather.gov feed); click par wahi METAR page khulta hai. Max. Recd. = local din ka ab tak ka maximum. "
               "Tomorrow / Day After Tomorrow abhi record nahi hue, is liye forecast max hain: Open-Meteo 'Best match' model, airport ki coordinates par "
               "(click par wahi Open-Meteo request khulti hai). Ye us desh ki met service ka apna data/forecast nahi hai. "
               "METAR na mile to value ke neeche '(model)' likha aata hai. 'wunderground.com' ka apna data dashboard tak nahi aata (API key chahiye): N/A + page link.")

with tab_m:
    if rows:
        mdf = pd.DataFrame([{"city": r["city"], "lat": r["lat"], "lon": r["lon"],
                             "temp": r["cur_v"]} for r in rows])
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
