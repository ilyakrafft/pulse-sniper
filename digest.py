"""Pulse Sniper: недельная сводка для владельца.
Читает data/bets.json (закрытые сигналы), считает, где система работает, а где нет,
даёт простые советы и присылает в Telegram. Сохраняет data/digest.json.
Переменные: BOT_TOKEN, OWNER_CHAT (ваш id в Telegram; если не задан, сводка только сохраняется)."""
import datetime as dt
import html
import json
import os
import urllib.request

import collect
from notify import NAMES

TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER = os.environ.get("OWNER_CHAT", "").strip()
MIN_N = 4  # меньше сигналов: вывод по городу не делаем


def pct(a, b):
    return round(100 * a / b) if b else 0


def group(rows, key):
    g = {}
    for r in rows:
        g.setdefault(key(r), []).append(r)
    return g


def stat(rows):
    n = len(rows)
    h = sum(1 for r in rows if r.get("hit"))
    return n, h, pct(h, n)


def build(bets, today):
    d7 = (today - dt.timedelta(days=7)).isoformat()
    d14 = (today - dt.timedelta(days=14)).isoformat()
    wk = [b for b in bets if b["date"] >= d7]
    prev = [b for b in bets if d14 <= b["date"] < d7]
    n, h, p = stat(wk)
    out = {"date": today.isoformat(), "week": {"n": n, "hit": h, "pct": p}, "prev": dict(zip(("n", "hit", "pct"), stat(prev))),
           "total": dict(zip(("n", "hit", "pct"), stat(bets))), "cities": [], "levels": [], "tips": []}
    for c, rows in group(bets, lambda b: b["city"]).items():
        cn, ch, cp = stat(rows)
        out["cities"].append({"city": c, "n": cn, "hit": ch, "pct": cp})
    for lv, rows in group(bets, lambda b: b.get("level", "mid")).items():
        ln, lh, lp = stat(rows)
        out["levels"].append({"level": lv, "n": ln, "hit": lh, "pct": lp})
    tests = [t for t in collect.load("test_signals.json", []) if t.get("status") == "done"]
    if tests:
        def grp(rows):
            k = sum(1 for t in rows if t.get("hit"))
            c = sum(t.get("price") or 0 for t in rows)
            return {"n": len(rows), "hit": k, "pct": pct(k, len(rows)), "exp": round(100 * sum(t.get("pm") or 0 for t in rows) / len(rows)) if rows else 0,
                    "roi": round(100 * (k - c) / c) if c else 0}
        out["test"] = {"all": grp(tests), "cheap": grp([t for t in tests if (t.get("price") or 0) < 0.8]),
                       "mid": grp([t for t in tests if 0.8 <= (t.get("price") or 0) <= 1.0]), "dear": grp([t for t in tests if (t.get("price") or 0) > 1.0])}
    out["cities"].sort(key=lambda x: (-x["pct"], -x["n"]))
    tn = out["total"]["n"]
    tips = out["tips"]
    if tn < 30:
        tips.append("Данных пока мало (%d закрытых сигналов). Пороги не меняем: нужно хотя бы 30–40." % tn)
    else:
        bad = [c for c in out["cities"] if c["n"] >= MIN_N and c["pct"] < max(30, out["total"]["pct"] - 25)]
        good = [c for c in out["cities"] if c["n"] >= MIN_N and c["pct"] >= out["total"]["pct"] + 15]
        for c in bad:
            tips.append("%s: %d%% при %d сигналах, заметно хуже среднего. Стоит ослабить доверие к этому городу или временно отключить." % (NAMES.get(c["city"], c["city"]), c["pct"], c["n"]))
        for c in good:
            tips.append("%s: %d%% при %d сигналах, лучший город. Можно оставить как есть." % (NAMES.get(c["city"], c["city"]), c["pct"], c["n"]))
        lv = {x["level"]: x for x in out["levels"]}
        if lv.get("light") and lv.get("strong") and lv["light"]["n"] >= MIN_N and lv["strong"]["n"] >= MIN_N and lv["light"]["pct"] >= lv["strong"]["pct"]:
            tips.append("«Осторожные» сигналы работают не хуже сильных. Уровни надёжности стоит пересмотреть.")
        if out["prev"]["n"] >= MIN_N and n >= MIN_N and p < out["prev"]["pct"] - 15:
            tips.append("Неделя заметно хуже прошлой (%d%% против %d%%). Пришлите эту сводку в чат, разберём причину." % (p, out["prev"]["pct"]))
        if not tips:
            tips.append("Явных проблем не видно. Продолжаем наблюдать.")
    return out


def text(o):
    w, t, p = o["week"], o["total"], o["prev"]
    s = "🧭 <b>Сводка для владельца</b>\n\n"
    s += "<b>За 7 дней:</b> закрыто %d, выиграло %d (%d%%)\n" % (w["n"], w["hit"], w["pct"])
    if p["n"]:
        s += "Неделей ранее: %d из %d (%d%%)\n" % (p["hit"], p["n"], p["pct"])
    s += "<b>Всего за всё время:</b> %d из %d (%d%%)\n" % (t["hit"], t["n"], t["pct"])
    rated = [c for c in o["cities"] if c["n"] >= MIN_N]
    if rated:
        s += "\n<b>По городам</b> (от %d сигналов):\n" % MIN_N
        for c in rated:
            s += "%s %s: %d%% (%d из %d)\n" % ("🟢" if c["pct"] >= 60 else "🟡" if c["pct"] >= 40 else "🔴", NAMES.get(c["city"], c["city"]), c["pct"], c["hit"], c["n"])
    names = {"strong": "Сильные", "mid": "Умеренные", "light": "Осторожные"}
    lv = [x for x in o["levels"] if x["n"]]
    if lv:
        s += "\n<b>По уровням:</b> " + ", ".join("%s %d%% (%d)" % (names.get(x["level"], x["level"]), x["pct"], x["n"]) for x in lv) + "\n"
    ts = o.get("test")
    if ts and ts["all"]["n"] >= 8:
        s += "\n<b>Широкий тест</b> (не для ставок): %d закрыто, сыграло %d%%, мы ждали %d%%\n" % (ts["all"]["n"], ts["all"]["pct"], ts["all"]["exp"])
        for k, nm in (("cheap", "дешёвые до 80¢"), ("mid", "80–100¢"), ("dear", "дороже 100¢")):
            g = ts[k]
            if g["n"]:
                s += "• %s: %d из %d (%d%%), прибыль на вложенное %+d%%\n" % (nm, g["hit"], g["n"], g["pct"], g["roi"])
    s += "\n<b>Что видно</b>\n" + "\n".join("• " + html.escape(x) for x in o["tips"])
    s += "\n\nПерешлите это сообщение в чат с Claude, и я предложу точные правки."
    return s


def main():
    bets = collect.load("bets.json", [])
    o = build(bets, dt.date.today())
    collect.save("digest.json", o)
    msg = text(o)
    print(msg)
    if TOKEN and OWNER:
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % TOKEN, method="POST",
                                     data=json.dumps({"chat_id": OWNER, "text": msg, "parse_mode": "HTML"}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20)
        except Exception as e:  # noqa: BLE001
            print("не отправлено:", type(e).__name__, e)
    else:
        print("OWNER_CHAT не задан: сводка только сохранена.")


if __name__ == "__main__":
    main()
