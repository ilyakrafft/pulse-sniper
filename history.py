"""Pulse Sniper: история закрытых рынков погоды за последние ~90 дней.
Для каждого города и дня берёт закрытое событие Polymarket, находит выигравший вариант (цена «Да» ≈ 1),
станцию из правил события и сравнивает с архивной температурой Open-Meteo в точке станции.
Результат: data/market_history.json (по дням) и data/market_history_summary.json (по городам)."""
import datetime as dt
import json
import time
from concurrent.futures import ThreadPoolExecutor

import collect as C

DAYS = 90
ARCH = "https://archive-api.open-meteo.com/v1/archive"


def winner_of(ev):
    best = None
    for mk in ev.get("markets") or []:
        p = C.yes_price(mk)
        parsed = C.parse_bucket(mk.get("groupItemTitle") or mk.get("question") or "")
        if p is None or not parsed:
            continue
        if best is None or p > best[0]:
            best = (p, parsed)
    if not best or best[0] < 0.95:
        return None
    label, lo, hi, centre = best[1]
    return {"l": label, "lo": round(lo, 2), "hi": round(hi, 2), "c": round(centre, 2)}


def one(args):
    cid, slug, d = args
    ev = None
    for _ in range(2):
        ev = C.fetch_event(C.slug_for(slug, d))
        if ev is not None:
            break
        time.sleep(1)
    if not ev:
        return None
    w = winner_of(ev) if ev.get("closed") else None
    return {"c": cid, "d": d.isoformat(), "closed": bool(ev.get("closed")), "w": w, "st": C.station_of(ev),
            "vol": C.num(ev.get("volume"))}


def archive(cid, lat, lon, start, end):
    url = "%s?latitude=%s&longitude=%s&start_date=%s&end_date=%s&daily=temperature_2m_max&timezone=auto" % (ARCH, lat, lon, start, end)
    try:
        o = C.get(url)
        o = o[0] if isinstance(o, list) else o
        dd = o["daily"]
        return dict(zip(dd["time"], dd["temperature_2m_max"]))
    except Exception as e:  # noqa: BLE001
        print("архив", cid, type(e).__name__)
        return {}


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    jobs = [(c[0], c[1], today - dt.timedelta(days=k)) for c in C.CITIES for k in range(1, DAYS + 1)]
    with ThreadPoolExecutor(8) as ex:
        rows = [r for r in ex.map(one, jobs) if r]
    start = (today - dt.timedelta(days=DAYS)).isoformat()
    end = (today - dt.timedelta(days=1)).isoformat()
    summary = {}
    for cid, slug, lat, lon, icao in C.CITIES:
        arch = archive(cid, lat, lon, start, end)
        mine = [r for r in rows if r["c"] == cid]
        won = [r for r in mine if r["w"]]
        diffs = []
        for r in won:
            a = arch.get(r["d"])
            r["arch"] = a
            if a is not None:
                diffs.append(r["c"] if False else r["w"]["c"] - a)
        stations = sorted({r["st"] for r in mine if r["st"]})
        n = len(diffs)
        mean = sum(diffs) / n if n else None
        inbin = sum(1 for r in won if r.get("arch") is not None and r["w"]["lo"] <= r["arch"] < r["w"]["hi"])
        summary[cid] = {"days_found": len(mine), "resolved": len(won), "stations": stations, "code": icao,
                        "mean_diff_vs_era5": round(mean, 2) if mean is not None else None,
                        "era5_in_winner_bin": inbin, "compared": n,
                        "winner_c_avg": round(sum(r["w"]["c"] for r in won) / len(won), 1) if won else None}
        print(cid, summary[cid])
    C.save("market_history.json", {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), "rows": rows})
    C.save("market_history_summary.json", summary)


if __name__ == "__main__":
    main()
