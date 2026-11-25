#!/usr/bin/env python3
"""Теневая проверка категории «Крипто» (рынки Polymarket вида «цена выше X к дате»).

Ничего не показывает пользователям и не шлёт сигналов. Каждый час:
1. находит открытые рынки bitcoin/ethereum/solana/xrp «above on <дата>» на Polymarket;
2. берёт спотовую цену и волатильность с биржи (Binance, запасной вариант Coinbase);
3. считает вероятность модели P(цена > X в момент расчёта) и записывает её рядом с ценой рынка
   за 24 и за 6 часов до расчёта;
4. после закрытия рынка записывает итог и считает, кто точнее: модель или рынок (Brier).
Модель получит право на сигналы, только если она точнее рынка на достаточном числе закрытых рынков.

Дополнительно собирает обзор самых активных рынков других категорий (data/screener.json) без прогнозов.
Любая ошибка не должна ломать остальные workflow: скрипт всегда завершается кодом 0 и пишет статус.
"""
import json
import math
import pathlib
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import datetime as dt
from zoneinfo import ZoneInfo

DATA = pathlib.Path("data")
CDIR = DATA / "crypto"
GAMMA = "https://gamma-api.polymarket.com/events"
UA = {"User-Agent": "Mozilla/5.0 (pulse-sniper shadow)", "Accept": "application/json"}
ET = ZoneInfo("America/New_York")
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]

# slug-префикс рынка -> (метка, пара Binance, пара Coinbase)
ASSETS = {
    "bitcoin": ("BTC", "BTCUSDT", "BTC-USD"),
    "ethereum": ("ETH", "ETHUSDT", "ETH-USD"),
    "solana": ("SOL", "SOLUSDT", "SOL-USD"),
    "xrp": ("XRP", "XRPUSDT", "XRP-USD"),
}
WINDOWS = (24, 6)            # за сколько часов до расчёта фиксируем сравнение модели и рынка
RESOLVE_HOUR_ET = 12         # расчёт по свече 12:00 по Нью-Йорку (проверяется по тексту правил, см. rules.json)
FAT = 0.15                   # доля «толстых хвостов» в смеси: у криптовалют большие движения чаще, чем у нормального закона
FAT_SCALE = 3.0
MIN_EDGE = 0.06              # для условной проверки доходности: берём сторону, где модель лучше цены хотя бы на 6 п.п.
NEED_MARKETS = 100           # сколько закрытых рынков нужно для вердикта
NEED_EVENTS = 40             # и сколько разных событий (монета + день): страйки одного дня зависят друг от друга, это не независимые проверки
SCREENER_TAGS = ["politics", "sports", "crypto", "economy", "esports"]


# ---------- сеть ----------
def get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def safe_get(url):
    try:
        return get(url)
    except Exception:
        return None


def fetch_event(slug):
    d = safe_get(GAMMA + "?slug=" + urllib.parse.quote(slug))
    return d[0] if isinstance(d, list) and d else None


# ---------- математика ----------
def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def cdf_std(z):
    """Функция распределения доходности с единичной дисперсией: смесь двух нормальных (тяжёлые хвосты)."""
    a = 1.0 / math.sqrt((1 - FAT) + FAT * FAT_SCALE ** 2)
    return (1 - FAT) * phi(z / a) + FAT * phi(z / (a * FAT_SCALE))


def prob_above(spot, strike, sigma_h, tau_h):
    """Вероятность, что цена в момент расчёта выше strike. Дрейф считаем нулевым."""
    if spot <= 0 or strike <= 0 or sigma_h <= 0 or tau_h <= 0:
        return None
    s = sigma_h * math.sqrt(tau_h)
    return 1.0 - cdf_std(math.log(strike / spot) / s)


def hourly_sigma(closes):
    """Почасовая волатильность: полусумма экспоненциальной оценки (лямбда 0,97) и общей по всему окну."""
    r = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(r) < 48:
        return None
    var_long = sum(x * x for x in r) / len(r)
    lam, ew = 0.97, None
    for x in r:
        ew = x * x if ew is None else lam * ew + (1 - lam) * x * x
    return math.sqrt(0.5 * ew + 0.5 * var_long)


# ---------- цена и волатильность с биржи ----------
def series_binance(sym):
    for host in ("data-api.binance.vision", "api.binance.com"):
        d = safe_get("https://%s/api/v3/klines?symbol=%s&interval=1h&limit=500" % (host, sym))
        if isinstance(d, list) and len(d) > 100:
            try:
                return host, [float(k[4]) for k in d]
            except (ValueError, IndexError, TypeError):
                pass
    return None


def series_coinbase(pair):
    d = safe_get("https://api.exchange.coinbase.com/products/%s/candles?granularity=3600" % pair)
    if isinstance(d, list) and len(d) > 100:
        try:
            rows = sorted(d, key=lambda k: k[0])
            return "coinbase", [float(k[4]) for k in rows]
        except (ValueError, IndexError, TypeError):
            pass
    return None


def market_series(prefix):
    _, bsym, cpair = ASSETS[prefix]
    return series_binance(bsym) or series_coinbase(cpair)


# ---------- рынки Polymarket ----------
def parse_prices(m):
    v = m.get("outcomePrices")
    try:
        if isinstance(v, str):
            v = json.loads(v)
        return [float(x) for x in v]
    except (ValueError, TypeError):
        return None


def parse_strike(m):
    for src in (m.get("groupItemTitle"), m.get("question")):
        if not src:
            continue
        mm = re.search(r"above\s+\$?([\d,]+(?:\.\d+)?)", src, re.I) or re.search(r"([\d][\d,]*(?:\.\d+)?)", src)
        if mm:
            try:
                return float(mm.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def slug_date(slug, today):
    m = re.search(r"-above-on-([a-z]+)-(\d{1,2})(?:-(\d{4}))?$", slug)
    if not m or m.group(1) not in MONTHS:
        return None
    mon, day = MONTHS.index(m.group(1)) + 1, int(m.group(2))
    years = [int(m.group(3))] if m.group(3) else [today.year - 1, today.year, today.year + 1]
    best = None
    for y in years:
        try:
            d = dt.date(y, mon, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def resolve_time(d):
    """Момент расчёта: 12:00 по Нью-Йорку в дату рынка, в UTC."""
    return dt.datetime(d.year, d.month, d.day, RESOLVE_HOUR_ET, 0, tzinfo=ET).astimezone(dt.timezone.utc)


def candidate_slugs(now):
    out = []
    today = now.astimezone(ET).date()
    for off in range(-1, 4):
        d = today + dt.timedelta(days=off)
        for p in ASSETS:
            base = "%s-above-on-%s-%d" % (p, MONTHS[d.month - 1], d.day)
            out += [base, "%s-%d" % (base, d.year)]
    return out


def discover(now):
    """Все найденные события «above on» (по списку тега «crypto» и по вероятным адресам)."""
    found = {}
    lst = safe_get(GAMMA + "?tag_slug=crypto&active=true&closed=false&limit=200&order=endDate&ascending=true")
    for e in (lst if isinstance(lst, list) else []):
        s = e.get("slug", "")
        if re.match(r"^(%s)-above-on-" % "|".join(ASSETS), s):
            found[s] = e
    for s in candidate_slugs(now):
        if s not in found:
            e = fetch_event(s)
            if e:
                found[s] = e
            time.sleep(0.15)
    return found


# ---------- журнал ----------
def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def mkt_price(m):
    """Цена «Да»: середина между лучшей заявкой на покупку и продажу, иначе цена из outcomePrices."""
    try:
        b, a = float(m.get("bestBid")), float(m.get("bestAsk"))
        if 0 < b <= a < 1:
            return (a + b) / 2, b, a
    except (TypeError, ValueError):
        pass
    p = parse_prices(m)
    if p and 0 < p[0] < 1:
        return p[0], None, None
    return None, None, None


def snapshot_open(found, now, pending, status):
    series_cache = {}
    for slug, e in found.items():
        prefix = slug.split("-above-on-")[0]
        if prefix not in ASSETS or e.get("closed"):
            continue
        d = slug_date(slug, now.astimezone(ET).date())
        if not d:
            continue
        T = resolve_time(d)
        tau = (T - now).total_seconds() / 3600.0
        if tau <= 0.5 or tau > WINDOWS[0] + 1.5:
            continue
        if prefix not in series_cache:
            series_cache[prefix] = market_series(prefix)
            ser = series_cache[prefix]
            status["assets"][ASSETS[prefix][0]] = (
                {"ok": False} if not ser else
                {"ok": True, "source": ser[0], "spot": ser[1][-1], "sigma_day_pct": round(100 * (hourly_sigma(ser[1]) or 0) * math.sqrt(24), 2)})
        ser = series_cache[prefix]
        if not ser:
            continue
        spot, sig = ser[1][-1], hourly_sigma(ser[1])
        if not sig:
            continue
        if prefix not in status["rules"]:
            status["rules"][prefix] = {"slug": slug, "end_gamma": e.get("endDate"), "end_assumed": T.isoformat(),
                                       "resolutionSource": e.get("resolutionSource"),
                                       "text": (e.get("description") or (e.get("markets") or [{}])[0].get("description") or "")[:900]}
        for m in e.get("markets") or []:
            k = parse_strike(m)
            pm, bid, ask = mkt_price(m)
            if k is None or pm is None or m.get("closed"):
                continue
            key = slug + "|" + str(k)
            rec = pending.setdefault(key, {"asset": ASSETS[prefix][0], "slug": slug, "strike": k, "T": T.isoformat(), "snaps": {}})
            for w in WINDOWS:
                # окно пишем только рядом с его моментом (допуск 3 часа на задержки расписания GitHub)
                if w - 3 < tau <= w and str(w) not in rec["snaps"]:
                    pmod = prob_above(spot, k, sig, tau)
                    if pmod is None:
                        continue
                    rec["snaps"][str(w)] = {"t": now.isoformat(timespec="minutes"), "tau": round(tau, 2), "spot": spot,
                                            "sigma_h": round(sig, 6), "model": round(pmod, 4), "market": round(pm, 4),
                                            "bid": bid, "ask": ask, "src": ser[0]}


def settle(pending, resolved, now, status):
    for key in list(pending):
        rec = pending[key]
        T = dt.datetime.fromisoformat(rec["T"])
        if now < T + dt.timedelta(minutes=30):
            continue
        e = fetch_event(rec["slug"])
        outcome = None
        if e:
            for m in e.get("markets") or []:
                if parse_strike(m) == rec["strike"]:
                    p = parse_prices(m)
                    if m.get("closed") and p and max(p) >= 0.99:
                        outcome = 1 if p[0] >= 0.99 else 0
        if outcome is not None:
            if rec["snaps"]:
                resolved.append({"key": key, "asset": rec["asset"], "slug": rec["slug"], "strike": rec["strike"],
                                 "T": rec["T"], "outcome": outcome, "snaps": rec["snaps"]})
            del pending[key]
        elif now > T + dt.timedelta(days=3):
            status["dropped"] += 1
            del pending[key]


def score(resolved):
    out = {}
    for w in WINDOWS:
        rows = [r for r in resolved if str(w) in r["snaps"]]
        if not rows:
            out[str(w)] = {"n": 0}
            continue
        bm = sum((r["snaps"][str(w)]["model"] - r["outcome"]) ** 2 for r in rows) / len(rows)
        bk = sum((r["snaps"][str(w)]["market"] - r["outcome"]) ** 2 for r in rows) / len(rows)
        bets = []
        for r in rows:
            s = r["snaps"][str(w)]
            edge = s["model"] - s["market"]
            if abs(edge) < MIN_EDGE:
                continue
            if edge > 0:
                cost = min(0.99, (s["ask"] if s.get("ask") else s["market"] + 0.01))
                bets.append((1.0 / cost - 1.0) if r["outcome"] == 1 else -1.0)
            else:
                cost = min(0.99, (1 - s["bid"]) if s.get("bid") else (1 - s["market"] + 0.01))
                bets.append((1.0 / cost - 1.0) if r["outcome"] == 0 else -1.0)
        out[str(w)] = {"n": len(rows), "events": len({r["slug"] for r in rows}), "brier_model": round(bm, 4), "brier_market": round(bk, 4),
                       "bets": len(bets), "roi": round(sum(bets) / len(bets), 3) if bets else None}
    return out


def verdict(sc):
    s = sc.get("24", {"n": 0})
    if s["n"] < NEED_MARKETS or s.get("events", 0) < NEED_EVENTS:
        return "learning", "Нужно %d закрытых рынков (сейчас %d) и %d событий (сейчас %d)." % (NEED_MARKETS, s["n"], NEED_EVENTS, s.get("events", 0))
    better = s["brier_model"] < s["brier_market"] * 0.97
    profit = s.get("bets", 0) >= 30 and (s.get("roi") or -1) > 0
    if better and profit:
        return "model_better", "Модель точнее рынка и в плюсе на условных ставках. Можно обсуждать сигналы."
    return "market_better", "Модель пока не лучше рынка. Сигналов по крипто не будет."


# ---------- обзор рынков других категорий ----------
def screener(now):
    cats = {}
    for tag in SCREENER_TAGS:
        d = None
        for order in ("volume24hr", "volume"):
            d = safe_get(GAMMA + "?tag_slug=%s&active=true&closed=false&limit=12&order=%s&ascending=false" % (tag, order))
            if isinstance(d, list) and d:
                break
        items = []
        for e in (d if isinstance(d, list) else [])[:10]:
            ms = [m for m in (e.get("markets") or []) if not m.get("closed")]
            if not ms:
                continue
            try:
                top = max(ms, key=lambda m: float(m.get("volume") or 0))
            except (TypeError, ValueError):
                top = ms[0]
            p = parse_prices(top)
            items.append({"title": e.get("title"), "slug": e.get("slug"), "end": e.get("endDate"),
                          "vol24": round(float(e.get("volume24hr") or 0)), "n": len(ms),
                          "q": top.get("groupItemTitle") or top.get("question"), "yes": round(p[0], 3) if p else None})
        cats[tag] = items
        time.sleep(0.2)
    save(DATA / "screener.json", {"updated": now.isoformat(timespec="minutes"), "cats": cats})


def main():
    now = dt.datetime.now(dt.timezone.utc)
    status = {"updated": now.isoformat(timespec="minutes"), "assets": {}, "rules": {}, "dropped": 0, "errors": []}
    pending = load(CDIR / "pending.json", {})
    resolved = load(CDIR / "resolved.json", [])
    try:
        found = discover(now)
        status["found_events"] = len(found)
        snapshot_open(found, now, pending, status)
        settle(pending, resolved, now, status)
    except Exception:
        status["errors"].append(traceback.format_exc()[-600:])
    sc = score(resolved)
    v, text = verdict(sc)
    status.update({"pending": len(pending), "resolved": len(resolved), "scores": sc, "verdict": v, "verdict_text": text,
                   "need": {"markets": NEED_MARKETS, "events": NEED_EVENTS}})
    if status["rules"]:
        save(CDIR / "rules.json", status["rules"])
    save(CDIR / "pending.json", pending)
    save(CDIR / "resolved.json", resolved[-3000:])
    try:
        screener(now)
    except Exception:
        status["errors"].append("screener: " + traceback.format_exc()[-400:])
    save(CDIR / "status.json", status)
    print(json.dumps({k: status[k] for k in ("found_events", "pending", "resolved", "verdict", "errors") if k in status}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("crypto_shadow: необработанная ошибка\n" + traceback.format_exc())
