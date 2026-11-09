"""Pulse Sniper: уведомления в Telegram.
1. Принимает /start из бота и привязывает чат к заявке.
2. Находит сигналы (та же математика, что в приложении) и рассылает:
   одобренным рефералам каждый сигнал, остальным один бесплатный в неделю.
3. Когда рынок закрылся, присылает итог. В воскресенье вечером присылает статистику недели.
Секреты берутся из переменных окружения GitHub: BOT_TOKEN, SB_URL, SB_KEY, SB_SECRET.
"""
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import urllib.request

import collect

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SB_URL = os.environ.get("SB_URL", "").rstrip("/")
SB_KEY = os.environ.get("SB_KEY", "")
SB_SECRET = os.environ.get("SB_SECRET", "")
REF_URL = "https://pulsemarket.trade/?ref=4h5ipy"
EVENT_URL = "https://pulsemarket.trade/ru/event/"
NAMES = {
    "dallas": "Даллас", "shenzhen": "Шэньчжэнь", "jeddah": "Джидда", "london": "Лондон", "paris": "Париж",
    "telaviv": "Тель-Авив", "helsinki": "Хельсинки", "amsterdam": "Амстердам", "munich": "Мюнхен",
    "chongqing": "Чунцин", "nyc": "Нью-Йорк", "hongkong": "Гонконг", "milan": "Милан", "moscow": "Москва",
    "sf": "Сан-Франциско", "miami": "Майами", "madrid": "Мадрид", "la": "Лос-Анджелес", "houston": "Хьюстон",
    "singapore": "Сингапур", "tokyo": "Токио", "warsaw": "Варшава",
}
MON = ["янв.", "февр.", "марта", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."]
MIN_EDGE, MIN_PM, MAX_RANGE, MIN_PRICE = 0.08, 0.3, 3, 0.05


# ---------- сеть ----------
def post(url, body, headers=None):
    h = {"Content-Type": "application/json", "User-Agent": "pulse-sniper-bot"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def tg(method, body):
    return post("https://api.telegram.org/bot%s/%s" % (BOT_TOKEN, method), body)


def rpc(fn, body):
    h = {"apikey": SB_KEY}
    if SB_KEY.startswith("eyJ"):
        h["Authorization"] = "Bearer " + SB_KEY
    return post("%s/rest/v1/rpc/%s" % (SB_URL, fn), body, h)


def send(chat, text, button=None):
    body = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if button:
        body["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "url": button[1]}]]}
    try:
        tg("sendMessage", body)
        return True
    except Exception as e:  # noqa: BLE001  (чат заблокировал бота и т.п.)
        print("не отправлено:", type(e).__name__)
        return False


def hid(chat):
    """В публичном репозитории чаты храним только в виде хэша."""
    return hashlib.sha256((SB_SECRET + str(chat)).encode()).hexdigest()[:12]


# ---------- время ----------
def week_of(d):
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def dlabel(d):
    return "%d %s" % (d.day, MON[d.month - 1])


# ---------- математика сигналов ----------
def cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def model(vals, lead):
    pairs = [(v, w) for v, (_, w) in zip(vals, collect.MODELS) if v is not None]
    if len(pairs) < 2:
        return None
    W = sum(w for _, w in pairs)
    mu = sum(v * w for v, w in pairs) / W
    sdm = math.sqrt(sum(w * (v - mu) ** 2 for v, w in pairs) / W)
    sigma = math.sqrt(sdm ** 2 + (0.6 + 0.3 * lead) ** 2)
    return mu, sigma, max(v for v, _ in pairs) - min(v for v, _ in pairs)


def fresh_forecasts():
    lat = ",".join(str(c[2]) for c in collect.CITIES)
    lon = ",".join(str(c[3]) for c in collect.CITIES)
    url = "%s?latitude=%s&longitude=%s&daily=temperature_2m_max&models=%s&forecast_days=4&timezone=auto" % (
        collect.METEO, lat, lon, ",".join(m for m, _ in collect.MODELS))
    data = collect.get(url)
    data = data if isinstance(data, list) else [data]
    out = {}
    for (cid, _, _, _), o in zip(collect.CITIES, data):
        daily = o.get("daily") or {}
        for i, day in enumerate(daily.get("time") or []):
            vals = []
            for m, _ in collect.MODELS:
                arr = daily.get("temperature_2m_max_" + m) or []
                vals.append(arr[i] if i < len(arr) else None)
            out["%s|%s" % (cid, day)] = vals
    return out


def find_signals(markets, forecasts, today):
    sigs = []
    slugs = {c[0]: c[1] for c in collect.CITIES}
    for m in markets:
        d = dt.date.fromisoformat(m["date"])
        lead = (d - today).days
        if lead < 0 or lead > 2:
            continue
        f = model(forecasts.get("%s|%s" % (m["c"], m["date"])) or [], lead)
        if not f:
            continue
        mu, sigma, rng = f
        best = None
        for b in m["b"]:
            pm = cdf((b["hi"] - mu) / sigma) - cdf((b["lo"] - mu) / sigma)
            e = pm - b["p"]
            if b["p"] >= MIN_PRICE and (best is None or e > best[2]):
                best = (b, pm, e)
        if best and best[2] >= MIN_EDGE and best[1] >= MIN_PM and rng <= MAX_RANGE:
            b, pm, e = best
            sigs.append({"key": "%s|%s" % (m["c"], m["date"]), "city": m["c"], "date": m["date"], "label": b["l"],
                         "pm": round(pm, 3), "price": b["p"], "edge": round(e, 3), "strong": e >= 0.15 and rng <= 2,
                         "url": EVENT_URL + collect.slug_for(slugs[m["c"]], d)})
    sigs.sort(key=lambda s: -s["edge"])
    return sigs


# ---------- тексты ----------
def esc(x):
    return html.escape(str(x))


def explain(label):
    """Что значит вариант простыми словами: условие победы и перевод в градусы Цельсия."""
    m = re.match(r"^(-?\d+)[–-](-?\d+)°F$", label)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return "от %d до %d°F" % (a, b), "≈%d–%d°C" % (round((a - 32) / 1.8), round((b - 32) / 1.8)), True
    m = re.match(r"^(-?\d+(?:\.\d+)?)°([CF]) и (ниже|выше)$", label)
    if m:
        v, u, low = m.group(1), m.group(2), m.group(3) == "ниже"
        conv = "≈%d°C" % round((float(v) - 32) / 1.8) if u == "F" else ""
        return "%s°%s или %s" % (v, u, "холоднее" if low else "теплее"), conv, False
    m = re.match(r"^(-?\d+(?:\.\d+)?)°C$", label)
    if m:
        return "ровно %s°C" % m.group(1), "", False
    return label, "", False


def money(x):
    return ("$%.2f" % x).replace(".", ",")


def signal_text(s, member):
    d = dt.date.fromisoformat(s["date"])
    cond, conv, rng = explain(s["label"])
    head = "⚡ <b>Сильный сигнал</b>" if s["strong"] else "⚡ <b>Сигнал</b>"
    price = s["price"]
    t = "%s · %s · %s\n\n" % (head, esc(NAMES.get(s["city"], s["city"])), dlabel(d))
    t += "👉 <b>Ставка: «ДА» на «%s»</b>\n" % esc(s["label"])
    if conv:
        t += "(%s)\n" % esc(conv)
    t += "Это один вариант из списка рынка%s, ставится одной кнопкой.\n\n" % (" (диапазон из двух градусов)" if rng else "")
    t += "Выиграет, если максимум дня будет <b>%s</b>. При любой другой температуре ставка сгорает.\n\n" % esc(cond)
    t += "Модели: %d%% · рынок: %d%% · преимущество +%d п.п.\n" % (round(s["pm"] * 100), round(price * 100), round(s["edge"] * 100))
    if price > 0:
        t += "Цена «Да» %d¢: ставка $10 при выигрыше вернёт ≈ %s.\n" % (round(price * 100), money(10 / price))
    t += "\n<b>Как поставить:</b>\n1. Нажмите кнопку «Открыть рынок» ниже.\n2. Найдите строку «%s».\n3. Выберите «Да» (Yes) и введите сумму." % esc(s["label"])
    if member:
        return t + "\n\n✅ Можно ставить. Итог пришлю, когда рынок закроется."
    return (t + "\n\n🎁 Это бесплатный сигнал недели. Постоянные сигналы получают рефералы владельца сервиса на Pulse Market: "
            "зарегистрируйтесь по ссылке и отправьте заявку в приложении.")


def outcome_text(rec, win, hit):
    d = dt.date.fromisoformat(rec["date"])
    head = "✅ <b>Ставка выиграла</b>" if hit else "❌ <b>Ставка не сыграла</b>"
    t = "%s\n%s · %s\n\nНаш вариант: <b>%s</b>\nИтог рынка: <b>%s</b>" % (
        head, esc(NAMES.get(rec["city"], rec["city"])), dlabel(d), esc(rec["label"]), esc(win))
    if not hit:
        t += "\n\nСигнал не гарантирует результат: рынок закрылся на другом варианте."
    return t


def week_text(rows, open_n):
    n = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    t = "📊 <b>Итоги недели</b>\n\nСигналов закрыто: %d\n✅ Выиграло: %d\n❌ Не сыграло: %d\nДоля выигравших: %d%%" % (
        n, hits, n - hits, round(100 * hits / n) if n else 0)
    priced = [r for r in rows if r.get("price")]
    if priced and len(priced) == n:
        roi = sum((10 / r["price"] - 10) if r["hit"] else -10 for r in priced)
        t += "\n\nУсловный итог, если ставить по $10 на каждый сигнал по цене из сигнала: <b>%s%s</b> (без комиссий)." % (
            "+" if roi >= 0 else "−", money(abs(roi)))
    if open_n:
        t += "\nЕщё ждут итога: %d" % open_n
    return t


# ---------- шаги ----------
def process_updates(st):
    try:
        res = tg("getUpdates", {"offset": st.get("offset", 0), "timeout": 0, "allowed_updates": ["message"]})
    except Exception as e:  # noqa: BLE001
        print("getUpdates:", type(e).__name__)
        return
    for u in res.get("result", []):
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        if not chat or not text.startswith("/start"):
            continue
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            try:
                r = rpc("bot_link", {"p_secret": SB_SECRET, "p_token": parts[1].strip(), "p_chat": chat})
            except Exception:  # noqa: BLE001
                r = "error"
            if r == "ok":
                send(chat, "✅ Уведомления включены. Сигналы, итоги сделок и статистика недели будут приходить сюда.")
            else:
                send(chat, "Не удалось подключить уведомления. Откройте приложение Pulse Sniper и нажмите «Включить уведомления» ещё раз.")
        else:
            send(chat, "Это бот Pulse Sniper. Откройте приложение, перейдите в Кабинет и нажмите «Включить уведомления».")


def run(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date()
    wk = week_of(today)
    st = collect.load("bot.json", {})
    st.setdefault("sent", {})
    st.setdefault("free_week", {})
    st.setdefault("log", [])
    process_updates(st)
    try:
        subs = rpc("bot_subscribers", {"p_secret": SB_SECRET}) or []
    except Exception as e:  # noqa: BLE001
        print("подписчики:", type(e).__name__)
        subs = []
    members = {hid(s["chat_id"]): s["chat_id"] for s in subs if s.get("status") == "approved"}
    others = {hid(s["chat_id"]): s["chat_id"] for s in subs if s.get("status") != "approved"}
    everyone = dict(others)
    everyone.update(members)

    # новые сигналы
    try:
        markets = collect.load("markets.json", {}).get("markets", [])
        sigs = find_signals(markets, fresh_forecasts(), today)
    except Exception as e:  # noqa: BLE001
        print("сигналы:", type(e).__name__, e)
        sigs = []
    for s in sigs:
        if s["key"] in st["sent"]:
            continue
        rec = {"city": s["city"], "date": s["date"], "label": s["label"], "pm": s["pm"], "price": s["price"], "to": []}
        for h, chat in members.items():
            if send(chat, signal_text(s, True), ("Открыть на Pulse Market", s["url"])):
                rec["to"].append(h)
        for h, chat in others.items():
            if st["free_week"].get(h) == wk:
                continue
            if send(chat, signal_text(s, False), ("Регистрация на Pulse Market", REF_URL)):
                rec["to"].append(h)
                st["free_week"][h] = wk
        st["sent"][s["key"]] = rec
        print("сигнал", s["key"], "получателей:", len(rec["to"]))

    # итоги
    results = collect.load("results.json", {})
    bets = collect.load("bets.json", [])
    for key, rec in list(st["sent"].items()):
        r = results.get(key)
        if not r or rec.get("done"):
            continue
        hit = rec["label"] == r["win"]
        for h in rec["to"]:
            if h in everyone:
                send(everyone[h], outcome_text(rec, r["win"], hit))
        rec["done"] = True
        rec["hit"] = hit
        st["log"].append({"date": rec["date"], "week": week_of(dt.date.fromisoformat(rec["date"])), "hit": hit, "price": rec.get("price")})
        bets.append({"date": rec["date"], "city": rec["city"], "label": rec["label"], "win": r["win"], "hit": hit,
                     "price": rec.get("price"), "pm": rec.get("pm")})

    # недельная статистика: воскресенье, после 17:00 UTC
    if now.weekday() == 6 and now.hour >= 17 and st.get("last_summary") != wk:
        rows = [x for x in st["log"] if x["week"] == wk]
        open_n = sum(1 for r in st["sent"].values() if not r.get("done") and week_of(dt.date.fromisoformat(r["date"])) == wk)
        if rows:
            for chat in everyone.values():
                send(chat, week_text(rows, open_n))
        st["last_summary"] = wk

    # чистим старое
    cut = (today - dt.timedelta(days=21)).isoformat()
    st["sent"] = {k: v for k, v in st["sent"].items() if v["date"] >= cut}
    st["log"] = [x for x in st["log"] if x["date"] >= cut]
    st["free_week"] = {h: w for h, w in st["free_week"].items() if w == wk}
    cut90 = (today - dt.timedelta(days=90)).isoformat()
    collect.save("bets.json", [b for b in bets if b["date"] >= cut90])
    collect.save("bot.json", st)


if __name__ == "__main__":
    if not (BOT_TOKEN and SB_URL and SB_KEY and SB_SECRET):
        print("Секреты бота не заданы, уведомления пропущены.")
    else:
        try:
            run()
        except Exception as e:  # noqa: BLE001
            print("ошибка уведомлений:", type(e).__name__, e)
