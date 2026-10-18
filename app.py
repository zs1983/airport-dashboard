"""Polymarket Temperature Dashboard (Met Website + 5 weather sources).

Run:  pip install -r requirements.txt && streamlit run app.py
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
    "New York", "Chicago", "Miami", "Dallas", "Seattle", "Atlanta", "Los Angeles", "Denver", "Phoenix", "Houston",
    "Boston", "Washington D.C.", "Las Vegas", "San Francisco", "Minneapolis", "Detroit", "Philadelphia", "San Diego",
    "Austin", "Charlotte", "London", "Paris", "Tokyo", "Seoul", "Beijing", "Shanghai", "Munich", "Amsterdam", "Madrid",
    "Rome", "Stockholm", "Ankara", "Toronto", "Sao Paulo", "Singapore", "Lucknow", "Mumbai", "Dubai", "Tel Aviv",
    "Bangkok", "Jakarta", "Cairo", "Mexico City", "Buenos Aires", "Bogota", "Santiago", "Sydney", "Melbourne",
    "Wellington", "Johannesburg", "Honolulu",
]

# Aap ki list ke links (har city ki apni website). Data na mile to "Click" isi link par le jata hai.
LIST_LINKS = {
    **{c: "https://www.weather.gov" for c in (
        "nyc", "chicago", "miami", "dallas", "seattle", "atlanta", "los angeles", "denver", "phoenix", "houston",
        "boston", "washington dc", "las vegas", "san francisco", "minneapolis", "detroit", "philadelphia",
        "san diego", "austin", "charlotte", "honolulu")},
    "london": "https://www.metoffice.gov.uk", "paris": "https://meteofrance.com", "tokyo": "https://www.jma.go.jp",
    "seoul": "https://www.weather.go.kr", "beijing": "https://www.cma.gov.cn", "shanghai": "https://www.wunderground.com",
    "munich": "https://www.dwd.de", "amsterdam": "https://www.knmi.nl", "madrid": "https://www.aemet.es",
    "rome": "https://www.meteoam.it", "stockholm": "https://www.smhi.se", "ankara": "https://www.mgm.gov.tr",
    "toronto": "https://weather.gc.ca", "sao paulo": "https://portal.inmet.gov.br", "singapore": "https://www.weather.gov.sg",
    "lucknow": "https://mausam.imd.gov.in", "mumbai": "https://mausam.imd.gov.in", "dubai": "https://www.ncm.ae",
    "tel aviv": "https://ims.gov.il", "bangkok": "https://www.tmd.go.th", "jakarta": "https://www.bmkg.go.id",
    "cairo": "https://www.ema.gov.eg", "mexico city": "https://smn.conagua.gob.mx", "buenos aires": "https://www.smn.gob.ar",
    "bogota": "http://www.ideam.gov.co", "santiago": "https://meteochile.gob.cl", "sydney": "http://www.bom.gov.au",
    "melbourne": "http://www.bom.gov.au", "wellington": "https://www.metservice.com", "johannesburg": "https://www.weathersa.co.za",
}

# Alag spelling wale naam ek jaise samjhe jayen (accents aur case ignore hote hain)
CITY_ALIASES = {"new york": "nyc", "new york city": "nyc", "sao paulo": "sao paulo",
                "washington d.c.": "washington dc", "washington, d.c.": "washington dc", "washington": "washington dc",
                "washington, dc": "washington dc"}
# Location dhoondne ke liye (geocoding) behtar naam
GEO_ALIASES = {"NYC": "New York", "nyc": "New York", "Washington DC": "Washington", "Washington D.C.": "Washington"}


# Har country/city ki national met (weather) service ki website. Naya add karna ho to bas ek line.
# City-specific override pehle check hota hai, phir country, aur phir WMO ka fallback.
MET_BY_CITY = {
    **{c: ("NWS", "https://www.weather.gov") for c in (
        "chicago", "miami", "dallas", "seattle", "atlanta", "los angeles", "phoenix", "houston", "boston",
        "washington dc", "las vegas", "san francisco", "minneapolis", "detroit", "philadelphia", "san diego",
        "austin", "charlotte", "honolulu")},
    "nyc": ("NWS New York", "https://www.weather.gov/okx/"),
    "denver": ("NWS Boulder", "https://www.weather.gov/bou/"),
    "hong kong": ("Hong Kong Observatory", "https://www.hko.gov.hk/en/index.html"),
    "wellington": ("MetService", "https://www.metservice.com/"),
    "munich": ("DWD", "https://www.dwd.de/EN/weather/weather_climate_local/bavaria/munic/_node.html"),
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
    "chicago": "KORD", "miami": "KMIA", "dallas": "KDAL", "seattle": "KSEA", "atlanta": "KATL", "los angeles": "KLAX",
    "phoenix": "KPHX", "houston": "KHOU", "boston": "KBOS", "washington dc": "KDCA", "las vegas": "KLAS",
    "san francisco": "KSFO", "minneapolis": "KMSP", "detroit": "KDTW", "philadelphia": "KPHL", "san diego": "KSAN",
    "austin": "KAUS", "charlotte": "KCLT", "honolulu": "PHNL", "seoul": "RKSI", "madrid": "LEMD", "rome": "LIRF",
    "stockholm": "ESSA", "ankara": "LTAC", "toronto": "CYYZ", "lucknow": "VILK", "mumbai": "VABB", "dubai": "OMDB",
    "tel aviv": "LLBG", "bangkok": "VTBS", "jakarta": "WIII", "cairo": "HECA", "mexico city": "MMMX",
    "bogota": "SKBO", "santiago": "SCEL", "sydney": "YSSY", "melbourne": "YMML", "johannesburg": "FAOR",
}
STATION_RE = re.compile(r"site=([A-Za-z0-9]{4})\b")


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
    "Weather.gov": "https://aviationweather.gov/data/metar/",
    "Wunderground.com": "https://www.wunderground.com/",
    "Weather.com": "https://en.weather.com.cn/",
    "Open-Meteo.com": "https://open-meteo.com/",
}
AVN = "Weather.gov"   # SOURCES ki ye entry ab 'aviationweather.gov' group ke taur par dikhti hai (METAR data)
TITLE_RE = re.compile(r"^Highest temperature in (.+?) on (.+?)\?*$", re.I)


# ------------------------------------------------------------ helpers -----
def c_to_f(c):
    return None if c is None or pd.isna(c) else c * 9 / 5 + 32


def source_url(lat, lon, model: str) -> str:
    """Exact Open-Meteo API request that the numbers in this column came from."""
    return (f"{METEO_URL}?latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m&daily=temperature_2m_max,temperature_2m_min"
            f"&models={model}&forecast_days=2&timezone=auto")


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


def get_retry(url, params, tries=3, headers=None):
    """GET with backoff on 429/5xx (honours Retry-After)."""
    last = None
    for attempt in range(tries):
        r = requests.get(url, params=params, timeout=TIMEOUT, **({"headers": headers} if headers else {}))
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


# ---------------------------------------------- official met-service data ----
# Sirf in cities ka us desh ki met service se free/bina key ka data milta hai. Baqi cities mein "Click" aata hai.
FMI_URL = "https://opendata.fmi.fi/wfs"
SG_TEMP_URL = "https://api.data.gov.sg/v1/environment/air-temperature"
SG_FC_URL = "https://api.data.gov.sg/v1/environment/4-day-weather-forecast"
HKO_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php"
NWS_UA = {"User-Agent": "polymarket-temperature-dashboard (streamlit)", "Accept": "application/geo+json"}
OFFICIAL = {   # city -> (kind, local tz, extra)
    "helsinki": ("fmi", "Europe/Helsinki", {"fmisid": "100968", "stn": "Helsinki-Vantaa airport (FMI)"}),
    "singapore": ("sg", "Asia/Singapore", {"stn": "Changi (MSS/NEA)"}),
    "hong kong": ("hko", "Asia/Hong_Kong", {"stn": "Chek Lap Kok - HK airport (HKO)"}),
    "nyc": ("nws", "America/New_York", {"icao": "KLGA", "lat": 40.7769, "lon": -73.8740, "stn": "LaGuardia KLGA (NWS)"}),
    "munich": ("dwd", "Europe/Berlin", {"stn": "Munich Airport 10870 (DWD)",
                                        "url": "https://opendata.dwd.de/weather/weather_reports/poi/10870-BEOB.csv"}),
    "denver": ("nws", "America/Denver", {"icao": "KBKF", "lat": 39.7017, "lon": -104.7517, "stn": "Buckley KBKF (NWS)"}),
}


_US = {   # city -> (ICAO, lat, lon, tz)
    "chicago": ("KORD", 41.9786, -87.9048, "America/Chicago"), "miami": ("KMIA", 25.7959, -80.2870, "America/New_York"),
    "dallas": ("KDAL", 32.8471, -96.8518, "America/Chicago"), "seattle": ("KSEA", 47.4502, -122.3088, "America/Los_Angeles"),
    "atlanta": ("KATL", 33.6407, -84.4277, "America/New_York"), "los angeles": ("KLAX", 33.9416, -118.4085, "America/Los_Angeles"),
    "phoenix": ("KPHX", 33.4342, -112.0116, "America/Phoenix"), "houston": ("KHOU", 29.6454, -95.2789, "America/Chicago"),
    "boston": ("KBOS", 42.3656, -71.0096, "America/New_York"), "washington dc": ("KDCA", 38.8512, -77.0402, "America/New_York"),
    "las vegas": ("KLAS", 36.0840, -115.1537, "America/Los_Angeles"), "san francisco": ("KSFO", 37.6213, -122.3790, "America/Los_Angeles"),
    "minneapolis": ("KMSP", 44.8848, -93.2223, "America/Chicago"), "detroit": ("KDTW", 42.2162, -83.3554, "America/Detroit"),
    "philadelphia": ("KPHL", 39.8744, -75.2424, "America/New_York"), "san diego": ("KSAN", 32.7338, -117.1933, "America/Los_Angeles"),
    "austin": ("KAUS", 30.1945, -97.6699, "America/Chicago"), "charlotte": ("KCLT", 35.2140, -80.9431, "America/New_York"),
    "honolulu": ("PHNL", 21.3187, -157.9225, "Pacific/Honolulu"),
}
for _c, (_i, _la, _lo, _tz) in _US.items():
    OFFICIAL[_c] = ("nws", _tz, {"icao": _i, "lat": _la, "lon": _lo, "stn": f"{_i} airport (NWS)"})


def _prep_url(url, params):
    return requests.Request("GET", url, params=params).prepare().url


def _official_one(kind: str, tzname: str, x: dict) -> dict:
    """{'current','max_rec','high_d1','high_d2'} -> (value C or None, source page url, note)."""
    zi = ZoneInfo(tzname)
    now_l = datetime.now(zi)
    d0 = now_l.date()
    d1, d2 = d0 + timedelta(days=1), d0 + timedelta(days=2)
    out: dict = {}
    if kind == "fmi":
        start = datetime.combine(d0, datetime.min.time(), tzinfo=zi).astimezone(timezone.utc)
        prm = {"service": "WFS", "version": "2.0.0", "request": "getFeature",
               "storedquery_id": "fmi::observations::weather::simple", "fmisid": x["fmisid"],
               "parameters": "t2m", "timestep": 10,
               "starttime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "endtime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        r = get_retry(FMI_URL, prm)
        vals = [(t, float(v)) for t, v in re.findall(
            r"<BsWfs:Time>([^<]+)</BsWfs:Time>\s*<BsWfs:ParameterValue>([^<]+)</BsWfs:ParameterValue>", r.text)
            if v.strip().lower() != "nan"]
        if vals:
            u = _prep_url(FMI_URL, prm)
            out["current"] = (vals[-1][1], u, f"{x['stn']} - last obs {vals[-1][0]}")
            out["max_rec"] = (max(v for _, v in vals), u, f"{x['stn']} - max since local midnight")
    elif kind == "dwd":
        r = get_retry(x["url"], None)
        lines = r.content.decode("utf-8", errors="replace").splitlines()
        col = lines[0].split(";").index("dry_bulb_temperature_at_2_meter_above_ground")
        obs = []
        for ln in lines[3:]:   # line 2 = units, line 3 = German names
            f = ln.split(";")
            if len(f) <= col or f[col].strip() in ("", "---"):
                continue
            try:
                t = datetime.strptime(f[0] + " " + f[1], "%d.%m.%y %H:%M").replace(tzinfo=timezone.utc)
                obs.append((t, float(f[col].replace(",", "."))))
            except ValueError:
                continue
        obs.sort()
        if obs:
            out["current"] = (obs[-1][1], x["url"], f"{x['stn']} - last obs {obs[-1][0].astimezone(PKT).strftime('%d %b %I:%M %p PKT')}")
            today = [v for t, v in obs if t.astimezone(zi).date() == d0]
            if today:
                out["max_rec"] = (max(today), x["url"], f"{x['stn']} - max of hourly obs since local midnight")
    elif kind == "sg":
        prm = {"date": d0.isoformat()}
        r = get_retry(SG_TEMP_URL, prm)
        vals = [(it["timestamp"], rd["value"]) for it in r.json().get("items", [])
                for rd in it.get("readings", []) if rd.get("station_id") == "S24" and rd.get("value") is not None]
        if vals:
            u = _prep_url(SG_TEMP_URL, prm)
            out["current"] = (float(vals[-1][1]), u, f"{x['stn']} - last obs {vals[-1][0]}")
            out["max_rec"] = (float(max(v for _, v in vals)), u, f"{x['stn']} - max since local midnight")
        try:
            fc = get_retry(SG_FC_URL, None).json()["items"][0]["forecasts"]
            by = {f["date"]: f["temperature"]["high"] for f in fc}
            for k, d in (("high_d1", d1), ("high_d2", d2)):
                if d.isoformat() in by:
                    out[k] = (float(by[d.isoformat()]), SG_FC_URL, "MSS 4-day forecast (Singapore-wide high)")
        except (requests.RequestException, KeyError, IndexError, ValueError):
            pass
    elif kind == "hko":
        prm = {"dataType": "rhrread", "lang": "en"}
        r = get_retry(HKO_URL, prm).json()
        for e in r.get("temperature", {}).get("data", []):
            if e.get("place") == "Chek Lap Kok" and e.get("value") is not None:
                out["current"] = (float(e["value"]), _prep_url(HKO_URL, prm),
                                  f"{x['stn']} - {r.get('temperature', {}).get('recordTime', '')}")
        try:
            prm2 = {"dataType": "fnd", "lang": "en"}
            fc = get_retry(HKO_URL, prm2).json().get("weatherForecast", [])
            by = {f["forecastDate"]: f["forecastMaxtemp"]["value"] for f in fc}
            for k, d in (("high_d1", d1), ("high_d2", d2)):
                if d.strftime("%Y%m%d") in by:
                    out[k] = (float(by[d.strftime("%Y%m%d")]), _prep_url(HKO_URL, prm2), "HKO 9-day forecast (Hong Kong-wide high)")
        except (requests.RequestException, KeyError, ValueError):
            pass
    elif kind == "nws":
        icao = x["icao"]
        page = f"https://www.weather.gov/wrh/timeseries?site={icao}"
        start = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        feats = get_retry(f"https://api.weather.gov/stations/{icao}/observations",
                          {"start": start, "limit": 200}, headers=NWS_UA).json().get("features", [])
        obs = []
        for f in feats:
            pr = f.get("properties", {})
            v = (pr.get("temperature") or {}).get("value")
            if v is not None and pr.get("timestamp"):
                obs.append((datetime.fromisoformat(pr["timestamp"].replace("Z", "+00:00")), float(v)))
        obs.sort()
        if obs:
            out["current"] = (obs[-1][1], page, f"{x['stn']} - last obs {obs[-1][0].astimezone(PKT).strftime('%d %b %I:%M %p PKT')}")
            today = [v for t, v in obs if t.astimezone(zi).date() == d0]
            if today:
                out["max_rec"] = (max(today), page, f"{x['stn']} - max of hourly obs since local midnight")
        try:
            pt = get_retry(f"https://api.weather.gov/points/{x['lat']},{x['lon']}", None, headers=NWS_UA).json()
            per = get_retry(pt["properties"]["forecast"], None, headers=NWS_UA).json()["properties"]["periods"]
            by = {}
            for pd_ in per:
                if pd_.get("isDaytime"):
                    dt = datetime.fromisoformat(pd_["startTime"]).date()
                    t = float(pd_["temperature"])
                    by[dt] = (t - 32) * 5 / 9 if pd_.get("temperatureUnit") == "F" else t
            fpage = f"https://forecast.weather.gov/MapClick.php?lat={x['lat']}&lon={x['lon']}"
            for k, d in (("high_d1", d1), ("high_d2", d2)):
                if d in by:
                    out[k] = (by[d], fpage, "NWS point forecast (daytime high)")
        except (requests.RequestException, KeyError, IndexError, ValueError):
            pass
    return out


@st.cache_data(ttl=120, show_spinner=False)
def fetch_official(cities: tuple) -> dict:
    """{norm_city: {'current'|'max_rec'|'high_d1'|'high_d2': (valueC, url, note)}} - jo mile sirf wahi."""
    res = {}
    for c in cities:
        if c not in OFFICIAL:
            continue
        kind, tzname, x = OFFICIAL[c]
        try:
            res[c] = _official_one(kind, tzname, x)
            _store()[("official", c)] = res[c]
        except (requests.RequestException, KeyError, ValueError, IndexError):
            res[c] = _store().get(("official", c), {})
    return res


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
                    def dmax(off_d):
                        k = (now_local + timedelta(days=off_d)).strftime("%Y-%m-%d")
                        ix = dtimes.index(k) if k in dtimes else None
                        return mx[ix] if ix is not None and ix < len(mx) else None
                    per[name] = {
                        "high_d1": dmax(1), "high_d2": dmax(2),
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


# Click par kholne wali (insani) website - raw API nahi
HUMAN_LINK = {
    "helsinki": "https://en.ilmatieteenlaitos.fi/", "singapore": "https://www.weather.gov.sg/",
    "hong kong": "https://www.hko.gov.hk/en/index.html",
    "munich": "https://www.dwd.de/EN/weather/weather_climate_local/bavaria/munic/_node.html",
}
# Har city ka us desh ki met website par apna page (jahan tak link pakka hai); baqi LIST_LINKS (homepage) par
MET_PAGE = {
    "london": "https://weather.metoffice.gov.uk/forecast/gcpvj0v07",
    "paris": "https://meteofrance.com/previsions-meteo-france/paris/75000",
    "madrid": "https://www.aemet.es/en/eltiempo/prediccion/municipios/madrid-id28079",
    "ankara": "https://www.mgm.gov.tr/eng/forecast-cities.aspx?m=ANKARA",
    "toronto": "https://weather.gc.ca/city/pages/on-143_metric_e.html",
    "sydney": "http://www.bom.gov.au/places/nsw/sydney/",
    "melbourne": "http://www.bom.gov.au/places/vic/melbourne/",
    "wellington": "https://www.metservice.com/towns-cities/locations/wellington",
    "beijing": "https://weather.cma.cn/web/weather/54511.html",
    "shanghai": "https://www.wunderground.com/history/daily/ZSPD",
    "tokyo": "https://www.jma.go.jp/bosai/forecast/#area_type=offices&area_code=130000",
    "amsterdam": "https://www.knmi.nl/nederland-nu/weer/waarnemingen",
    "hong kong": "https://www.hko.gov.hk/en/wxinfo/ts/text_readings.htm",
    "helsinki": "https://en.ilmatieteenlaitos.fi/latest-observations",
}


def met_page(city: str, icao: str, fallback: str) -> str:
    c = norm_city(city)
    if c in HUMAN_LINK:
        return HUMAN_LINK[c]
    if c in MET_PAGE:
        return MET_PAGE[c]
    if icao and (icao[0] == "K" or icao.startswith("PH")) and c in LIST_LINKS and LIST_LINKS[c] == "https://www.weather.gov":
        return f"https://www.weather.gov/wrh/timeseries?site={icao}"
    return LIST_LINKS.get(c, fallback)


# ------------------------------------------------------------- table ------
# Light background per source for the "Current" columns
CUR_BG = ["#d6ecff", "#d9f5dc", "#fff4c2", "#ffdfe6", "#e8dcff"]

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
    total_w = sum(FROZEN_W) + 4 * 105 + n * 4 * 92
    h = [f'<div style="overflow:auto;max-height:78vh;border:1px solid #b8bec6;border-radius:4px;background:#fff">'
         f'<table style="border-collapse:separate;border-spacing:0;min-width:{total_w}px;width:100%">']
    h.append("<thead>")

    def th(txt, extra="", **kw):
        attrs = " ".join(f'{k}="{v}"' for k, v in kw.items())
        return f'<th {attrs} style="{BASE}{HEAD_BG}{extra}">{txt}</th>'

    # Row 1: 4 frozen rowspan headers + group header
    r1 = "".join(
        th(t, _frozen_style(i, True) + f"height:{sum(ROW_H)}px;", rowspan=3)
        for i, t in enumerate(["Sr.", "City with Country", "Day End Time<br>(PKT)", "Poly Market"]))
    r1 += th("Met Website", f"position:sticky;top:0;z-index:5;height:{ROW_H[0]}px;", colspan=4)
    r1 += th("Temperature °C &amp; °F",
             f"position:sticky;top:{ROW_T[0]}px;z-index:4;height:{ROW_H[0]}px;", colspan=4 * n)
    h.append(f"<tr>{r1}</tr>")

    # Row 2: Met Website sub-headers (rowspan 3) + site names
    r2 = "".join(
        th(t, f"position:sticky;top:{ROW_T[1]}px;z-index:4;height:{ROW_H[1] + ROW_H[2] + ROW_H[3]}px;width:105px;min-width:105px;"
              f"{'background:#ffe8cc;' if i == 0 else ''}", rowspan=2)
        for i, t in enumerate(("Current", "Today<br>Max. Recd.<br>Temp.", "Tomorrow<br>Max. Recd.<br>Temp.",
                               "Day After<br>Tomorrow<br>Max. Recd. Temp.")))
    r2 += "".join(
        th(f'<a href="{SITE_HOME[nm]}" target="_blank" rel="noopener" style="color:#0b3d91">'
           f'{"aviationweather.gov" if nm == AVN else html.escape(nm)}</a><br>'
           f'<span style="font-weight:400;font-size:11px">'
           f'{"(airport METAR; forecast: model)" if nm == AVN else "(" + html.escape(lbl) + " model)"}</span>',
           f"position:sticky;top:{ROW_T[1]}px;z-index:4;height:{ROW_H[1]}px;", colspan=4)
        for nm, lbl, _ in SOURCES)
    h.append(f"<tr>{r2}</tr>")

    # Row 3: har site ke neeche Current / Today Max / Tomorrow Max / Day After Tomorrow Max
    r3 = ""
    for i, _ in enumerate(SOURCES):
        for j, t in enumerate(("Current", "Today<br>Max. Recd.<br>Temp.", "Tomorrow<br>Max. Recd.<br>Temp.",
                               "Day After<br>Tomorrow<br>Max. Recd. Temp.")):
            r3 += th(t, f"position:sticky;top:{ROW_T[2]}px;z-index:4;height:{ROW_H[2] + ROW_H[3]}px;width:100px;min-width:100px;"
                        f"{'background:' + CUR_BG[i % len(CUR_BG)] + ';' if j == 0 else ''}")
    h.append(f"<tr>{r3}</tr>")
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
        # Met Website: us desh ki met service ka data; na mile to airport METAR / Open-Meteo model (label ke saath)
        tag = lambda t: f'<br><span style="font-size:10px;font-weight:400;color:#555">({t})</span>'
        off = r["official"]
        om = r["src"].get("Open-Meteo.com", {})
        list_url = met_page(r["city"], r["icao"], r["met_url"])
        metar_page = f"https://aviationweather.gov/data/metar/?ids={r['icao']}&hours=24"
        om_page = (f"https://open-meteo.com/en/docs?latitude={r['lat']}&longitude={r['lon']}"
                   f"&hourly=temperature_2m&daily=temperature_2m_max&models=best_match&forecast_days=3&timezone=auto")
        for k in ("current", "max_rec", "high_d1", "high_d2"):
            if k in off:
                val, url, note = off[k]
                cell = temp_cell(val, url if url.startswith("https://www.weather.gov") or url.startswith("https://forecast.weather.gov") else list_url,
                                 note + " (click: us airport/city ka met service page)")
            elif k in ("current", "max_rec") and r["is_metar"]:
                v = r["cur_v"] if k == "current" else r["max_v"]
                cell = temp_cell(v, metar_page, f"Data: {r['icao']} airport METAR (NOAA) - {r['met_name']} ka apna data nahi. Click: us airport ki METAR page") + tag("METAR")
            else:
                cell = temp_cell(om.get(k), om_page, "Data: Open-Meteo model (city ki coordinates par) - met service ka apna data nahi. Click: Open-Meteo page") + tag("model")
            cell = cell.replace("<a href", '<a style="color:#0b3d91" href')
            bg = "background:#ffe8cc;font-weight:600;" if k == "current" else row_bg
            cells.append(f'<td style="{BASE}{bg}white-space:nowrap;">{cell}</td>')
        for si, (name, _l, model) in enumerate(SOURCES):
            if name == AVN:   # aviationweather.gov: METAR (Current / Today Max), forecast Open-Meteo "(model)"
                for k in ("current", "max_rec", "high_d1", "high_d2"):
                    bg = (f"background:{CUR_BG[si % len(CUR_BG)]};font-weight:600;" if k == "current" else row_bg)
                    if k in ("current", "max_rec") and r["is_metar"]:
                        v = r["cur_v"] if k == "current" else r["max_v"]
                        cell = temp_cell(v, metar_page, f"{r['icao']} airport METAR - aviationweather.gov (last obs {r['obs_note']})")
                    elif k in ("current", "max_rec"):
                        cell = temp_cell(om.get(k), om_page, "METAR available nahi: Open-Meteo model value") + tag("model")
                    else:
                        cell = temp_cell(om.get(k), om_page, "aviationweather.gov forecast temperature publish nahi karta: Open-Meteo model") + tag("model")
                    cell = cell.replace("<a href", '<a style="color:#0b3d91" href')
                    cells.append(f'<td style="{BASE}{bg}white-space:nowrap;">{cell}</td>')
                continue
            src = r["src"].get(name, {})
            url = source_url(r["lat"], r["lon"], model)
            for k in ("current", "max_rec", "high_d1", "high_d2"):
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
    default=city_options,   # sab Polymarket cities pehle se select
    help="Shuru mein sab cities select hoti hain (aur % filter lagta hai). Kisi city ko hatane ke liye x dabayein.")

# Filter: top-2 outcomes both inside [lo, hi]
poly["top2"] = poly["buckets"].apply(lambda b: b[:2])
poly["pass"] = poly["top2"].apply(lambda t: len(t) == 2 and all(lo <= p <= hi for _, p in t))

def keep(r) -> bool:
    in_list = (r["city"] in picked) if picked else True
    return in_list and (show_all or bool(r["pass"]))


all_rows = []
for _, r in poly.iterrows():
    loc = locate(r["city"])
    all_rows.append((r, loc))

def icao_for(r) -> str:
    return (str(r.get("icao") or "")).upper() or FALLBACK_ICAO.get(norm_city(r["city"]), "")


chosen = [(r, loc) for r, loc in all_rows if keep(r) and loc]
icaos = tuple(sorted({icao_for(r) for r, _ in chosen if icao_for(r)}))
metar, m_err = fetch_metar(icaos) if icaos else ({}, None)
if m_err:
    st.warning(m_err)
coords = tuple(sorted({(round(loc["lat"], 4), round(loc["lon"], 4)) for _, loc in chosen}))
weather, w_err, w_stamp = fetch_weather(coords) if coords else ({}, None, None)
if w_err:
    st.warning(w_err)

def _official_for(official: dict, city: str, ic: str) -> dict:
    """Official data tabhi jab uska station wohi airport ho jo Polymarket use karta hai."""
    c = norm_city(city)
    cfg = OFFICIAL.get(c)
    if cfg and cfg[0] == "nws" and ic and ic != cfg[2]["icao"]:
        return {}
    return official.get(c, {})


official = fetch_official(tuple(sorted({norm_city(r["city"]) for r, _ in chosen}))) if chosen else {}
rows = []
for r, loc in chosen:
    wk = (round(loc["lat"], 4), round(loc["lon"], 4))
    w = weather.get(wk, {"tz": "UTC", "src": {}})
    ic = icao_for(r)
    stn = metar.get(ic)
    cur_v = max_v = None
    obs_note = ""
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
    poly_html = "<br>".join(f"<b>{p:.0f}%</b> ({html.escape(t)})" for t, p in r["top2"]) or "N/A"
    rows.append({"city": r["city"], "country": loc["country"],
                 "day_end": day_end_pkt(r["end"], w["tz"]), "link": r["link"],
                 "poly_html": poly_html, "src": w["src"], "lat": loc["lat"], "lon": loc["lon"],
                 "icao": ic, "is_metar": is_metar, "obs_note": obs_note, "cur_v": cur_v, "max_v": max_v,
                 "official": _official_for(official, r["city"], ic),
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
            "polymarket": re.sub("<[^>]+>", "", r["poly_html"].replace("<br>", " | ")),
            "day_end_pkt": re.sub("<[^>]+>", " ", r["day_end"]),
            **{f"{n}_{k}_c": r["src"].get(n, {}).get(k) for n, _, _ in SOURCES
               for k in ("current", "max_rec", "high_d1", "high_d2")}} for r in rows])
        st.download_button("Download CSV", flat.to_csv(index=False).encode(), "temperature_dashboard.csv", "text/csv")
    if skipped:
        st.caption("Location nahi mili (skip): " + ", ".join(skipped))
    st.caption("AVIATIONWEATHER.GOV: Current aur Today Max. Recd. = us airport ki NOAA METAR reading (click: aviationweather.gov ka METAR page). "
               "aviationweather.gov forecast temperature publish nahi karta, is liye Tomorrow / Day After Tomorrow Open-Meteo model hain, neeche '(model)' likha hai. "
               "MET WEBSITE: jahan us desh ki met service ka free data milta hai (Helsinki: FMI, Singapore: NEA/MSS, Hong Kong: HKO, Munich: DWD, US cities: NWS) wahan unka apna data; "
               "baqi jagah number ke neeche label hai: (METAR) = us airport ki NOAA METAR reading, (model) = Open-Meteo forecast - ye met service ka apna data nahi hai. "
               "Sab weather columns Open-Meteo API ke alag models se hain (ECMWF, NOAA GFS, DWD ICON, CMA, Best match); "
               "ye Windy/Weather.gov/Wunderground/Weather.com ke apne published numbers nahi hain. "
               "Har temperature par click karein to wohi Open-Meteo API page khulega jahan se number liya gaya. "
               "Header mein site ke naam par click se us site ka homepage khulta hai. "
               "Today Max. Recd. = aaj ka ab tak ka model-based maximum (city ke local din ka); Tomorrow / Day After Tomorrow = model ka forecast maximum.")

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
