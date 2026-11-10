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
MODELS = [
    ("ecmwf_ifs025", 1.3, "ECMWF IFS", "Европейский центр"),
    ("ecmwf_aifs025_single", 1.2, "ECMWF AIFS", "ИИ-модель ECMWF"),
    ("icon_seamless", 1.1, "ICON", "Германия, DWD (EU, D2)"),
    ("gfs_seamless", 1.0, "GFS", "США, NOAA (HRRR)"),
    ("ukmo_seamless", 1.0, "UKMO", "Великобритания, Met Office"),
    ("meteofrance_seamless", 0.9, "Météo-France", "ARPEGE, AROME"),
    ("gem_seamless", 0.8, "GEM", "Канада, ECCC"),
    ("jma_seamless", 0.6, "JMA", "Япония, GSM, MSM"),
    ("kma_seamless", 0.5, "KMA", "Корея, GDPS"),
    ("cma_grapes_global", 0.4, "CMA GRAPES", "Китай"),
    ("bom_access_global", 0.4, "BoM ACCESS", "Австралия"),
]
MODEL_W = {m[0]: m[1] for m in MODELS}
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
MAX_PER_DAY = 3       # сигналов на одну дату рынка
MIN_MODELS = 4
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
            out.append({"l": label, "lo": round(lo, 3), "hi": round(hi, 3), "p": round(price, 3), "c": round(centre, 2)})
    return out


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


def consensus(vals):
    pairs = [(v, MODEL_W.get(k, 0.5)) for k, v in as_dict(vals).items() if isinstance(v, (int, float)) and math.isfinite(v)]
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


def predict(vals, lead, calib_city=(0.0, None, 0)):
    c = consensus(vals)
    if not c:
        return None
    mu, sdm, n = c
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


def corridor(vals, lead=1, calib_city=(0.0, None, 0)):
    pr = predict(vals, lead, calib_city)
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
                markets.append({"c": cid, "date": d.isoformat(), "vol": vol_label(ev.get("volume")), "b": b})
    return markets


# ---------- прогнозы ----------
def collect_forecast_all(now):
    """Все модели по координатам станций. Каждая модель отдельным запросом: одна неудача не ломает остальные."""
    import time
    lat = ",".join(str(c[2]) for c in CITIES)
    lon = ",".join(str(c[3]) for c in CITIES)
    maps, offsets, used = {}, {}, []
    for key, w, name, sub in MODELS:
        url = "%s?latitude=%s&longitude=%s&daily=temperature_2m_max&models=%s&forecast_days=7&timezone=auto" % (METEO, lat, lon, key)
        try:
            data = get(url)
        except Exception as e:  # noqa: BLE001
            print("модель", key, "не загрузилась:", type(e).__name__)
            continue
        data = data if isinstance(data, list) else [data]
        got = 0
        for c, o in zip(CITIES, data):
            daily = (o or {}).get("daily") or {}
            arr = daily.get("temperature_2m_max_" + key) or daily.get("temperature_2m_max")
            times = daily.get("time")
            if not arr or not times:
                continue
            maps.setdefault(c[0], {})[key] = {t: v for t, v in zip(times, arr) if isinstance(v, (int, float))}
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
def build_signals(markets, fc, calib, obs, now, sigs):
    """Единые правила. Новый сигнал фиксируется один раз: цена и вероятность остаются теми, что были в момент выдачи."""
    have = {s["key"] for s in sigs}
    per_date = {}
    for s in sigs:
        per_date[s["date"]] = per_date.get(s["date"], 0) + 1
    fresh = []
    for m in markets:
        key = "%s|%s" % (m["c"], m["date"])
        if key in have:
            continue
        vals = (fc.get("cities") or {}).get(m["c"])
        if not vals:
            continue
        d = dt.date.fromisoformat(m["date"])
        ln = local_now(now, fc, m["c"])
        lead = (d - ln.date()).days
        if lead < 0 or lead > 2:
            continue
        i = (fc.get("dates") or []).index(m["date"]) if m["date"] in (fc.get("dates") or []) else None
        if i is None:
            continue
        pr = predict({k: arr[i] for k, arr in vals.items() if i < len(arr)}, lead, calib_of(calib, m["c"]))
        if not pr or pr["sd"] > MAX_SD:
            continue
        floor = None
        mu, sigma = pr["mu"], pr["sigma"]
        if lead == 0 and ICAO.get(m["c"]):
            if m["c"] in obs:
                floor = obs[m["c"]] - 1.0
                f = clamp((17 - (ln.hour + ln.minute / 60.0)) / 10.0, 0.3, 1.0)  # к вечеру максимум дня почти известен
                sigma = max(0.5, sigma * f)
                mu = max(mu, obs[m["c"]])
                pr = dict(pr, mu=mu, sigma=sigma)
            elif ln.hour >= 12:
                continue  # день идёт, а наблюдений нет: не рискуем
        pms = [(bucket_prob(b["lo"], b["hi"], mu, sigma, floor), b) for b in m["b"]]
        top2 = sorted((p for p, _ in pms), reverse=True)[:2]
        best = None
        for pm, b in pms:
            if not (MIN_PRICE <= b["p"] <= MAX_PRICE) or pm < min(top2):
                continue
            p = MODEL_SHARE * pm + (1 - MODEL_SHARE) * b["p"]
            e = p - b["p"]
            if best is None or e > best[2]:
                best = (b, p, e, pm)
        if best and best[1] >= MIN_PM and best[2] >= MIN_EDGE:
            b, p, e, pm = best
            fresh.append({"key": key, "city": m["c"], "date": m["date"], "label": b["l"], "lo": b["lo"], "hi": b["hi"],
                          "price": b["p"], "pm": round(p, 3), "pm_model": round(pm, 3), "mu": round(pr["mu"], 2),
                          "sigma": round(pr["sigma"], 2), "sd": round(pr["sd"], 2), "n": pr["n"], "edge": round(e, 3),
                          "level": "strong" if e >= 0.10 and pr["sd"] <= 0.8 and p >= 0.4 else "mid",
                          "first": now.isoformat(timespec="minutes"), "status": "open",
                          "url": EVENT_URL + slug_for(next(c[1] for c in CITIES if c[0] == m["c"]), d)})
    fresh.sort(key=lambda s: -s["edge"])
    for s in fresh:
        if per_date.get(s["date"], 0) >= MAX_PER_DAY:
            continue
        per_date[s["date"]] = per_date.get(s["date"], 0) + 1
        sigs.append(s)
    return sigs


def settle_signals(sigs, results):
    for s in sigs:
        r = results.get(s["key"])
        if s["status"] == "open" and r:
            s["win"], s["hit"], s["status"] = r["win"], s["label"] == r["win"], "done"
    return sigs


def bets_from(sigs, old):
    keep = {(b["date"], b["city"]): b for b in old}
    for s in sigs:
        if s["status"] == "done":
            keep[(s["date"], s["city"])] = {"date": s["date"], "city": s["city"], "label": s["label"], "win": s["win"],
                                              "hit": s["hit"], "price": s["price"], "pm": s["pm"], "edge": s["edge"], "level": s["level"]}
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
            rows.append({"d": day, "mu": round(cs[0], 2), "sd": round(cs[1], 2), "a": win[0]["c"]})
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
        if markets and fc.get("cities"):
            sigs = build_signals(markets, fc, calib, fetch_obs(markets, fc, now), now, sigs)
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
