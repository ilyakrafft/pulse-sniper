"""Pulse Sniper: сбор данных и расчёт сигналов (единый источник для приложения, бота и отчёта).
Каждый запуск:
1. скачивает рынки максимальной температуры с Polymarket (те же, что на Pulse Market) -> data/markets.json
2. раз в час собирает прогнозы 11 моделей по координатам аэропортов (станций, по которым рынки закрываются) -> data/forecast.json
3. считает сигналы по единым правилам и фиксирует каждый при выдаче (цена, вероятность) -> data/signals.json
4. когда рынок закрывается, записывает итог сигнала -> data/bets.json; учится на фактах -> data/calib.json
Только стандартная библиотека Python, ключи не нужны.
"""
import json
import math
import pathlib
import re
import urllib.error
import urllib.request
import datetime as dt

GAMMA = "https://gamma-api.polymarket.com/events?slug="
METEO = "https://api.open-meteo.com/v1/forecast"
METAR = "https://aviationweather.gov/api/data/metar"
EVENT_URL = "https://pulsemarket.trade/ru/event/"
UA = {"User-Agent": "pulse-sniper-collector"}
DATA = pathlib.Path("data")
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
# Модели Open-Meteo (бесплатно, без ключа). «seamless» уже включает региональные модели:
# ICON-EU/D2, GFS+HRRR, UKMO UKV, Météo-France AROME/ARPEGE, JMA MSM, KMA LDPS.
# key, вес, название, подпись
US = ["dallas", "nyc", "sf", "miami", "la", "houston"]
EU = ["london", "paris", "amsterdam", "munich", "milan", "madrid", "helsinki", "warsaw"]
# key, вес, название, подпись, города (None = все). Региональные модели точнее на 1–2 дня и запрашиваются только для своих городов.
MODELS = [
    ("ecmwf_ifs025", 1.3, "ECMWF IFS", "Европейский центр", None),
    ("ecmwf_aifs025_single", 1.2, "ECMWF AIFS", "ИИ-модель ECMWF", None),
    ("icon_seamless", 1.1, "ICON", "Германия, DWD", None),
    ("gfs_seamless", 1.0, "GFS", "США, NOAA", None),
    ("ukmo_seamless", 1.0, "UKMO", "Великобритания, Met Office", None),
    ("meteofrance_seamless", 0.9, "Météo-France", "ARPEGE, AROME", None),
    ("gem_seamless", 0.8, "GEM", "Канада, ECCC", None),
    ("jma_seamless", 0.6, "JMA", "Япония, GSM, MSM", None),
    ("kma_seamless", 0.5, "KMA", "Корея, GDPS", None),
    ("cma_grapes_global", 0.4, "CMA GRAPES", "Китай", None),
    ("bom_access_global", 0.4, "BoM ACCESS", "Австралия", None),
    ("gfs_hrrr", 1.3, "NOAA HRRR", "США, 3 км", US),
    ("ncep_nbm_conus", 1.2, "NOAA NBM", "США, сводный", US),
    ("icon_d2", 1.3, "ICON-D2", "Европа, 2 км", EU),
    ("knmi_harmonie_arome_europe", 1.0, "HARMONIE-AROME", "Европа, 2,5 км", EU),
    ("meteofrance_arome_france_hd", 1.2, "AROME HD", "Франция, 1,5 км", ["paris"]),
    ("ukmo_uk_deterministic_2km", 1.2, "UKMO UKV", "Великобритания, 2 км", ["london"]),
]
MODEL_W = {m[0]: m[1] for m in MODELS}
MODEL_W["ens"] = 1.6
ENS_MODELS = ["ecmwf_ifs025", "gfs025", "icon_seamless", "gem_global"]  # ансамбли: ECMWF ENS, GEFS, ICON-EPS, GEPS
ENS_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
OLD_KEYS = ["ecmwf_ifs025", "icon_seamless", "gfs_seamless", "gem_seamless"]  # порядок старого формата forecasts.json
# id, slug в адресе события, широта и долгота СТАНЦИИ (аэропорта, по которому закрывается рынок), код ICAO
CITIES = [
    ("dallas", "dallas", 32.847, -96.852, "KDAL"), ("shenzhen", "shenzhen", 22.639, 113.811, "ZGSZ"),
    ("jeddah", "jeddah", 21.680, 39.157, "OEJN"), ("london", "london", 51.505, 0.055, "EGLC"),
    ("paris", "paris", 49.010, 2.548, "LFPG"), ("telaviv", "tel-aviv", 32.011, 34.887, "LLBG"),
    ("helsinki", "helsinki", 60.317, 24.963, "EFHK"), ("amsterdam", "amsterdam", 52.311, 4.768, "EHAM"),
    ("munich", "munich", 48.354, 11.786, "EDDM"), ("chongqing", "chongqing", 29.719, 106.642, "ZUCK"),
    ("nyc", "nyc", 40.777, -73.874, "KLGA"), ("hongkong", "hong-kong", 22.302, 114.174, None),
    ("milan", "milan", 45.631, 8.728, "LIMC"), ("moscow", "moscow", 55.591, 37.261, "UUWW"),
    ("sf", "san-francisco", 37.621, -122.379, "KSFO"), ("miami", "miami", 25.796, -80.287, "KMIA"),
    ("madrid", "madrid", 40.498, -3.568, "LEMD"), ("la", "los-angeles", 33.942, -118.408, "KLAX"),
    ("houston", "houston", 29.645, -95.279, "KHOU"), ("singapore", "singapore", 1.364, 103.991, "WSSS"),
    ("tokyo", "tokyo", 35.549, 139.780, "RJTT"), ("warsaw", "warsaw", 52.166, 20.967, "EPWA"),
]
ICAO = {c[0]: c[4] for c in CITIES}

# ---------- единые правила сигнала (их же показывает приложение и отправляет бот) ----------
MIN_PRICE = 0.15      # дешевле: слишком дальний вариант
MAX_PRICE = 0.85
MIN_PM = 0.25         # итоговая вероятность варианта (один градус редко бывает выше 30–40%)
MIN_EDGE = 0.05       # итоговая вероятность минус цена
MODEL_SHARE = 0.6     # доля моделей в итоговой вероятности, остальное это сама цена рынка
MAX_SD = 1.3          # допустимый разброс моделей, °C
MAX_PER_DAY = 6       # сигналов на одну дату рынка
MIN_MODELS = 4
LAST_NEW_HOUR = 14   # на сегодняшний рынок новые сигналы только до 14:00 по времени города
MAX_CORR_COST = 0.95   # сумма цен трёх вариантов: чтобы осталась запас на комиссию
MIN_CORR_P = 0.45     # шанс попасть в коридор
MIN_CORR_EDGE = 0.025  # шанс минус стоимость коридора
ALLOW_SINGLE = False   # одиночные ставки отключены: они проигрывают чаще коридора
UNTIL_HOUR = 16      # после 16:00 по времени города сигнал в приложении и в боте больше не показывается
BASE_SIGMA, LEAD_SIGMA = 1.0, 0.25   # типичная ошибка суточного максимума на станции, °C

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def load(name, default):
    p = DATA / name
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(name, obj):
    DATA.mkdir(exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def slug_for(city_slug, d):
    return "highest-temperature-in-%s-on-%s-%d-%d" % (city_slug, MONTHS[d.month - 1], d.day, d.year)


def fetch_event(slug):
    try:
        data = get(GAMMA + slug)
    except (urllib.error.URLError, ValueError):
        return None
    return data[0] if isinstance(data, list) and data else None


def f2c(f):
    return (f - 32) / 1.8


def parse_bucket(title):
    """Название варианта -> (подпись, нижняя граница °C, верхняя °C, центр °C)."""
    t = (title or "").strip()
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\s+or\s+(below|lower|higher|above)", t, re.I)
    if m:
        v, unit, low = float(m.group(1)), m.group(2).upper(), m.group(3).lower() in ("below", "lower")
        c = v if unit == "C" else f2c(v)
        half = 0.5 if unit == "C" else 0.5 / 1.8
        lo, hi = (-90.0, c + half) if low else (c - half, 90.0)
        label = "%s°%s %s" % (m.group(1), unit, "и ниже" if low else "и выше")
        return label, lo, hi, c
    m = re.match(r"^(-?\d+)\s*[-–]\s*(-?\d+)\s*°?\s*F", t, re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return "%d–%d°F" % (a, b), f2c(a - 0.5), f2c(b + 0.5), f2c((a + b) / 2)
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*°?\s*C", t, re.I)
    if m:
        v = float(m.group(1))
        return "%s°C" % m.group(1), v - 0.5, v + 0.5, v
    return None


def yes_price(market):
    p = market.get("outcomePrices")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            return None
    try:
        return float(p[0])
    except (TypeError, ValueError, IndexError):
        return None


def buckets_of(event):
    out = []
    for mk in event.get("markets") or []:
        parsed = parse_bucket(mk.get("groupItemTitle") or mk.get("question") or "")
        price = yes_price(mk)
        if parsed and price is not None:
            label, lo, hi, centre = parsed
            b = {"l": label, "lo": round(lo, 3), "hi": round(hi, 3), "p": round(price, 3), "c": round(centre, 2)}
            try:  # реальная цена покупки «Да» (лучшая заявка продавца): купить по средней цене нельзя
                ask = float(mk.get("bestAsk"))
                if 0 < ask < 1:
                    b["ask"] = round(ask, 3)
            except (TypeError, ValueError):
                pass
            out.append(b)
    return out


def station_of(ev):
    """Станция, по которой закрывается рынок, из правил самого события (ссылка Wunderground заканчивается кодом ICAO)."""
    parts = [ev.get("resolutionSource"), ev.get("description")] + [mk.get("resolutionSource") for mk in (ev.get("markets") or [])[:2]]
    txt = " ".join(str(x) for x in parts if x)
    m = re.search(r"wunderground\.com/[^\s\"')]*?/([A-Z]{4})(?:[/?#\s\"')]|$)", txt)
    return m.group(1) if m else None


def num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def vol_label(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    s = ("$%.1fK" % (v / 1000)) if v >= 1000 else ("$%d" % v)
    return s.replace(".", ",")


# ---------- математика прогноза ----------
def cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def as_dict(vals):
    """Старый формат forecasts.json был списком из четырёх моделей, новый словарь."""
    if isinstance(vals, dict):
        return vals
    return {k: v for k, v in zip(OLD_KEYS, vals or [])}


def consensus(vals, wmap=None):
    wmap = wmap or {}
    pairs = [(v, wmap.get(k, MODEL_W.get(k, 0.5))) for k, v in as_dict(vals).items() if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(pairs) < MIN_MODELS:
        return None
    W = sum(w for _, w in pairs)
    mu = sum(v * w for v, w in pairs) / W
    sdm = math.sqrt(sum(w * (v - mu) ** 2 for v, w in pairs) / W)
    return mu, sdm, len(pairs)


def calib_of(calib, city):
    """Поправка и реальная ошибка по истории закрытых дней этого города."""
    rows = (calib.get(city) or [])[-40:]
    n = len(rows)
    if n < 5:
        return 0.0, None, n
    errs = [r["a"] - r["mu"] for r in rows]
    bias = sum(errs) / n
    rmse = math.sqrt(sum((e - bias) ** 2 for e in errs) / n)
    return clamp(bias * n / (n + 6), -1.5, 1.5), rmse, n


def model_weights(calib, city):
    """Вес каждой модели по её собственной ошибке в этом городе: точные растут, слабые теряют вес."""
    rows = (calib.get(city) or [])[-40:]
    errs = {}
    for r in rows:
        for k, v in (r.get("m") or {}).items():
            errs.setdefault(k, []).append(abs(v - r["a"]))
    ok = {k: sum(e) / len(e) for k, e in errs.items() if len(e) >= 8}
    if len(ok) < 3:
        return {}
    ref = sorted(ok.values())[len(ok) // 2]
    out = {}
    for k, mae in ok.items():
        n = len(errs[k])
        f = clamp((ref / max(mae, 0.3)) ** 2, 0.3, 3.0)
        out[k] = MODEL_W.get(k, 0.5) * (1 + (f - 1) * n / (n + 10.0))
    return out


def predict(vals, lead, calib_city=(0.0, None, 0), es=None, wmap=None):
    c = consensus(vals, wmap)
    if not c:
        return None
    mu, sdm, n = c
    if es is not None and es > 0:  # реальный разброс ансамблей (с запасом: ансамбли обычно слишком уверены)
        sdm = math.sqrt(0.5 * sdm ** 2 + 0.5 * (1.2 * es) ** 2)
    bias, rmse, hist = calib_city
    base = BASE_SIGMA + LEAD_SIGMA * max(lead, 0)
    if rmse is not None:
        w = hist / (hist + 10.0)
        base = w * clamp(rmse, 0.7, 3.0) + (1 - w) * base
    sigma = math.sqrt(sdm ** 2 + base ** 2)
    return {"mu": mu + bias, "sigma": sigma, "sd": sdm, "n": n}


def bucket_prob(lo, hi, mu, sigma, floor=None):
    """Вероятность, что максимум дня попадёт в [lo, hi]. floor: максимум уже не ниже этого значения (наблюдения)."""
    if floor is not None:
        lo = max(lo, floor)
        if hi <= lo:
            return 0.0
        tail = max(1 - cdf((floor - mu) / sigma), 0.02)
        return min(1.0, (cdf((hi - mu) / sigma) - cdf((lo - mu) / sigma)) / tail)
    return cdf((hi - mu) / sigma) - cdf((lo - mu) / sigma)


def corridor(vals, lead=1, calib_city=(0.0, None, 0), wmap=None):
    pr = predict(vals, lead, calib_city, None, wmap)
    if not pr:
        return None
    mu, sigma = pr["mu"], pr["sigma"]
    k0 = int(math.floor(mu + 0.5))
    p = cdf((k0 + 1.5 - mu) / sigma) - cdf((k0 - 1.5 - mu) / sigma)
    return {"k": k0, "p": round(p, 3), "range": round(pr["sd"] * 2, 2)}


# ---------- рынки ----------
def collect_markets(today):
    markets = []
    for cid, slug, _, _, _ in CITIES:
        for add in range(0, 3):
            d = today + dt.timedelta(days=add)
            ev = fetch_event(slug_for(slug, d))
            if not ev or ev.get("closed"):
                continue
            b = sorted(buckets_of(ev), key=lambda x: -x["p"])
            if b:
                markets.append({"c": cid, "date": d.isoformat(), "vol": vol_label(ev.get("volume")), "b": b,
                                "st": station_of(ev), "liq": num(ev.get("liquidity")), "v24": num(ev.get("volume24hr"))})
    return markets


# ---------- прогнозы ----------
def collect_forecast_all(now):
    """Все модели по координатам станций. Каждая модель отдельным запросом: одна неудача не ломает остальные."""
    import time
    maps, offsets, used = {}, {}, []
    for key, w, name, sub, region in MODELS:
        cs = [c for c in CITIES if region is None or c[0] in region]
        lat = ",".join(str(c[2]) for c in cs)
        lon = ",".join(str(c[3]) for c in cs)
        url = "%s?latitude=%s&longitude=%s&daily=temperature_2m_max&models=%s&forecast_days=7&timezone=auto" % (METEO, lat, lon, key)
        try:
            data = get(url)
        except Exception as e:  # noqa: BLE001
            print("модель", key, "не загрузилась:", type(e).__name__)
            continue
        data = data if isinstance(data, list) else [data]
        got = 0
        for c, o in zip(cs, data):
            daily = (o or {}).get("daily") or {}
            arr = daily.get("temperature_2m_max_" + key) or daily.get("temperature_2m_max")
            times = daily.get("time")
            if not arr or not times:
                continue
            mp = {t: v for t, v in zip(times, arr) if isinstance(v, (int, float))}
            if not mp:
                continue
            maps.setdefault(c[0], {})[key] = mp
            offsets[c[0]] = int((o or {}).get("utc_offset_seconds") or 0)
            got += 1
        if got:
            used.append({"key": key, "name": name, "sub": sub, "w": w})
        time.sleep(1)
    if len(used) < MIN_MODELS:
        return None
    alld = sorted({t for per in maps.values() for mp in per.values() for t in mp})
    first = dt.date.fromisoformat(alld[0])
    dates = [(first + dt.timedelta(days=i)).isoformat() for i in range(7)]
    cities = {cid: {k: [mp.get(d) for d in dates] for k, mp in per.items()} for cid, per in maps.items()}
    return {"updated": now.isoformat(timespec="minutes"), "models": used, "dates": dates, "offsets": offsets, "cities": cities}


def collect_ens(now):
    """Ансамбли (ECMWF ENS, GEFS, ICON-EPS, GEPS): по каждому участнику суточный максимум, затем среднее и разброс."""
    import time
    lat = ",".join(str(c[2]) for c in CITIES)
    lon = ",".join(str(c[3]) for c in CITIES)
    pool = {}  # город -> дата -> список максимумов участников
    used = 0
    for key in ENS_MODELS:
        url = "%s?latitude=%s&longitude=%s&hourly=temperature_2m&models=%s&forecast_days=7&timezone=auto" % (ENS_URL, lat, lon, key)
        try:
            data = get(url)
        except Exception as e:  # noqa: BLE001
            print("ансамбль", key, "не загрузился:", type(e).__name__)
            continue
        data = data if isinstance(data, list) else [data]
        got = 0
        for c, o in zip(CITIES, data):
            h = (o or {}).get("hourly") or {}
            times = h.get("time") or []
            for name, arr in h.items():
                if not name.startswith("temperature_2m") or not isinstance(arr, list):
                    continue
                per = {}
                for t, v in zip(times, arr):
                    if isinstance(v, (int, float)):
                        per.setdefault(t[:10], []).append(v)
                for d, vs in per.items():
                    if len(vs) >= 22:  # только полные сутки
                        pool.setdefault(c[0], {}).setdefault(d, []).append(max(vs))
                got += 1
        if got:
            used += 1
        time.sleep(1)
    out = {}
    for cid, per in pool.items():
        for d, vs in per.items():
            if len(vs) >= 20:
                m = sum(vs) / len(vs)
                sd = math.sqrt(sum((v - m) ** 2 for v in vs) / len(vs))
                out.setdefault(cid, {})[d] = [round(m, 2), round(sd, 2), len(vs)]
    if used < 1 or not out:
        return None
    return {"updated": now.isoformat(timespec="minutes"), "models": used, "cities": out}


def merge_ens(fc, ens):
    """Добавляем ансамбль как ещё одну «модель» и сохраняем его разброс для расчёта вероятностей."""
    if not ens or not fc.get("cities"):
        return fc
    dates = fc.get("dates") or []
    sd = {}
    got = 0
    for cid, per in (ens.get("cities") or {}).items():
        if cid not in fc["cities"]:
            continue
        fc["cities"][cid]["ens"] = [per[d][0] if d in per else None for d in dates]
        sd[cid] = [per[d][1] if d in per else None for d in dates]
        got += 1
    if got:
        fc["ens_sd"] = sd
        fc["models"] = [m for m in fc.get("models", []) if m["key"] != "ens"] + [
            {"key": "ens", "name": "Ансамбли", "sub": "ECMWF, GEFS, ICON, GEM: %d" % ens.get("models", 0), "w": MODEL_W["ens"]}]
    return fc


def freeze_forecasts(forecasts, fc, today):
    """Запоминаем прогноз заранее (дата строго после сегодняшней), чтобы потом честно сверить с фактом."""
    for cid, per in (fc.get("cities") or {}).items():
        for i, d in enumerate(fc.get("dates") or []):
            if dt.date.fromisoformat(d) <= today:
                continue
            vals = {k: arr[i] for k, arr in per.items() if i < len(arr) and arr[i] is not None}
            if vals:
                forecasts["%s|%s" % (cid, d)] = vals
    return forecasts


def local_now(now, fc, city):
    return now + dt.timedelta(seconds=(fc.get("offsets") or {}).get(city, 0))


# ---------- наблюдения на станциях (METAR): максимум дня, уже достигнутый ----------
def fetch_obs(markets, fc, now):
    need = {}
    for m in markets:
        icao = ICAO.get(m["c"])
        if icao and local_now(now, fc, m["c"]).date().isoformat() == m["date"]:
            need[m["c"]] = m["date"]
    if not need:
        return {}
    ids = ",".join(ICAO[c] for c in need)
    try:
        rows = get("%s?ids=%s&format=json&hours=30" % (METAR, ids))
    except Exception as e:  # noqa: BLE001
        print("METAR не загрузился:", type(e).__name__)
        return {}
    by_icao = {v: k for k, v in ICAO.items() if v}
    out = {}
    for r in rows if isinstance(rows, list) else []:
        city = by_icao.get(r.get("icaoId"))
        t, ts = r.get("temp"), r.get("obsTime")
        if city not in need or not isinstance(t, (int, float)) or not isinstance(ts, (int, float)):
            continue
        ldt = dt.datetime.fromtimestamp(ts, dt.timezone.utc) + dt.timedelta(seconds=(fc.get("offsets") or {}).get(city, 0))
        if ldt.date().isoformat() == need[city]:
            out[city] = max(out.get(city, -99), t)
    return out


# ---------- сигналы ----------
def buy_price(b):
    a = b.get("ask")
    return a if isinstance(a, (int, float)) and 0 < a < 1 else b["p"]


def evaluate(m, fc, calib, obs, now):
    """Один рынок -> (сигнал или None, причина отказа по-русски)."""
    vals = (fc.get("cities") or {}).get(m["c"])
    if not vals:
        return None, "Нет прогноза моделей по этому городу"
    icao = ICAO.get(m["c"])
    if m.get("st") and icao and m["st"] != icao:
        return None, "Рынок закрывается по станции %s, а прогноз у нас по %s: не рискуем" % (m["st"], icao)
    d = dt.date.fromisoformat(m["date"])
    ln = local_now(now, fc, m["c"])
    lead = (d - ln.date()).days
    if lead < 0 or lead > 2:
        return None, "Рынок вне окна сигналов"
    dates = fc.get("dates") or []
    if m["date"] not in dates:
        return None, "Нет прогноза на эту дату"
    i = dates.index(m["date"])
    es = ((fc.get("ens_sd") or {}).get(m["c"]) or [None] * 7)[i] if i < 7 else None
    pr = predict({k: arr[i] for k, arr in vals.items() if i < len(arr)}, lead, calib_of(calib, m["c"]), es, model_weights(calib, m["c"]))
    if not pr:
        return None, "Мало моделей дали прогноз"
    if pr["sd"] > MAX_SD:
        return None, "Модели расходятся (разброс %.1f°), уверенного прогноза нет" % pr["sd"]
    if lead == 0 and ln.hour >= LAST_NEW_HOUR:
        return None, "День уже прошёл пик, рынок почти решён"
    floor = None
    mu, sigma = pr["mu"], pr["sigma"]
    if lead == 0 and icao:
        if m["c"] in obs:
            floor = obs[m["c"]] - 1.0
            f = clamp((17 - (ln.hour + ln.minute / 60.0)) / 10.0, 0.3, 1.0)
            sigma = max(0.5, sigma * f)
            mu = max(mu, obs[m["c"]])
            pr = dict(pr, mu=mu, sigma=sigma)
        elif ln.hour >= 12:
            return None, "Нет свежих данных станции на сегодня"
    bks = sorted(m["b"], key=lambda b: b["lo"])
    pms = [bucket_prob(b["lo"], b["hi"], mu, sigma, floor) for b in bks]
    dist = [{"l": b["l"], "m": round(p, 3), "p": b["p"]} for b, p in zip(bks, pms)]
    base = {"city": m["c"], "date": m["date"], "mu": round(mu, 2), "sigma": round(sigma, 2), "sd": round(pr["sd"], 2), "n": pr["n"],
            "st": m.get("st") or icao, "liq": m.get("liq"), "v24": m.get("v24"), "dist": dist,
            "first": now.isoformat(timespec="minutes"), "status": "open",
            "until": (dt.datetime(d.year, d.month, d.day, UNTIL_HOUR, tzinfo=dt.timezone.utc)
                      - dt.timedelta(seconds=(fc.get("offsets") or {}).get(m["c"], 0))).isoformat(timespec="minutes")}
    # --- коридор: три соседних варианта, ставки так, чтобы выплата была одинаковой ---
    best_c = None
    for j in range(0, len(bks) - 2):
        w = bks[j:j + 3]
        cost = sum(buy_price(b) for b in w)
        mid = sum(b["p"] for b in w)
        pm = sum(pms[j:j + 3])
        p = MODEL_SHARE * pm + (1 - MODEL_SHARE) * mid
        e = p - cost
        if best_c is None or e > best_c[0]:
            best_c = (e, j, w, cost, pm, p)
    if best_c and best_c[3] <= MAX_CORR_COST and best_c[5] >= MIN_CORR_P and best_c[0] >= MIN_CORR_EDGE:
        e, j, w, cost, pm, p = best_c
        return dict(base, kind="corr", label=w[1]["l"], labels=[b["l"] for b in w], lo=w[0]["lo"], hi=w[2]["hi"],
                    legs=[{"l": b["l"], "price": buy_price(b), "pm": round(x, 3)} for b, x in zip(w, pms[j:j + 3])],
                    price=round(cost, 3), pm=round(p, 3), pm_model=round(pm, 3), edge=round(e, 3),
                    level="strong" if e >= 0.10 and p >= 0.65 and pr["sd"] <= 0.8 else ("mid" if e >= 0.05 else "light")), None
    # --- один вариант (если коридор не прошёл) ---
    if not ALLOW_SINGLE:
        return None, "Коридор невыгоден: цена трёх вариантов выше нашей оценки (лучшее преимущество %+d п.п.)" % round(((best_c[0] if best_c else 0)) * 100)
    top2 = sorted(pms, reverse=True)[:2]
    best = None
    for b, pm in zip(bks, pms):
        price = buy_price(b)
        if not (MIN_PRICE <= price <= MAX_PRICE) or pm < min(top2):
            continue
        p = MODEL_SHARE * pm + (1 - MODEL_SHARE) * b["p"]
        e = p - price
        if best is None or e > best[2]:
            best = (b, p, e, pm, price)
    if best and best[1] >= MIN_PM and best[2] >= MIN_EDGE:
        b, p, e, pm, price = best
        return dict(base, kind="single", label=b["l"], labels=[b["l"]], lo=b["lo"], hi=b["hi"], legs=[{"l": b["l"], "price": price, "pm": round(pm, 3)}],
                    price=price, pm=round(p, 3), pm_model=round(pm, 3), edge=round(e, 3),
                    level="strong" if e >= 0.10 and pr["sd"] <= 0.8 and p >= 0.4 else "mid"), None
    return None, "Цена рынка уже учла прогноз: выгоды нет (лучшее преимущество %+d п.п.)" % round(((best_c[0] if best_c else 0)) * 100)


def build_signals(markets, fc, calib, obs, now, sigs):
    """Единые правила. Новый сигнал фиксируется один раз: цена и вероятность остаются теми, что были в момент выдачи."""
    have = {s["key"] for s in sigs}
    per_date = {}
    for s in sigs:
        per_date[s["date"]] = per_date.get(s["date"], 0) + 1
    fresh, skips = [], []
    for m in markets:
        key = "%s|%s" % (m["c"], m["date"])
        if key in have:
            continue
        sig, why = evaluate(m, fc, calib, obs, now)
        if sig:
            sig["key"] = key
            sig["url"] = EVENT_URL + slug_for(next(c[1] for c in CITIES if c[0] == m["c"]), dt.date.fromisoformat(m["date"]))
            fresh.append(sig)
        else:
            skips.append({"c": m["c"], "date": m["date"], "r": why})
    fresh.sort(key=lambda s: -s["edge"])
    for s in fresh:
        if per_date.get(s["date"], 0) >= MAX_PER_DAY:
            skips.append({"c": s["city"], "date": s["date"], "r": "Уже есть %d сигнала на эту дату: берём самые сильные" % MAX_PER_DAY})
            continue
        per_date[s["date"]] = per_date.get(s["date"], 0) + 1
        sigs.append(s)
    return sigs, skips


def settle_signals(sigs, results):
    for s in sigs:
        r = results.get(s["key"])
        if s["status"] == "open" and r:
            s["win"], s["hit"], s["status"] = r["win"], r["win"] in (s.get("labels") or [s["label"]]), "done"
    return sigs


def bets_from(sigs, old):
    keep = {(b["date"], b["city"]): b for b in old}
    for s in sigs:
        if s["status"] == "done":
            keep[(s["date"], s["city"])] = {"date": s["date"], "city": s["city"], "label": s["label"], "win": s["win"],
                                              "hit": s["hit"], "price": s["price"], "pm": s["pm"], "edge": s["edge"], "level": s["level"],
                                              "kind": s.get("kind", "single"), "labels": s.get("labels") or [s["label"]]}
    cut = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    return sorted((b for b in keep.values() if b["date"] >= cut), key=lambda b: b["date"])


# ---------- итоги ----------
def resolve(forecasts, stats, results, watch, calib, today):
    """Ищет закрытые рынки. results: победивший вариант (для сигналов и виртуального счёта).
    stats: коридор из трёх градусов (справочно). calib: прогноз против факта (обучение поправок)."""
    done = {(r["city"], r["date"]) for r in stats}
    slugs = {c[0]: c[1] for c in CITIES}
    for key in sorted(set(forecasts) | set(watch)):
        cid, day = key.split("|")
        d = dt.date.fromisoformat(day)
        if d > today:
            continue
        if (today - d).days > 7 or key in results:
            forecasts.pop(key, None)
            watch.pop(key, None)
            continue
        ev = fetch_event(slug_for(slugs[cid], d))
        if not ev:
            continue
        win = [b for b in buckets_of(ev) if b["p"] >= 0.95]
        if not win:
            continue  # ещё не закрыт
        actual = int(math.floor(win[0]["c"] + 0.5))
        results[key] = {"win": win[0]["l"], "actual": actual}
        vals = as_dict(forecasts.get(key))
        cs = consensus(vals) if vals else None
        if cs:
            rows = calib.setdefault(cid, [])
            rows.append({"d": day, "mu": round(cs[0], 2), "sd": round(cs[1], 2), "a": win[0]["c"],
                         "m": {k: round(v, 1) for k, v in vals.items() if isinstance(v, (int, float))}})
            calib[cid] = rows[-60:]
        c = corridor(vals) if vals else None
        if c and c["p"] >= 0.45 and (cid, day) not in done:
            stats.append({"date": day, "city": cid, "k": c["k"], "p": c["p"], "actual": actual,
                          "hit": abs(actual - c["k"]) <= 1, "v": vals})
        forecasts.pop(key, None)
        watch.pop(key, None)
    return stats


def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    forecasts = load("forecasts.json", {})
    stats = load("stats.json", [])
    results = load("results.json", {})
    watch = load("watch.json", {})
    calib = load("calib.json", {})
    sigs = load("signals.json", [])
    fc = load("forecast.json", {})
    try:
        upd = dt.datetime.fromisoformat(fc.get("updated", "2000-01-01T00:00+00:00"))
        if (now - upd).total_seconds() > 50 * 60:
            new = collect_forecast_all(now)
            if new:
                fc = new
                save("forecast.json", fc)
                print("прогнозы: моделей", len(fc["models"]))
    except Exception as e:  # noqa: BLE001
        print("прогнозы не загрузились:", e)
    try:
        ens = load("ens.json", {})
        upd = dt.datetime.fromisoformat(ens.get("updated", "2000-01-01T00:00+00:00"))
        if (now - upd).total_seconds() > 170 * 60:
            new = collect_ens(now)
            if new:
                ens = new
                save("ens.json", ens)
                print("ансамбли: источников", ens["models"])
        if fc.get("cities") and ens.get("cities"):
            fc = merge_ens(fc, ens)
            save("forecast.json", fc)
    except Exception as e:  # noqa: BLE001
        print("ансамбли не загрузились:", e)
    markets = None
    try:
        markets = collect_markets(today)
        for m in markets:
            watch["%s|%s" % (m["c"], m["date"])] = 1
        save("markets.json", {"updated": now.isoformat(timespec="minutes"), "markets": markets})
        print("рынков:", len(markets))
    except Exception as e:  # noqa: BLE001
        print("рынки не загрузились:", e)
    try:
        forecasts = freeze_forecasts(forecasts, fc, today)
        stats = resolve(forecasts, stats, results, watch, calib, today)
    except Exception as e:  # noqa: BLE001
        print("итоги не посчитались:", e)
    try:
        skips = []
        if markets and fc.get("cities"):
            sigs, skips = build_signals(markets, fc, calib, fetch_obs(markets, fc, now), now, sigs)
            save("skips.json", {"updated": now.isoformat(timespec="minutes"), "items": skips})
            bad = sorted({"%s %s≠%s" % (m["c"], m["st"], ICAO.get(m["c"])) for m in markets if m.get("st") and ICAO.get(m["c"]) and m["st"] != ICAO[m["c"]]})
            if bad:
                print("ВНИМАНИЕ, станция рынка не совпадает с нашей:", ", ".join(bad))
        sigs = settle_signals(sigs, results)
        cut = (today - dt.timedelta(days=30)).isoformat()
        sigs = [s for s in sigs if s["date"] >= cut]
        save("bets.json", bets_from(sigs, load("bets.json", [])))
        print("сигналов:", len(sigs), "открытых:", sum(1 for s in sigs if s["status"] == "open"))
    except Exception as e:  # noqa: BLE001
        print("сигналы не посчитались:", type(e).__name__, e)
    save("signals.json", sigs)
    save("forecasts.json", forecasts)
    save("stats.json", stats)
    save("results.json", results)
    save("watch.json", watch)
    save("calib.json", calib)
    print("итогов в истории:", len(stats))


if __name__ == "__main__":
    main()
