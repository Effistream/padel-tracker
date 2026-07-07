#!/usr/bin/env python3
"""Padel tracker — hlídá volné kurty v Reenio a posílá Telegram notifikace.

Běží periodicky (GitHub Actions cron). Každý běh:
  1. stáhne termíny na HORIZON_DAYS dopředu z veřejného Reenio API,
  2. spočítá volnou kapacitu po 30min slotech,
  3. zpracuje Telegram příkazy (/filtr, /volno, /pauza, ...),
  4. porovná se snapshotem z minulého běhu a nahlásí uvolněné sloty,
  5. uloží nový stav do state.json (commituje workflow).

Bez TELEGRAM_BOT_TOKEN v prostředí běží v dry-run režimu (jen vypisuje).
"""

import json
import os
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_URL = "https://areal-cisarska-louka.reenio.cz"
RESERVATION_URL = f"{BASE_URL}/cs/service/hriste-padel-48086"
RESOURCE_ID = 48086
RESOURCE_TYPE = 3
HORIZON_DAYS = 7  # jedno volání Term/List s viewMode=7-days
SLOT_MINUTES = 30
TZ = ZoneInfo("Europe/Prague")
STATE_FILE = Path(__file__).parent / "state.json"

DAY_NAMES = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"]
DAY_TOKENS = {"po": 0, "ut": 1, "st": 2, "ct": 3, "pa": 4, "so": 5, "ne": 6}
DAY_ALIASES = {"vikend": [5, 6], "vsedni": [0, 1, 2, 3, 4]}

HELP_TEXT = (
    "🎾 Padel tracker — hlídám uvolněné kurty (Císařská louka).\n\n"
    "Příkazy:\n"
    "/filtr po-pa 16:00-22:00 — přidat časový filtr notifikací\n"
    "/filtr so,ne — přidat filtr (bez času = celý den)\n"
    "/filtry — vypsat aktivní filtry\n"
    "/smazfiltry — smazat všechny filtry (chodí pak vše)\n"
    "/volno — přehled volných slotů na 7 dní\n"
    "/pauza — pozastavit notifikace\n"
    "/start — obnovit notifikace\n"
    "/help — tato nápověda\n\n"
    "Dny: po ut st ct pa so ne, rozsah po-pa, výčet so,ne, "
    "aliasy vikend / vsedni. Odpovídám jen při pravidelné kontrole, "
    "takže reakce může trvat i ~10 minut."
)


# ---------------------------------------------------------------- Reenio API

def fetch_events(date_str):
    """Stáhne termíny od date_str na 7 dní dopředu."""
    params = urllib.parse.urlencode({
        "date": date_str,
        "viewMode": "7-days",
        "page": "0",
        "filter.resource[0].id": str(RESOURCE_ID),
        "filter.resource[0].type": str(RESOURCE_TYPE),
        "includeColors": "false",
        "findNearestAvailable": "false",
    })
    req = urllib.request.Request(
        f"{BASE_URL}/cs/api/Term/List",
        data=params.encode(),
        headers={"User-Agent": "padel-tracker (osobni notifikace volnych kurtu)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("status") != "OK":
        raise RuntimeError(f"Reenio API vrátilo status {payload.get('status')!r}")
    return payload["data"]["events"]


def parse_utc(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def parse_duration(text):
    """Reenio doba "1:00:00" / "2400:00:00" (H:MM:SS) → timedelta."""
    try:
        hours, minutes, seconds = (int(p) for p in text.split(":"))
        return timedelta(hours=hours, minutes=minutes, seconds=seconds)
    except (ValueError, AttributeError):
        return timedelta(0)


def build_slots(events, now):
    """Mapa {"YYYY-MM-DDTHH:MM": počet volných kurtů} po 30min slotech.

    Počítá jen sloty, které lze ještě rezervovat (Reenio uzavírá rezervace
    reservationDueDate před začátkem slotu); klíče jsou v čase Europe/Prague.
    """
    slots = {}
    for event in events:
        if not event.get("isVisible", True) or not event.get("isEnabled", True):
            continue
        capacity = event.get("maxCapacity") or 0
        if capacity <= 0:
            continue
        start = parse_utc(event["start"])
        end = parse_utc(event["end"])
        due = parse_duration(event.get("reservationDueDate") or "")
        reservations = [
            (parse_utc(r["start"]), parse_utc(r["end"]), r.get("capacity", 1))
            for r in event.get("reservations") or []
        ]
        slot = start
        step = timedelta(minutes=SLOT_MINUTES)
        while slot < end:
            slot_end = slot + step
            if slot > now + due:
                occupied = sum(c for s, e, c in reservations if s < slot_end and e > slot)
                key = slot.astimezone(TZ).strftime("%Y-%m-%dT%H:%M")
                slots[key] = slots.get(key, 0) + max(0, capacity - occupied)
            slot = slot_end
    return slots


# ---------------------------------------------------------------- filtry

def strip_diacritics(text):
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)
    )


def parse_days(spec):
    """"po-pa" / "so,ne" / "vikend" → seznam dnů (Po=0). None při chybě."""
    days = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part in DAY_ALIASES:
            days.update(DAY_ALIASES[part])
        elif "-" in part:
            a, _, b = part.partition("-")
            if a not in DAY_TOKENS or b not in DAY_TOKENS:
                return None
            a, b = DAY_TOKENS[a], DAY_TOKENS[b]
            if a <= b:
                days.update(range(a, b + 1))
            else:  # rozsah přes neděli, např. so-po
                days.update(range(a, 7))
                days.update(range(0, b + 1))
        elif part in DAY_TOKENS:
            days.add(DAY_TOKENS[part])
        else:
            return None
    return sorted(days) or None


def parse_time(text):
    """"16:00" nebo "16" → minuty od půlnoci. None při chybě."""
    hh, _, mm = text.partition(":")
    # isdigit() nestačí — pustí i unicode číslice (²), na kterých int() padá
    digits = "0123456789"
    if not hh or any(c not in digits for c in hh + mm):
        return None
    hours, minutes = int(hh), int(mm or 0)
    if hours > 24 or minutes > 59 or (hours == 24 and minutes > 0):
        return None
    return hours * 60 + minutes


def parse_filter(args):
    """Argumenty /filtr → {"days": [...], "from": min, "to": min}. None při chybě."""
    parts = strip_diacritics(args.lower()).split()
    if not parts or len(parts) > 2:
        return None
    days = parse_days(parts[0])
    if days is None:
        return None
    start, end = 0, 24 * 60
    if len(parts) == 2:
        a, dash, b = parts[1].partition("-")
        if not dash:
            return None
        start, end = parse_time(a), parse_time(b)
        if start is None or end is None or start >= end:
            return None
    return {"days": days, "from": start, "to": end}


def format_filter(f):
    days = ",".join(DAY_NAMES[d] for d in f["days"])
    if f["from"] == 0 and f["to"] == 24 * 60:
        return f"{days} (celý den)"
    return f"{days} {f['from'] // 60}:{f['from'] % 60:02d}–{f['to'] // 60}:{f['to'] % 60:02d}"


def slot_passes(key, filters):
    if not filters:
        return True
    dt = datetime.strptime(key, "%Y-%m-%dT%H:%M")
    minutes = dt.hour * 60 + dt.minute
    return any(
        dt.weekday() in f["days"] and f["from"] <= minutes < f["to"] for f in filters
    )


# ---------------------------------------------------------------- formátování

def czech_courts(n):
    if n == 1:
        return "volný 1 kurt"
    if 2 <= n <= 4:
        return f"volné {n} kurty"
    return f"volných {n} kurtů"


def format_day(dt):
    return f"{DAY_NAMES[dt.weekday()]} {dt.day}. {dt.month}."


def merge_intervals(keys):
    """Seřazené klíče slotů → [(start_dt, end_dt)] sloučených úseků."""
    intervals = []
    step = timedelta(minutes=SLOT_MINUTES)
    for key in sorted(keys):
        dt = datetime.strptime(key, "%Y-%m-%dT%H:%M")
        if intervals and intervals[-1][1] == dt:
            intervals[-1][1] = dt + step
        else:
            intervals.append([dt, dt + step])
    return intervals


def format_freed_message(freed):
    lines = ["🎾 Uvolnilo se místo na padelu!"]
    intervals = merge_intervals(freed)
    for start, end in intervals:
        keys = []
        t = start
        while t < end:
            keys.append(t.strftime("%Y-%m-%dT%H:%M"))
            t += timedelta(minutes=SLOT_MINUTES)
        count = min(freed[k] for k in keys)
        lines.append(
            f"• {format_day(start)} {start:%H:%M}–{end:%H:%M} ({czech_courts(count)})"
        )
    lines.append(f"\nRezervace: {RESERVATION_URL}")
    return "\n".join(lines)


def format_overview(slots, filters):
    free_keys = [k for k, v in slots.items() if v > 0 and slot_passes(k, filters)]
    if not free_keys:
        return "Žádné volné sloty v následujících 7 dnech" + (
            " (v rámci filtrů)." if filters else "."
        )
    by_day = {}
    for start, end in merge_intervals(free_keys):
        by_day.setdefault(start.date(), []).append(f"{start:%H:%M}–{end:%H:%M}")
    lines = ["🎾 Volné kurty (7 dní):"]
    for day in sorted(by_day):
        dt = datetime.combine(day, datetime.min.time())
        lines.append(f"{format_day(dt)}: {', '.join(by_day[day])}")
    if filters:
        lines.append("\n(filtrováno — /filtry pro přehled)")
    lines.append(f"Rezervace: {RESERVATION_URL}")
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, **params):
        if not self.token:
            print(f"[dry-run] {method}: {json.dumps(params, ensure_ascii=False)[:400]}")
            return []
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} selhalo: {payload}")
        return payload["result"]

    def send(self, chat_id, text):
        try:
            self.call(
                "sendMessage",
                chat_id=chat_id,
                text=text,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # neshodit celý běh kvůli jedné zprávě
            print(f"sendMessage selhalo: {exc}", file=sys.stderr)


def handle_command(text, state, slots, tg):
    """Zpracuje jeden příkaz od vlastníka chatu a odpoví."""
    chat_id = state["chat_id"]
    command, _, args = text.strip().partition(" ")
    command = command.lower().split("@")[0]

    if command == "/start":
        state["paused"] = False
        tg.send(chat_id, HELP_TEXT)
    elif command == "/help":
        tg.send(chat_id, HELP_TEXT)
    elif command == "/pauza":
        state["paused"] = True
        tg.send(chat_id, "⏸ Notifikace pozastaveny. Obnovíte příkazem /start.")
    elif command == "/filtr":
        new_filter = parse_filter(args)
        if new_filter is None:
            tg.send(
                chat_id,
                "Nerozumím. Příklady:\n/filtr po-pa 16:00-22:00\n"
                "/filtr so,ne 9:00-21:00\n/filtr vikend",
            )
        else:
            state["filters"].append(new_filter)
            active = "\n".join("• " + format_filter(f) for f in state["filters"])
            tg.send(chat_id, f"✅ Filtr přidán. Aktivní filtry:\n{active}")
    elif command == "/filtry":
        if state["filters"]:
            active = "\n".join("• " + format_filter(f) for f in state["filters"])
            tg.send(chat_id, f"Aktivní filtry (notifikace jen v těchto časech):\n{active}")
        else:
            tg.send(chat_id, "Žádné filtry — chodí notifikace na všechny časy.")
    elif command == "/smazfiltry":
        state["filters"] = []
        tg.send(chat_id, "🗑 Filtry smazány — chodí notifikace na všechny časy.")
    elif command == "/volno":
        tg.send(chat_id, format_overview(slots, state["filters"]))
    else:
        tg.send(chat_id, "Neznámý příkaz. Nápověda: /help")


def process_updates(state, slots, tg):
    if not tg.token:
        return
    updates = tg.call("getUpdates", offset=state["tg_offset"], timeout=0)
    for update in updates:
        state["tg_offset"] = max(state["tg_offset"], update["update_id"] + 1)
        message = update.get("message") or {}
        text = message.get("text")
        chat_id = (message.get("chat") or {}).get("id")
        if not text or chat_id is None:
            continue
        if state["chat_id"] is None:
            if text.strip().lower().startswith("/start"):
                state["chat_id"] = chat_id  # bot se uzamkne na první chat
            else:
                continue
        if chat_id != state["chat_id"]:
            continue  # cizí chaty ignorujeme
        try:
            handle_command(text, state, slots, tg)
        except Exception as exc:
            # pád tady by zablokoval posun offsetu a příkaz by se
            # přehrával každý běh znovu (crash-loop až 24 h)
            print(f"Zpracování příkazu {text!r} selhalo: {exc}", file=sys.stderr)
            tg.send(state["chat_id"], "⚠️ Příkaz se nepodařilo zpracovat.")


# ---------------------------------------------------------------- hlavní běh

def load_state():
    defaults = {
        "chat_id": None,
        "paused": False,
        "filters": [],
        "tg_offset": 0,
        "slots": {},
        "updated": None,
    }
    if STATE_FILE.exists():
        defaults.update(json.loads(STATE_FILE.read_text()))
    return defaults


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    )


def main():
    state = load_state()
    tg = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    now = datetime.now(timezone.utc)

    if os.environ.get("GITHUB_ACTIONS") and not tg.token:
        message = "TELEGRAM_BOT_TOKEN není nastaven (Settings → Secrets → Actions)."
        if state["chat_id"]:
            sys.exit(message)  # bot už běžel — tichý dry-run by byl výpadek
        print(f"::warning::{message}")

    events = fetch_events(now.astimezone(TZ).strftime("%Y-%m-%d"))
    slots = build_slots(events, now)
    print(f"Načteno {len(events)} termínů, {len(slots)} slotů, "
          f"{sum(1 for v in slots.values() if v > 0)} s volnou kapacitou.")

    process_updates(state, slots, tg)

    old_slots = state["slots"]
    freed = {
        key: count
        for key, count in slots.items()
        if count > 0 and old_slots.get(key) == 0 and slot_passes(key, state["filters"])
    }
    if freed and old_slots:
        message = format_freed_message(freed)
        print(message)
        if state["chat_id"] and not state["paused"]:
            tg.send(state["chat_id"], message)

    # 0-baseline slotů, které z feedu dočasně zmizely (deaktivovaný event,
    # výpadek API), zůstává — jinak by se návrat s volnou kapacitou nenahlásil
    now_key = now.astimezone(TZ).strftime("%Y-%m-%dT%H:%M")
    for key, count in old_slots.items():
        if count == 0 and key > now_key and key not in slots:
            slots[key] = 0

    state["slots"] = slots
    state["updated"] = now.astimezone(TZ).isoformat(timespec="seconds")
    save_state(state)


if __name__ == "__main__":
    main()
