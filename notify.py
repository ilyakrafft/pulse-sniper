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
import sys
import urllib.request

import collect

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SB_URL = os.environ.get("SB_URL", "").rstrip("/")
SB_KEY = os.environ.get("SB_KEY", "")
SB_SECRET = os.environ.get("SB_SECRET", "")
REF_URL = "https://pulsemarket.trade/?ref=4h5ipy"
APP_URL = "https://ilyakrafft.github.io/pulse-sniper/"
BOT_NAME = "pulsniper_bot"
BOT_APP = os.environ.get("BOT_APP", "")  # короткое имя Mini App из BotFather (необязательно)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
REF_RE = re.compile(r"https://pulsemarket\.trade/[^\s]*ref=[A-Za-z0-9_\-]{3,40}[^\s]*")
EVENT_URL = "https://pulsemarket.trade/ru/event/"
NAMES = {
    "dallas": "Даллас", "shenzhen": "Шэньчжэнь", "jeddah": "Джидда", "london": "Лондон", "paris": "Париж",
    "telaviv": "Тель-Авив", "helsinki": "Хельсинки", "amsterdam": "Амстердам", "munich": "Мюнхен",
    "chongqing": "Чунцин", "nyc": "Нью-Йорк", "hongkong": "Гонконг", "milan": "Милан", "moscow": "Москва",
    "sf": "Сан-Франциско", "miami": "Майами", "madrid": "Мадрид", "la": "Лос-Анджелес", "houston": "Хьюстон",
    "singapore": "Сингапур", "tokyo": "Токио", "warsaw": "Варшава",
}
MON = ["янв.", "февр.", "марта", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."]


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


def find_signals(today):
    """Сигналы считает collect.py и записывает в data/signals.json. Бот и приложение берут одни и те же записи."""
    out = []
    for r in collect.load("signals.json", []):
        if r.get("status") != "open" or r["date"] < (today - dt.timedelta(days=1)).isoformat():
            continue
        until = r.get("until") or (r["date"] + "T12:00+00:00")
        if dt.datetime.now(dt.timezone.utc) > dt.datetime.fromisoformat(until):
            continue  # время ставить на этот рынок прошло
        out.append({"key": r["key"], "city": r["city"], "date": r["date"], "label": r["label"], "pm": r["pm"], "price": r["price"],
                    "edge": r["edge"], "strong": r.get("level") == "strong", "url": r.get("url") or EVENT_URL,
                    "kind": r.get("kind", "single"), "labels": r.get("labels") or [r["label"]], "legs": r.get("legs") or [], "st": r.get("st")})
    out.sort(key=lambda s: -s["edge"])
    return out


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


def corr_text(s, member):
    d = dt.date.fromisoformat(s["date"])
    head = "⚡ <b>Сильный сигнал · коридор</b>" if s["strong"] else "⚡ <b>Сигнал · коридор</b>"
    cost = s["price"]
    t = "%s · %s · %s\n\n" % (head, esc(NAMES.get(s["city"], s["city"])), dlabel(d))
    t += "👉 <b>Ставьте «ДА» сразу на три соседних варианта:</b>\n"
    n = 10 / cost if cost > 0 else 0
    for leg in s["legs"]:
        t += "• <b>%s</b>: цена %d¢, сумма %s\n" % (esc(leg["l"]), round(leg["price"] * 100), money(n * leg["price"]))
    t += "\nСуммы даны для вложения $10 в коридор. Выплата будет одинаковой при любом из трёх исходов.\n\n"
    t += "✅ Если максимум дня попадёт в <b>любой</b> из трёх вариантов, вернётся ≈ %s (прибыль %s).\n" % (money(n), money(n - 10))
    t += "❌ Если мимо, потеряете вложенные $10.\n\n"
    t += "Шанс попасть: <b>%d%%</b> · коридор стоит %d%% (выгодно при шансе выше) · преимущество +%d п.п.\n" % (round(s["pm"] * 100), round(cost * 100), round(s["edge"] * 100))
    if s.get("st"):
        t += "Станция, по которой закроется рынок: %s.\n" % esc(s["st"])
    t += "\n<b>Как поставить:</b>\n1. Нажмите «Открыть рынок» ниже.\n2. В каждой из трёх строк выберите «Да» (Yes) и введите свою сумму."
    if member:
        return t + "\n\n✅ Можно ставить. Итог пришлю, когда рынок закроется."
    return (t + "\n\n🎁 Это бесплатный сигнал недели. Постоянные сигналы получают рефералы владельца сервиса на Pulse Market: "
            "зарегистрируйтесь по ссылке и отправьте заявку в приложении.")


def signal_text(s, member):
    if s.get("kind") == "corr":
        return corr_text(s, member)
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
    t += "Наша оценка: %d%% · цена рынка: %d%% · преимущество +%d п.п.\n" % (round(s["pm"] * 100), round(price * 100), round(s["edge"] * 100))
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
    ours = ", ".join(rec.get("labels") or [rec["label"]])
    t = "%s\n%s · %s\n\nНаш %s: <b>%s</b>\nИтог рынка: <b>%s</b>" % (
        head, esc(NAMES.get(rec["city"], rec["city"])), dlabel(d), "коридор" if rec.get("kind") == "corr" else "вариант", esc(ours), esc(win))
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
def mail_hash(e):
    return hashlib.sha256(e.strip().lower().encode("utf-8")).hexdigest()


def partner_link(code):
    if BOT_APP:
        return "https://t.me/%s/%s?startapp=%s" % (BOT_NAME, BOT_APP, code)
    return "%s?p=%s" % (APP_URL, code)


PARTNER_HELP = ("Как открывать доступ вашим рефералам:\n"
                "1. Откройте на Pulse Market список ваших рефералов.\n"
                "2. Скопируйте почты и пришлите их мне сообщением (можно сразу много, в любом виде).\n"
                "3. Все, кто в списке, получат доступ сами, а я напишу им об этом.\n\n"
                "Сами почты я не сохраняю, храню только их необратимые отпечатки. Повторять список можно в любой момент: уже добавленные не задваиваются.")


def handle_message(msg):
    text = (msg.get("text") or "").strip()
    chat = (msg.get("chat") or {}).get("id")
    uid = (msg.get("from") or {}).get("id")
    if not chat or not text:
        return
    if text.startswith("/start"):
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
            send(chat, "Это бот Pulse Sniper. Откройте приложение, перейдите в Кабинет и нажмите «Включить уведомления».\n\n"
                       "Есть своя реферальная ссылка Pulse Market и хотите приглашать людей в приложение? Пришлите:\n/partner ваша_ссылка")
        return
    if text.startswith("/owner"):
        parts = text.split(maxsplit=1)
        try:
            r = rpc("bot_owner", {"p_secret": SB_SECRET, "p_tg": uid, "p_code": parts[1].strip() if len(parts) == 2 else ""})
        except Exception:  # noqa: BLE001
            r = "error"
        try:
            tg("deleteMessage", {"chat_id": chat, "message_id": msg.get("message_id")})
        except Exception:  # noqa: BLE001
            pass
        send(chat, "✅ Вы подтверждены как владелец. Теперь можно присылать список почт ваших рефералов." if r == "ok"
             else "Код не подошёл.")
        return
    if text.startswith("/partner") or text.startswith("/mylink"):
        m = REF_RE.search(text)
        if not m:
            send(chat, "Пришлите команду вместе с вашей реферальной ссылкой Pulse Market, например:\n/partner https://pulsemarket.trade/?ref=ВАШ_КОД")
            return
        try:
            code = rpc("bot_partner", {"p_secret": SB_SECRET, "p_tg": uid, "p_ref": m.group(0)})
        except Exception:  # noqa: BLE001
            code = "error"
        if code == "not_member":
            send(chat, "Партнёром можно стать после входа в приложение: зарегистрируйтесь на Pulse Market по ссылке вашего пригласителя, откройте приложение и введите почту. Когда пригласитель вас добавит, вы сможете подключить свою ссылку.")
            return
        if not re.match(r"^[a-z0-9]{3,16}$", str(code)):
            send(chat, "Не удалось подключить ссылку. Проверьте, что она вида https://pulsemarket.trade/?ref=ВАШ_КОД, и пришлите ещё раз.")
            return
        send(chat, "✅ Вы подключены как партнёр.\n\nВаша ссылка для приглашения людей в приложение:\n%s\n\n%s" % (partner_link(code), PARTNER_HELP))
        return
    mails = sorted(set(e.lower() for e in EMAIL_RE.findall(text)))
    if mails:
        try:
            r = rpc("bot_add_emails", {"p_secret": SB_SECRET, "p_tg": uid, "p_hashes": [mail_hash(e) for e in mails]})
        except Exception:  # noqa: BLE001
            send(chat, "Не удалось добавить список. Попробуйте ещё раз чуть позже.")
            return
        if isinstance(r, dict) and r.get("error") == "no_partner":
            send(chat, "Сначала подключите вашу реферальную ссылку: /partner https://pulsemarket.trade/?ref=ВАШ_КОД")
            return
        if isinstance(r, dict) and r.get("error") in ("not_member", "too_many", "limit"):
            send(chat, "Список не принят: у вас нет прав партнёра или в списке слишком много почт (максимум 300 за раз).")
            return
        if isinstance(r, dict) and "added" in r:
            send(chat, "✅ Принято почт: %d. Новых рефералов: %d. Всего в вашем списке: %d.\nДоступ открыт у ожидавших: %d." % (
                len(mails), r["added"], r["total"], r["opened"]))
            try:
                tg("deleteMessage", {"chat_id": chat, "message_id": msg.get("message_id")})
            except Exception:  # noqa: BLE001
                pass
            return
        send(chat, "Не удалось добавить список. Попробуйте ещё раз чуть позже.")
        return


def process_updates(st):
    try:
        res = tg("getUpdates", {"offset": st.get("offset", 0), "timeout": 0, "allowed_updates": ["message"]})
    except Exception as e:  # noqa: BLE001
        print("getUpdates:", type(e).__name__)
        return
    for u in res.get("result", []):
        st["offset"] = u["update_id"] + 1
        try:
            handle_message(u.get("message") or {})
        except Exception as e:  # noqa: BLE001
            print("сообщение не обработано:", type(e).__name__)


def get_subs():
    try:
        return rpc("bot_subscribers", {"p_secret": SB_SECRET}) or []
    except Exception as e:  # noqa: BLE001
        print("подписчики:", type(e).__name__)
        return []


def welcome_new(st, subs):
    """Новым участникам (их почта появилась в списке) пишет, что доступ открыт."""
    cur = {hid(s["chat_id"]): s["chat_id"] for s in subs if s.get("status") == "approved"}
    known = set(st.get("members", []))
    if st.get("members_init"):
        for h, chat in cur.items():
            if h not in known:
                send(chat, "✅ <b>Доступ открыт!</b>\n\nТеперь вы будете получать все сигналы с пометкой «можно ставить», итоги сделок и статистику недели. "
                           "В приложении полный доступ уже включён.")
    st["members"] = sorted(cur)
    st["members_init"] = True


def run_chat(now=None):
    """Быстрый режим: только сообщения боту и приветствие новых участников."""
    st = collect.load("bot.json", {})
    process_updates(st)
    welcome_new(st, get_subs())
    collect.save("bot.json", st)


def run(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date()
    wk = week_of(today)
    st = collect.load("bot.json", {})
    st.setdefault("sent", {})
    st.setdefault("free_week", {})
    st.setdefault("log", [])
    process_updates(st)
    subs = get_subs()
    welcome_new(st, subs)
    refs = {hid(s["chat_id"]): s.get("ref") or REF_URL for s in subs}
    members = {hid(s["chat_id"]): s["chat_id"] for s in subs if s.get("status") == "approved"}
    others = {hid(s["chat_id"]): s["chat_id"] for s in subs if s.get("status") != "approved"}
    everyone = dict(others)
    everyone.update(members)

    # новые сигналы
    try:
        sigs = find_signals(today)
    except Exception as e:  # noqa: BLE001
        print("сигналы:", type(e).__name__, e)
        sigs = []
    for s in sigs:
        if s["key"] in st["sent"]:
            continue
        rec = {"city": s["city"], "date": s["date"], "label": s["label"], "pm": s["pm"], "price": s["price"], "to": [],
               "kind": s.get("kind", "single"), "labels": s.get("labels") or [s["label"]]}
        for h, chat in members.items():
            if send(chat, signal_text(s, True), ("Открыть на Pulse Market", s["url"])):
                rec["to"].append(h)
        for h, chat in others.items():
            if st["free_week"].get(h) == wk:
                continue
            if send(chat, signal_text(s, False), ("Регистрация на Pulse Market", refs.get(h, REF_URL))):
                rec["to"].append(h)
                st["free_week"][h] = wk
        st["sent"][s["key"]] = rec
        print("сигнал", s["key"], "получателей:", len(rec["to"]))

    # итоги
    results = collect.load("results.json", {})
    for key, rec in list(st["sent"].items()):
        r = results.get(key)
        if not r or rec.get("done"):
            continue
        hit = r["win"] in (rec.get("labels") or [rec["label"]])
        for h in rec["to"]:
            if h in everyone:
                send(everyone[h], outcome_text(rec, r["win"], hit))
        rec["done"] = True
        rec["hit"] = hit
        st["log"].append({"date": rec["date"], "week": week_of(dt.date.fromisoformat(rec["date"])), "hit": hit, "price": rec.get("price")})

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
    collect.save("bot.json", st)


if __name__ == "__main__":
    if not (BOT_TOKEN and SB_URL and SB_KEY and SB_SECRET):
        print("Секреты бота не заданы, уведомления пропущены.")
    else:
        try:
            if len(sys.argv) > 1 and sys.argv[1] == "chat":
                run_chat()
            else:
                run()
        except Exception as e:  # noqa: BLE001
            print("ошибка уведомлений:", type(e).__name__, e)
