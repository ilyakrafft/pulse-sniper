"""Pulse Sniper: сбор данных.
Каждый запуск:
1. скачивает рынки максимальной температуры с Polymarket (те же, что на Pulse Market) -> data/markets.json
2. запоминает прогнозы 4 моделей на завтра и дальше -> data/forecasts.json
3. находит закрытые рынки, берёт фактическую температуру и сравнивает с нашим коридором -> data/stats.json
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
UA = {"User-Agent": "pulse-sniper-collector"}
DATA = pathlib.Path("data")
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
MODELS = [("ecmwf_ifs025", 1.3), ("icon_seamless", 1.1), ("gfs_seamless", 1.0), ("gem_seamless", 0.9)]
# id, slug в адресе события, широта, долгота
CITIES = [
    ("dallas", "dallas", 32.78, -96.8), ("shenzhen", "shenzhen", 22.54, 114.06),
    ("jeddah", "jeddah", 21.49, 39.19), ("london", "london", 51.51, -0.13),
    ("paris", "paris", 48.86, 2.35), ("telaviv", "tel-aviv", 32.09, 34.78),
    ("helsinki", "helsinki", 60.17, 24.94), ("amsterdam", "amsterdam", 52.37, 4.9),
    ("munich", "munich", 48.14, 11.58), ("chongqing", "chongqing", 29.56, 106.55),
    ("nyc", "nyc", 40.71, -74.0), ("hongkong", "hong-kong", 22.32, 114.17),
    ("milan", "milan", 45.46, 9.19), ("moscow", "moscow", 55.75, 37.62),
    ("sf", "san-francisco", 37.77, -122.42), ("miami", "miami", 25.76, -80.19),
    ("madrid", "madrid", 40.42, -3.7), ("la", "los-angeles", 34.05, -118.24),
    ("houston", "houston", 29.76, -95.37), ("singapore", "singapore", 1.35, 103.82),
    ("tokyo", "tokyo", 35.68, 139.69), ("warsaw", "warsaw", 52.23, 21.01),
]


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


# ---------- та же математика, что в приложении ----------
def cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def corridor(vals, lead=1):
    pairs = [(v, w) for v, (_, w) in zip(vals, MODELS) if v is not None]
    if len(pairs) < 2:
        return None
    W = sum(w for _, w in pairs)
    mu = sum(v * w for v, w in pairs) / W
    sdm = math.sqrt(sum(w * (v - mu) ** 2 for v, w in pairs) / W)
    sigma = math.sqrt(sdm ** 2 + (0.6 + 0.3 * lead) ** 2)
    k0 = int(math.floor(mu + 0.5))
    p = cdf((k0 + 1.5 - mu) / sigma) - cdf((k0 - 1.5 - mu) / sigma)
    rng = max(v for v, _ in pairs) - min(v for v, _ in pairs)
    return {"k": k0, "p": round(p, 3), "range": round(rng, 2)}


def collect_markets(today):
    markets = []
    for cid, slug, _, _ in CITIES:
        for add in range(0, 3):
            d = today + dt.timedelta(days=add)
            ev = fetch_event(slug_for(slug, d))
            if not ev or ev.get("closed"):
                continue
            b = sorted(buckets_of(ev), key=lambda x: -x["p"])
            if b:
                markets.append({"c": cid, "date": d.isoformat(), "vol": vol_label(ev.get("volume")), "b": b})
    return markets


def collect_forecasts(forecasts):
    lat = ",".join(str(c[2]) for c in CITIES)
    lon = ",".join(str(c[3]) for c in CITIES)
    url = "%s?latitude=%s&longitude=%s&daily=temperature_2m_max&models=%s&forecast_days=4&timezone=auto" % (
        METEO, lat, lon, ",".join(m for m, _ in MODELS))
    data = get(url)
    data = data if isinstance(data, list) else [data]
    for (cid, _, _, _), o in zip(CITIES, data):
        daily = o.get("daily") or {}
        times = daily.get("time") or []
        for i, day in enumerate(times):
            if i == 0:
                continue  # сегодняшний день не храним: нужен прогноз, сделанный заранее
            vals = []
            for m, _ in MODELS:
                arr = daily.get("temperature_2m_max_" + m) or []
                vals.append(arr[i] if i < len(arr) else None)
            forecasts["%s|%s" % (cid, day)] = vals
    return forecasts


def resolve(forecasts, stats, today):
    done = {(r["city"], r["date"]) for r in stats}
    slugs = {c[0]: c[1] for c in CITIES}
    for key in list(forecasts):
        cid, day = key.split("|")
        d = dt.date.fromisoformat(day)
        if d > today or (cid, day) in done:
            if (cid, day) in done:
                del forecasts[key]
            continue
        if (today - d).days > 7:
            del forecasts[key]
            continue
        ev = fetch_event(slug_for(slugs[cid], d))
        if not ev:
            continue
        win = [b for b in buckets_of(ev) if b["p"] >= 0.95]
        if not win:
            continue  # ещё не закрыт
        actual = int(math.floor(win[0]["c"] + 0.5))
        c = corridor(forecasts[key])
        if c and c["p"] >= 0.45 and c["range"] <= 3:  # только то, что показали бы как сигнал
            stats.append({"date": day, "city": cid, "k": c["k"], "p": c["p"], "actual": actual,
                          "hit": abs(actual - c["k"]) <= 1, "v": forecasts[key]})
        del forecasts[key]
    return stats


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    now = dt.datetime.now(dt.timezone.utc)
    forecasts = load("forecasts.json", {})
    stats = load("stats.json", [])
    try:
        forecasts = collect_forecasts(forecasts)
    except Exception as e:  # noqa: BLE001
        print("прогнозы не загрузились:", e)
    try:
        stats = resolve(forecasts, stats, today)
    except Exception as e:  # noqa: BLE001
        print("итоги не посчитались:", e)
    try:
        markets = collect_markets(today)
        save("markets.json", {"updated": now.isoformat(timespec="minutes"), "markets": markets})
        print("рынков:", len(markets))
    except Exception as e:  # noqa: BLE001
        print("рынки не загрузились:", e)
    save("forecasts.json", forecasts)
    save("stats.json", stats)
    print("итогов в истории:", len(stats))


if __name__ == "__main__":
    main()
