import datetime as dt, json
import collect as C
today = dt.datetime.now(dt.timezone.utc).date()
for ago in (10, 40, 80, 120, 170):
    d = today - dt.timedelta(days=ago)
    ev = C.fetch_event(C.slug_for("london", d))
    if not ev:
        print(ago, "нет события"); continue
    mk = (ev.get("markets") or [])[3]
    tok = json.loads(mk.get("clobTokenIds") or "[]")[0]
    for q in ("interval=max&fidelity=60", "interval=max&fidelity=720", "interval=max"):
        try:
            h = (C.get("https://clob.polymarket.com/prices-history?market=%s&%s" % (tok, q)).get("history")) or []
            ts = [x["t"] for x in h]
            print(ago, q, "точек", len(h), "от", dt.datetime.fromtimestamp(min(ts), dt.timezone.utc).isoformat()[:16] if ts else None,
                  "до", dt.datetime.fromtimestamp(max(ts), dt.timezone.utc).isoformat()[:16] if ts else None)
        except Exception as e:
            print(ago, q, "ошибка", type(e).__name__, getattr(e, "code", ""))
