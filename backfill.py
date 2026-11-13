"""Pulse Sniper: быстрое обучение по истории.
Берёт архив прогнозов Open-Meteo за последние ~2 недели и фактические показания станций (METAR),
считает, насколько каждая модель ошибалась в каждом городе, и дописывает это в data/calib.json.
Строки помечены bf=1: по ним учим поправку (bias) и веса моделей, но НЕ уверенность (sigma): архивные прогнозы
чуть точнее настоящих «за сутки», и это сделало бы систему излишне самоуверенной."""
import datetime as dt
import math
import time

import collect as C

HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
DAYS = 15
MODELS_BF = ["ecmwf_ifs025", "ecmwf_aifs025_single", "icon_seamless", "gfs_seamless", "ukmo_seamless",
             "gem_seamless", "jma_seamless", "cma_grapes_global", "ecmwf_ifs"]


def fetch_forecasts(today):
    """{город: {дата: {модель: значение}}} и сдвиги часовых поясов."""
    start, end = (today - dt.timedelta(days=DAYS)).isoformat(), (today - dt.timedelta(days=1)).isoformat()
    cs = C.CITIES
    lat = ",".join(str(c[2]) for c in cs)
    lon = ",".join(str(c[3]) for c in cs)
    out, offs = {}, {}
    for key in MODELS_BF:
        url = "%s?latitude=%s&longitude=%s&start_date=%s&end_date=%s&daily=temperature_2m_max&models=%s&timezone=auto" % (HIST, lat, lon, start, end, key)
        data = None
        for _ in range(2):
            try:
                data = C.get(url)
                break
            except Exception as e:  # noqa: BLE001
                print("архив", key, "не загрузился:", type(e).__name__, getattr(e, "code", ""))
                time.sleep(6)
        if data is None:
            continue
        data = data if isinstance(data, list) else [data]
        for c, o in zip(cs, data):
            daily = (o or {}).get("daily") or {}
            arr = daily.get("temperature_2m_max_" + key) or daily.get("temperature_2m_max")
            times = daily.get("time")
            if not arr or not times:
                continue
            offs[c[0]] = int((o or {}).get("utc_offset_seconds") or 0)
            for t, v in zip(times, arr):
                if isinstance(v, (int, float)):
                    out.setdefault(c[0], {}).setdefault(t, {})[key] = float(v)
        time.sleep(3)
        print("архив", key, "ок")
    return out, offs


def fetch_actuals(offs, today):
    """{город: {дата: максимум по станции}} за последние дни (только полные сутки)."""
    rows = []
    for c in C.CITIES:  # по одной станции: общий запрос обрезается (около 400 записей на всех)
        if not c[4]:
            continue
        got = None
        for hours in (336, 240):
            try:
                got = C.get("%s?ids=%s&format=json&hours=%d" % (C.METAR, c[4], hours))
                if isinstance(got, list) and got:
                    break
            except Exception as e:  # noqa: BLE001
                print("METAR", c[4], hours, "ч не загрузился:", type(e).__name__, getattr(e, "code", ""))
        if isinstance(got, list):
            rows += got
            print("METAR", c[4], len(got), "записей")
        time.sleep(1)
    by_icao = {c[4]: c[0] for c in C.CITIES if c[4]}
    days = {}
    for r in rows if isinstance(rows, list) else []:
        city, t, ts = by_icao.get(r.get("icaoId")), r.get("temp"), r.get("obsTime")
        if not city or not isinstance(t, (int, float)) or not isinstance(ts, (int, float)):
            continue
        ldt = dt.datetime.fromtimestamp(ts, dt.timezone.utc) + dt.timedelta(seconds=offs.get(city, 0))
        days.setdefault(city, {}).setdefault(ldt.date().isoformat(), []).append((ldt.hour, t))
    out = {}
    for city, dd in days.items():
        for d, obs in dd.items():
            hrs = {h for h, _ in obs}
            if d >= today.isoformat() or len(hrs) < 18 or not any(12 <= h <= 17 for h in hrs):
                continue  # день неполный: максимум мог быть недосчитан
            out.setdefault(city, {})[d] = max(t for _, t in obs)
    return out


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    calib = C.load("calib.json", {})
    fc, offs = fetch_forecasts(today)
    act = fetch_actuals(offs, today)
    added, errs, mae = 0, [], {}
    for city, days in act.items():
        rows = calib.setdefault(city, [])
        have = {r["d"] for r in rows}
        for d, a in sorted(days.items()):
            m = (fc.get(city) or {}).get(d)
            if not m or d in have:
                continue
            cons = C.consensus(m)
            if not cons:
                continue
            mu, sd, _ = cons
            if abs(a - mu) > 6:  # явная нестыковка данных (сбой станции или архива): такой день не берём
                continue
            rows.append({"d": d, "mu": round(mu, 2), "sd": round(sd, 2), "a": float(a), "m": {k: round(v, 1) for k, v in m.items()}, "bf": 1})
            added += 1
            errs.append(a - mu)
            for k, v in m.items():
                mae.setdefault(k, []).append(abs(v - a))
        rows.sort(key=lambda r: r["d"])
        calib[city] = rows[-60:]
    if added:
        C.save("calib.json", calib)
    print("добавлено строк:", added, "городов:", len(act))
    if errs:
        mean = sum(errs) / len(errs)
        print("средняя ошибка (факт − прогноз): %+.2f °C, разброс %.2f" % (mean, math.sqrt(sum((e - mean) ** 2 for e in errs) / len(errs))))
        for k, v in sorted(mae.items(), key=lambda x: sum(x[1]) / len(x[1])):
            print("  %-26s средняя ошибка %.2f °C (%d дней)" % (k, sum(v) / len(v), len(v)))


if __name__ == "__main__":
    main()
