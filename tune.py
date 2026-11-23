"""Pulse Sniper: самонастройка по реальным результатам.
Раз в неделю смотрит закрытые сигналы (data/bets.json) и осторожно подправляет правила в data/tuning.json:
  • строгость отбора (min_corr_p) и число сигналов в день (max_per_day) — не больше одного шага за запуск, в жёстких границах;
  • временная пауза для города, где результаты явно слабые (на 14 дней, потом снова проверяем).
Пока закрыто меньше 30 сигналов, ничего не меняет: данных мало. Все изменения пишутся в журнал, который видно в приложении."""
import datetime as dt

import collect as C
from notify import NAMES

NEED = 30            # сигналов нужно для первой настройки (считаем только сигналы по текущим правилам, см. since в tuning.json)
WINDOW = 20          # по скольким последним сигналам судим об общем качестве
# Судим по прибыли на вложенное (ROI), а не по доле попаданий: коридор попадает в 80–90% случаев, и по доле попаданий
# система всегда «смягчала» отбор, хотя при цене около 100% это ведёт в минус.
ROI_LOW, ROI_HIGH, RATE_HIGH = -0.08, 0.15, 0.80
P_MIN, P_MAX, P_STEP = 0.60, 0.85, 0.02
D_MIN, D_MAX = 3, 5
CITY_MIN_N, CITY_MAX_RATE = 6, 0.25
PAUSE_DAYS = 14
MAX_PAUSED = 3        # одновременно на паузе не больше трёх городов
DEFAULT = {"min_corr_p": 0.70, "max_per_day": 4}


def roi_of(rows):
    """Прибыль на вложенное: ставка на коридор по цене из сигнала, выплата 1."""
    pts = [(1.0 / b["price"] - 1.0) if b.get("hit") else -1.0 for b in rows if b.get("price")]
    return sum(pts) / len(pts) if pts else 0.0


def clamp(x, a, b):
    return max(a, min(b, x))


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    tn = C.load("tuning.json", {}) or {}
    since = tn.get("since") or ""
    bets = sorted([b for b in C.load("bets.json", []) if b["date"] >= since], key=lambda b: b["date"])
    params = dict(DEFAULT, **(tn.get("params") or {}))
    paused = {c: u for c, u in (tn.get("paused") or {}).items() if u >= today.isoformat()}
    log = list(tn.get("log") or [])
    ver = int(tn.get("version") or 0)
    changes = []
    n = len(bets)
    if n >= NEED:
        last = bets[-WINDOW:]
        rate = sum(1 for b in last if b.get("hit")) / len(last)
        roi = roi_of(last)
        if roi < ROI_LOW:
            p2, d2 = round(clamp(params["min_corr_p"] + P_STEP, P_MIN, P_MAX), 2), int(clamp(params["max_per_day"] - 1, D_MIN, D_MAX))
            if (p2, d2) != (params["min_corr_p"], params["max_per_day"]):
                changes.append("Последние %d сигналов: попало %d%%, прибыль на вложенное %+d%%. Отбор стал строже (порог шанса %d%%, не больше %d сигналов в день)." % (len(last), round(rate * 100), round(roi * 100), round(p2 * 100), d2))
                params.update(min_corr_p=p2, max_per_day=d2)
        elif roi > ROI_HIGH and rate >= RATE_HIGH:
            p2, d2 = round(clamp(params["min_corr_p"] - P_STEP, P_MIN, P_MAX), 2), int(clamp(params["max_per_day"] + 1, D_MIN, D_MAX))
            if (p2, d2) != (params["min_corr_p"], params["max_per_day"]):
                changes.append("Последние %d сигналов: попало %d%%, прибыль на вложенное %+d%%. Отбор стал мягче (порог шанса %d%%, до %d сигналов в день)." % (len(last), round(rate * 100), round(roi * 100), round(p2 * 100), d2))
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
    out = {"version": ver, "updated": today.isoformat(), "since": since, "status": "active" if n >= NEED else "learning", "closed": n, "need": NEED,
           "params": params, "paused": paused, "log": log}
    C.save("tuning.json", out)
    print("закрыто:", n, "версия:", ver, "параметры:", params, "паузы:", paused)
    for t in changes:
        print("•", t)


if __name__ == "__main__":
    main()
