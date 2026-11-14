"""Pulse Sniper: самонастройка по реальным результатам.
Раз в неделю смотрит закрытые сигналы (data/bets.json) и осторожно подправляет правила в data/tuning.json:
  • строгость отбора (min_corr_p) и число сигналов в день (max_per_day) — не больше одного шага за запуск, в жёстких границах;
  • временная пауза для города, где результаты явно слабые (на 14 дней, потом снова проверяем).
Пока закрыто меньше 30 сигналов, ничего не меняет: данных мало. Все изменения пишутся в журнал, который видно в приложении."""
import datetime as dt

import collect as C
from notify import NAMES

NEED = 30            # сигналов нужно для первой настройки
WINDOW = 20          # по скольким последним сигналам судим об общем качестве
LOW, HIGH = 0.35, 0.55
P_MIN, P_MAX, P_STEP = 0.45, 0.60, 0.02
D_MIN, D_MAX = 3, 5
CITY_MIN_N, CITY_MAX_RATE = 6, 0.25
PAUSE_DAYS = 14
MAX_PAUSED = 3        # одновременно на паузе не больше трёх городов
DEFAULT = {"min_corr_p": 0.50, "max_per_day": 4}


def clamp(x, a, b):
    return max(a, min(b, x))


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    bets = sorted(C.load("bets.json", []), key=lambda b: b["date"])
    tn = C.load("tuning.json", {}) or {}
    params = dict(DEFAULT, **(tn.get("params") or {}))
    paused = {c: u for c, u in (tn.get("paused") or {}).items() if u >= today.isoformat()}
    log = list(tn.get("log") or [])
    ver = int(tn.get("version") or 0)
    changes = []
    n = len(bets)
    if n >= NEED:
        last = bets[-WINDOW:]
        rate = sum(1 for b in last if b.get("hit")) / len(last)
        if rate < LOW:
            p2, d2 = round(clamp(params["min_corr_p"] + P_STEP, P_MIN, P_MAX), 2), int(clamp(params["max_per_day"] - 1, D_MIN, D_MAX))
            if (p2, d2) != (params["min_corr_p"], params["max_per_day"]):
                changes.append("Последние %d сигналов сыграли на %d%%: отбор стал строже (порог шанса %d%%, не больше %d сигналов в день)." % (len(last), round(rate * 100), round(p2 * 100), d2))
                params.update(min_corr_p=p2, max_per_day=d2)
        elif rate > HIGH:
            p2, d2 = round(clamp(params["min_corr_p"] - P_STEP, P_MIN, P_MAX), 2), int(clamp(params["max_per_day"] + 1, D_MIN, D_MAX))
            if (p2, d2) != (params["min_corr_p"], params["max_per_day"]):
                changes.append("Последние %d сигналов сыграли на %d%%: отбор стал мягче (порог шанса %d%%, до %d сигналов в день)." % (len(last), round(rate * 100), round(p2 * 100), d2))
                params.update(min_corr_p=p2, max_per_day=d2)
        by = {}
        for b in bets:
            by.setdefault(b["city"], []).append(b)
        for city, rows in by.items():
            r = rows[-10:]
            if len(paused) >= MAX_PAUSED:
                break  # не отключаем сразу много городов: слабые дни бывают общими
            if city not in paused and len(r) >= CITY_MIN_N and sum(1 for b in r if b.get("hit")) / len(r) <= CITY_MAX_RATE:
                paused[city] = (today + dt.timedelta(days=PAUSE_DAYS)).isoformat()
                changes.append("%s на паузе до %s: сыграло %d из %d последних сигналов." % (NAMES.get(city, city), paused[city], sum(1 for b in r if b.get("hit")), len(r)))
    if changes:
        ver += 1
        for t in changes:
            log.append({"d": today.isoformat(), "t": t})
        log = log[-12:]
    out = {"version": ver, "updated": today.isoformat(), "status": "active" if n >= NEED else "learning", "closed": n, "need": NEED,
           "params": params, "paused": paused, "log": log}
    C.save("tuning.json", out)
    print("закрыто:", n, "версия:", ver, "параметры:", params, "паузы:", paused)
    for t in changes:
        print("•", t)


if __name__ == "__main__":
    main()
