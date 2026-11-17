"""Pulse Sniper: честный прогон стратегии по истории (до 180 дней).
Для каждого закрытого рынка: прогноз моделей (архив Open-Meteo), цены вариантов накануне в 12:00 UTC
(история цен Polymarket), три соседних варианта вокруг прогноза, итог по выигравшему варианту.
Ограничения: архивные прогнозы чуть точнее настоящих; цена = последняя сделка + 1 п.п. на спред."""
import datetime as dt
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor

import collect as C

DAYS = 180
HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
CLOB = "https://clob.polymarket.com/prices-history?market=%s&interval=max&fidelity=60"
MODELS = ["ecmwf_ifs025", "icon_seamless", "gfs_seamless", "ukmo_seamless"]
SPREAD = 0.01
T0 = time.time()


def forecasts(today):
    start, end = (today - dt.timedelta(days=DAYS)).isoformat(), (today - dt.timedelta(days=1)).isoformat()
    out = {c[0]: {} for c in C.CITIES}
    for key in MODELS:
        for c in C.CITIES:
            url = "%s?latitude=%s&longitude=%s&start_date=%s&end_date=%s&daily=temperature_2m_max&models=%s&timezone=auto" % (HIST, c[2], c[3], start, end, key)
            o = None
            for _ in range(2):
                try:
                    o = C.get(url)
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(3)
            if not o:
                continue
            dd = o.get("daily") or {}
            arr = dd.get("temperature_2m_max_" + key) or dd.get("temperature_2m_max") or []
            for t, v in zip(dd.get("time") or [], arr):
                if v is not None:
                    out[c[0]].setdefault(t, {})[key] = v
        print("прогнозы", key, round(time.time() - T0))
    return out


def price_at(tok, ts):
    for _ in range(2):
        try:
            h = (C.get(CLOB % tok).get("history")) or []
            best = None
            for x in h:
                if x["t"] <= ts and ts - x["t"] <= 8 * 3600:
                    best = x["p"]
            return best
        except Exception:  # noqa: BLE001
            time.sleep(1)
    return None


def event_bins(args):
    cid, slug, d = args
    ev = C.fetch_event(C.slug_for(slug, d))
    if not ev or not ev.get("closed"):
        return None
    bins = []
    for mk in ev.get("markets") or []:
        parsed = C.parse_bucket(mk.get("groupItemTitle") or mk.get("question") or "")
        p = C.yes_price(mk)
        try:
            tok = json.loads(mk.get("clobTokenIds") or "[]")[0]
        except Exception:  # noqa: BLE001
            tok = None
        if parsed and p is not None and tok:
            lab, lo, hi, c = parsed
            bins.append({"l": lab, "lo": lo, "hi": hi, "c": c, "tok": tok, "win": p >= 0.95})
    if not any(b["win"] for b in bins):
        return None
    bins.sort(key=lambda b: b["lo"])
    return {"c": cid, "d": d, "bins": bins}


def fetch_prices(ev):
    d = dt.date.fromisoformat(ev["d"]) if isinstance(ev["d"], str) else ev["d"]
    ts = int(dt.datetime(d.year, d.month, d.day, 12, tzinfo=dt.timezone.utc).timestamp()) - 86400
    for b in ev["need"]:
        b["price"] = price_at(b["tok"], ts)
    return ev


def corridor(bins, mu, sigma):
    best = None
    for j in range(len(bins) - 2):
        w = bins[j:j + 3]
        if any(b.get("price") is None for b in w):
            continue
        pm = sum(C.cdf((b["hi"] - mu) / sigma) - C.cdf((b["lo"] - mu) / sigma) for b in w)
        mid = sum(b["price"] for b in w)
        cost = sum(min(0.99, b["price"] + SPREAD) for b in w)
        p = 0.6 * pm + 0.4 * mid
        if best is None or p > best["p"]:
            best = {"p": p, "pm": pm, "cost": cost, "mid": mid, "hit": any(b["win"] for b in w), "j": j}
    return best


def stat(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    pnl = sum((1 - r["cost"]) if r["hit"] else -r["cost"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    return {"n": n, "hit": round(sum(r["hit"] for r in rows) / n, 3), "roi": round(pnl / cost, 3), "avg_cost": round(cost / n, 3)}


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    fc = forecasts(today)
    jobs = [(c[0], c[1], today - dt.timedelta(days=k)) for c in C.CITIES for k in range(1, DAYS + 1)]
    with ThreadPoolExecutor(12) as ex:
        evs = [e for e in ex.map(event_bins, jobs) if e]
    print("закрытых событий", len(evs), round(time.time() - T0))
    for e in evs:
        e["d"] = e["d"].isoformat()
        f = fc[e["c"]].get(e["d"]) or {}
        vals = [f[k] for k in MODELS if k in f]
        e["mu"] = sum(vals) / len(vals) if len(vals) >= 3 else None
        e["sd"] = (sum((v - e["mu"]) ** 2 for v in vals) / len(vals)) ** 0.5 if e["mu"] is not None else None
        e["need"] = [b for b in e["bins"] if e["mu"] is not None and abs(b["c"] - e["mu"]) <= 3.5]
    evs = [e for e in evs if e["need"]]
    with ThreadPoolExecutor(16) as ex:
        evs = list(ex.map(fetch_prices, evs))
    print("цены загружены", round(time.time() - T0))
    # поправка «идём вперёд»: средняя ошибка модели за 20 предыдущих дней города (без заглядывания)
    by = {}
    for e in evs:
        w = next(b for b in e["bins"] if b["win"])
        e["wc"] = w["c"]
    for cid in {e["c"] for e in evs}:
        rows = sorted([e for e in evs if e["c"] == cid], key=lambda e: e["d"])
        hist = []
        for e in rows:
            e["bias"] = sum(hist[-20:]) / len(hist[-20:]) if len(hist) >= 5 else 0.0
            hist.append(e["wc"] - e["mu"])
    res = {"strict": [], "current": [], "loose": [], "market": [], "all": [], "current_bias": [], "strict_bias": []}
    per_city = {}
    for e in evs:
        for use_bias in (False, True):
            mu = e["mu"] + (e["bias"] if use_bias else 0.0)
            sigma = max(1.1, math.sqrt(1.0 + (e["sd"] or 0) ** 2))
            bins = [b for b in e["bins"] if b.get("price") is not None]
            c = corridor(e["bins"], mu, sigma)
            if not c:
                continue
            r = dict(c, city=e["c"], d=e["d"], edge=c["p"] - c["cost"])
            suf = "_bias" if use_bias else ""
            if r["cost"] <= 0.97 and r["p"] >= 0.5 and r["edge"] >= 0:
                res["strict" + suf].append(r)
            if r["cost"] <= 1.0 and r["p"] >= 0.55 and r["edge"] >= -0.10 and (e["sd"] or 0) <= 1.3:
                res["current" + suf].append(r)
                if use_bias:
                    per_city.setdefault(e["c"], []).append(r)
            if not use_bias:
                res["all"].append(r)
                if r["cost"] <= 1.0 and r["p"] >= 0.5:
                    res["loose"].append(r)
    # рынок сам по себе: коридор из трёх вариантов с максимальной суммой цен, без прогноза
    for e in evs:
        bs = e["bins"]
        best = None
        for j in range(len(bs) - 2):
            w = bs[j:j + 3]
            if any(b.get("price") is None for b in w):
                continue
            mid = sum(b["price"] for b in w)
            if best is None or mid > best[0]:
                best = (mid, w)
        if best:
            cost = sum(min(0.99, b["price"] + SPREAD) for b in best[1])
            res["market"].append({"cost": cost, "hit": any(b["win"] for b in best[1]), "city": e["c"], "d": e["d"]})
    summary = {k: stat(v) for k, v in res.items()}
    months = {}
    for r in res["current_bias"]:
        months.setdefault(r["d"][:7], []).append(r)
    summary["by_month_current_bias"] = {k: stat(v) for k, v in sorted(months.items())}
    summary["by_city_current_bias"] = {k: stat(v) for k, v in sorted(per_city.items())}
    summary["events"] = len(evs)
    summary["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    C.save("backtest.json", summary)


if __name__ == "__main__":
    main()
