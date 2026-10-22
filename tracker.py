#!/usr/bin/env python3
"""Padel tracker — hlídá volné kurty a posílá Telegram notifikace.

Sleduje dva areály: Císařskou louku (rezervační systém Reenio) a
Padel Powers Smíchov (PadelOS). Běží periodicky (GitHub Actions cron).
Každý běh:
  1. stáhne volné kurty na HORIZON_DAYS dopředu z obou areálů,
  2. spočítá, kolik kurtů jde na kterém 30min slotu reálně rezervovat,
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

HORIZON_DAYS = 7
SLOT_MINUTES = 30
# Kratší než hodinu kurt nikde nepronajmou, takže 30min okénko mezi dvěma
# rezervacemi není volno. 120 min se nesleduje zvlášť — nepokrývá nic, co
# by nebylo v 60 nebo 90.
DURATIONS = (60, 90)
TZ = ZoneInfo("Europe/Prague")
KEY_FORMAT = "%Y-%m-%dT%H:%M"
USER_AGENT = "padel-tracker (osobni notifikace volnych kurtu)"
STATE_FILE = Path(__file__).parent / "state.json"

CISARKA_URL = "https://areal-cisarska-louka.reenio.cz"
CISARKA_RESERVATION_URL = f"{CISARKA_URL}/cs/service/hriste-padel-48086"
CISARKA_RESOURCE_ID = 48086
CISARKA_RESOURCE_TYPE = 3

PADELOS_URL = "https://api.padelos.co"
PADELOS_COMPANY = "217"  # Padel Powers
PADELOS_VERSION = "2.4"
SMICHOV_CLUB = "216927"
SMICHOV_RESERVATION_URL = "https://player.padelos.co/company/217/bookings?locale=cs"
# PadelOS otevírací dobu nevrací, jen volné termíny; 6–24 odpovídá tomu,
# co klub nabízí (nejdřívější start 6:00, nejpozdější konec 24:00)
SMICHOV_HOURS = (6, 24)

WEBAPP_URL = "https://effistream.github.io/padel-tracker/"
FILTER_KEYBOARD = {
    "keyboard": [[{"text": "⚙️ Filtry", "web_app": {"url": WEBAPP_URL}}]],
    "resize_keyboard": True,
    "is_persistent": True,
}
BOT_COMMANDS = [
    {"command": "volno", "description": "Přehled volných slotů na 7 dní"},
    {"command": "filtry", "description": "Vypsat aktivní filtry"},
    {"command": "filtr", "description": "Přidat filtr, např. /filtr po-pa 16:00-22:00"},
    {"command": "smazfiltry", "description": "Smazat všechny filtry"},
    {"command": "pauza", "description": "Pozastavit notifikace"},
    {"command": "start", "description": "Obnovit notifikace / nápověda"},
    {"command": "help", "description": "Nápověda"},
]

DAY_NAMES = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"]
DAY_TOKENS = {"po": 0, "ut": 1, "st": 2, "ct": 3, "pa": 4, "so": 5, "ne": 6}
DAY_ALIASES = {"vikend": [5, 6], "vsedni": [0, 1, 2, 3, 4]}

HELP_TEXT = (
    "🎾 Padel tracker — hlídám uvolněné kurty na Císařské louce "
    "a v Padel Powers Smíchov.\n\n"
    "Filtry nejpohodlněji nastavíte tlačítkem ⚙️ Filtry dole u pole zprávy.\n\n"
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


def parse_utc(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# ------------------------------------------------- Císařská louka (Reenio)

def fetch_cisarka_events(date_str):
    """Stáhne termíny od date_str na 7 dní dopředu."""
    params = urllib.parse.urlencode({
        "date": date_str,
        "viewMode": "7-days",
        "page": "0",
        "filter.resource[0].id": str(CISARKA_RESOURCE_ID),
        "filter.resource[0].type": str(CISARKA_RESOURCE_TYPE),
        "includeColors": "false",
        "findNearestAvailable": "false",
    })
    req = urllib.request.Request(
        f"{CISARKA_URL}/cs/api/Term/List",
        data=params.encode(),
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("status") != "OK":
        raise RuntimeError(f"Reenio API vrátilo status {payload.get('status')!r}")
    return payload["data"]["events"]


def parse_duration(text):
    """Reenio doba "1:00:00" / "2400:00:00" (H:MM:SS) → timedelta."""
    try:
        hours, minutes, seconds = (int(p) for p in text.split(":"))
        return timedelta(hours=hours, minutes=minutes, seconds=seconds)
    except (ValueError, AttributeError):
        return timedelta(0)


def build_cisarka_slots(events, now):
    """Mapa {"YYYY-MM-DDTHH:MM": počet rezervovatelných kurtů} po 30 min.

    Volno se počítá jako kapacita − překrývající se rezervace, ale do mapy
    jde jen tam, kde na slot navazuje celých 60 nebo 90 minut volna uvnitř
    jednoho termínu (přes hranici dvou termínů rezervace nejde). Jinde
    zůstane nula — a právě přechod z nuly se hlásí jako uvolnění.
    Klíče jsou v čase Europe/Prague.
    """
    slots = {}
    step = timedelta(minutes=SLOT_MINUTES)
    for event in events:
        if not event.get("isVisible", True) or not event.get("isEnabled", True):
            continue
        capacity = event.get("maxCapacity") or 0
        if capacity <= 0:
            continue
        start = parse_utc(event["start"])
        end = parse_utc(event["end"])
        # Reenio uzavírá rezervace reservationDueDate před začátkem slotu
        cutoff = now + parse_duration(event.get("reservationDueDate") or "")
        reservations = [
            (parse_utc(r["start"]), parse_utc(r["end"]), r.get("capacity", 1))
            for r in event.get("reservations") or []
        ]
        free = {}
        slot = start
        while slot < end:
            slot_end = slot + step
            occupied = sum(c for s, e, c in reservations if s < slot_end and e > slot)
            free[slot] = max(0, capacity - occupied)
            slot = slot_end

        bookable = set()
        for begin in free:
            if begin <= cutoff:
                continue
            for minutes in DURATIONS:
                window = [begin + i * step for i in range(minutes // SLOT_MINUTES)]
                # poslední půlhodina v mapě = rezervace se vejde do termínu
                if window[-1] in free and all(free[t] > 0 for t in window):
                    bookable.update(window)

        for slot, count in free.items():
            if slot <= cutoff:
                continue
            key = slot.astimezone(TZ).strftime(KEY_FORMAT)
            slots[key] = slots.get(key, 0) + (count if slot in bookable else 0)
    return slots


def cisarka_slots(now):
    events = fetch_cisarka_events(now.astimezone(TZ).strftime("%Y-%m-%d"))
    return build_cisarka_slots(events, now)


# --------------------------------------------- Padel Powers Smíchov (PadelOS)

def fetch_smichov_day(date_str):
    """Volné kurty na Smíchově pro jeden den — PadelOS umí jen po dnech."""
    body = json.dumps({
        "date": date_str,
        "sport": "padel",
        "courtType": "",
        "courtSize": "",
        "courtTurf": "",
        "courtFeature": "",
        "searchTerm": "",
        "limit": "",
        "offset": "",
        "type": "",
    }).encode()
    req = urllib.request.Request(
        f"{PADELOS_URL}/customers/searchByDate",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept-Language": "cs",
            "User-Agent": USER_AGENT,
            "version": PADELOS_VERSION,
            "x-clubos-channel": "CLUBOS-WEB",
            "x-clubos-domain": "PADELOSCO",
            "x-clubos-company": PADELOS_COMPANY,
            "x-clubos-club-info": SMICHOV_CLUB,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if not payload.get("success"):
        raise RuntimeError("PadelOS API nevrátilo success")
    for club in payload.get("data") or []:
        if club.get("id") == SMICHOV_CLUB:
            return club
    raise RuntimeError(f"PadelOS API nevrátilo klub {SMICHOV_CLUB}")


def smichov_grid(today):
    """Všechny 30min sloty v otevírací době na celém horizontu.

    Potřeba kvůli nulám: PadelOS vrací jen to, co je volné, ale uvolnění
    se pozná jako přechod 0 → volno. Slot, o kterém nic nevíme, by se
    nenahlásil nikdy.
    """
    step = timedelta(minutes=SLOT_MINUTES)
    opening, closing = SMICHOV_HOURS
    for offset in range(HORIZON_DAYS):
        midnight = datetime.combine(today + timedelta(days=offset), datetime.min.time())
        slot = midnight + timedelta(hours=opening)
        close = midnight + timedelta(hours=closing)
        while slot < close:
            yield slot.strftime(KEY_FORMAT)
            slot += step


def smichov_slots(now):
    """Mapa slotů pro Smíchov — stejná sémantika jako u Císařské louky.

    PadelOS nevrací rezervace, ale rovnou volné kurty pro každou délku.
    Pokrytí 60 a 90 minut se sjednocuje: ve večerní špičce klub kratší
    než 90min rezervaci vůbec nenabízí, takže šedesátiminutový seznam
    sám o sobě by ty termíny zahodil.
    """
    step = timedelta(minutes=SLOT_MINUTES)
    today = now.astimezone(TZ).date()
    courts = {}
    for offset in range(HORIZON_DAYS):
        club = fetch_smichov_day((today + timedelta(days=offset)).isoformat())
        for block in club.get("availability") or []:
            minutes = int(block.get("duration") or 0)
            if minutes not in DURATIONS:
                continue
            for slot in block.get("slots") or []:
                begin = datetime.strptime(
                    f"{slot['date']} {slot['startTime']}", "%Y-%m-%d %H:%M"
                )
                ids = {court["id"] for court in slot.get("courts") or []}
                for i in range(minutes // SLOT_MINUTES):
                    key = (begin + i * step).strftime(KEY_FORMAT)
                    courts.setdefault(key, set()).update(ids)

    slots = {key: 0 for key in smichov_grid(today)}
    for key, ids in courts.items():
        slots[key] = len(ids)
    now_key = now.astimezone(TZ).strftime(KEY_FORMAT)
    return {key: count for key, count in slots.items() if key > now_key}


VENUES = [
    {
        "key": "cisarka",
        "name": "Císařská louka",
        "url": CISARKA_RESERVATION_URL,
        "fetch": cisarka_slots,
    },
    {
        "key": "smichov",
        "name": "Padel Powers Smíchov",
        "url": SMICHOV_RESERVATION_URL,
        "fetch": smichov_slots,
    },
]


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
    dt = datetime.strptime(key, KEY_FORMAT)
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
        dt = datetime.strptime(key, KEY_FORMAT)
        if intervals and intervals[-1][1] == dt:
            intervals[-1][1] = dt + step
        else:
            intervals.append([dt, dt + step])
    return intervals


def expand_to_free_run(keys, slots):
    """Uvolněné sloty → celé navazující volné úseky.

    Z nuly přeskočí jen ta půlhodina, kterou někdo zrušil, ale hrát se dá
    celé okno kolem ní. Bez roztažení by zpráva hlásila "17:00–17:30",
    což je čas, na který se rezervace ani vzít nedá.
    """
    step = timedelta(minutes=SLOT_MINUTES)
    grown = set(keys)
    for key in keys:
        for direction in (-1, 1):
            slot = datetime.strptime(key, KEY_FORMAT)
            while True:
                slot += direction * step
                neighbour = slot.strftime(KEY_FORMAT)
                if slots.get(neighbour, 0) <= 0:
                    break
                grown.add(neighbour)
    return grown


def format_freed_message(freed_by_venue):
    """[(areál, uvolněné sloty, všechny sloty)] → zpráva se sekcí na areál."""
    lines = ["🎾 Uvolnilo se místo na padelu!"]
    for venue, freed, slots in freed_by_venue:
        lines.append(f"\n{venue['name']}:")
        for start, end in merge_intervals(expand_to_free_run(freed, slots)):
            keys = []
            t = start
            while t < end:
                keys.append(t.strftime(KEY_FORMAT))
                t += timedelta(minutes=SLOT_MINUTES)
            count = min(slots[k] for k in keys)
            lines.append(
                f"• {format_day(start)} {start:%H:%M}–{end:%H:%M} ({czech_courts(count)})"
            )
        lines.append(f"Rezervace: {venue['url']}")
    return "\n".join(lines)


def format_overview(slots_by_venue, filters):
    lines = ["🎾 Volné kurty (7 dní):"]
    anything = False
    for venue in VENUES:
        slots = slots_by_venue.get(venue["key"]) or {}
        free_keys = [k for k, v in slots.items() if v > 0 and slot_passes(k, filters)]
        lines.append(f"\n{venue['name']}:")
        if not free_keys:
            lines.append("— nic volného")
            continue
        anything = True
        by_day = {}
        for start, end in merge_intervals(free_keys):
            by_day.setdefault(start.date(), []).append(f"{start:%H:%M}–{end:%H:%M}")
        for day in sorted(by_day):
            dt = datetime.combine(day, datetime.min.time())
            lines.append(f"{format_day(dt)}: {', '.join(by_day[day])}")
        lines.append(f"Rezervace: {venue['url']}")
    if not anything:
        return "Žádné volné sloty v následujících 7 dnech" + (
            " (v rámci filtrů)." if filters else "."
        )
    if filters:
        lines.append("\n(filtrováno — /filtry pro přehled)")
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

    def send(self, chat_id, text, **extra):
        try:
            self.call(
                "sendMessage",
                chat_id=chat_id,
                text=text,
                disable_web_page_preview=True,
                **extra,
            )
        except Exception as exc:  # neshodit celý běh kvůli jedné zprávě
            print(f"sendMessage selhalo: {exc}", file=sys.stderr)


def handle_command(text, state, slots_by_venue, tg):
    """Zpracuje jeden příkaz od vlastníka chatu a odpoví."""
    chat_id = state["chat_id"]
    command, _, args = text.strip().partition(" ")
    command = command.lower().split("@")[0]

    if command == "/start":
        state["paused"] = False
        tg.send(chat_id, HELP_TEXT, reply_markup=FILTER_KEYBOARD)
    elif command == "/help":
        tg.send(chat_id, HELP_TEXT, reply_markup=FILTER_KEYBOARD)
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
        tg.send(chat_id, format_overview(slots_by_venue, state["filters"]))
    else:
        tg.send(chat_id, "Neznámý příkaz. Nápověda: /help")


def validate_filters(data):
    """Data z Mini App → seznam filtrů, nebo None při nevalidním vstupu."""
    filters = data.get("filters") if isinstance(data, dict) else None
    if not isinstance(filters, list) or len(filters) > 20:
        return None
    result = []
    for f in filters:
        if not isinstance(f, dict):
            return None
        days, start, end = f.get("days"), f.get("from"), f.get("to")
        if (not isinstance(days, list) or not days
                or not all(isinstance(d, int) and 0 <= d <= 6 for d in days)
                or not isinstance(start, int) or not isinstance(end, int)
                or not 0 <= start < end <= 24 * 60):
            return None
        result.append({"days": sorted(set(days)), "from": start, "to": end})
    return result


def handle_webapp_data(raw, state, tg):
    """Uložení filtrů odeslaných z Mini App (nahrazuje celý seznam)."""
    chat_id = state["chat_id"]
    try:
        new_filters = validate_filters(json.loads(raw))
    except ValueError:
        new_filters = None
    if new_filters is None:
        tg.send(chat_id, "⚠️ Neplatná data z aplikace filtrů.")
        return
    state["filters"] = new_filters
    if new_filters:
        active = "\n".join("• " + format_filter(f) for f in new_filters)
        tg.send(chat_id, f"✅ Filtry uloženy:\n{active}")
    else:
        tg.send(chat_id, "✅ Filtry smazány — chodí notifikace na všechny časy.")


def process_updates(state, slots_by_venue, tg):
    if not tg.token:
        return
    updates = tg.call("getUpdates", offset=state["tg_offset"], timeout=0)
    for update in updates:
        state["tg_offset"] = max(state["tg_offset"], update["update_id"] + 1)
        message = update.get("message") or {}
        text = message.get("text")
        web_app = message.get("web_app_data") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None or not (text or web_app):
            continue
        if state["chat_id"] is None:
            if text and text.strip().lower().startswith("/start"):
                state["chat_id"] = chat_id  # bot se uzamkne na první chat
            else:
                continue
        if chat_id != state["chat_id"]:
            continue  # cizí chaty ignorujeme
        try:
            if web_app:
                handle_webapp_data(web_app.get("data") or "", state, tg)
            else:
                handle_command(text, state, slots_by_venue, tg)
        except Exception as exc:
            # pád tady by zablokoval posun offsetu a příkaz by se
            # přehrával každý běh znovu (crash-loop až 24 h)
            print(f"Zpracování zprávy selhalo: {exc}", file=sys.stderr)
            tg.send(state["chat_id"], "⚠️ Příkaz se nepodařilo zpracovat.")


# ---------------------------------------------------------------- hlavní běh

def ensure_commands(state, tg):
    """Jednorázově zaregistruje nabídku příkazů (menu po napsání /)."""
    if state.get("commands_set") or not tg.token:
        return
    tg.call("setMyCommands", commands=BOT_COMMANDS)
    state["commands_set"] = True


def load_state():
    defaults = {
        "chat_id": None,
        "paused": False,
        "filters": [],
        "tg_offset": 0,
        "venues": {},
        "updated": None,
        "commands_set": False,
    }
    if STATE_FILE.exists():
        defaults.update(json.loads(STATE_FILE.read_text()))
    # migrace z doby jednoho areálu: ploché "slots" byly Císařská louka
    legacy = defaults.pop("slots", None)
    if legacy and not defaults["venues"]:
        defaults["venues"] = {VENUES[0]["key"]: legacy}
    for venue in VENUES:
        defaults["venues"].setdefault(venue["key"], {})
    return defaults


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    )


def keep_zero_baselines(old_slots, slots, now):
    """Nuly slotů, které z feedu dočasně zmizely (deaktivovaný termín,
    výpadek API), zůstávají — jinak by se návrat s volnou kapacitou
    nenahlásil."""
    now_key = now.astimezone(TZ).strftime(KEY_FORMAT)
    for key, count in old_slots.items():
        if count == 0 and key > now_key and key not in slots:
            slots[key] = 0
    return slots


def main():
    state = load_state()
    tg = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    now = datetime.now(timezone.utc)

    if os.environ.get("GITHUB_ACTIONS") and not tg.token:
        message = "TELEGRAM_BOT_TOKEN není nastaven (Settings → Secrets → Actions)."
        if state["chat_id"]:
            sys.exit(message)  # bot už běžel — tichý dry-run by byl výpadek
        print(f"::warning::{message}")

    ensure_commands(state, tg)

    fresh = {}
    for venue in VENUES:
        try:
            slots = venue["fetch"](now)
        except Exception as exc:
            # výpadek jednoho areálu nesmí přepsat jeho snapshot — bez
            # starých nul by se uvolnění po návratu už nikdy nenahlásilo
            print(f"::warning::{venue['name']}: načtení selhalo: {exc}", file=sys.stderr)
            continue
        fresh[venue["key"]] = slots
        print(f"{venue['name']}: {len(slots)} slotů, "
              f"{sum(1 for v in slots.values() if v > 0)} s volnou kapacitou.")

    process_updates(state, {**state["venues"], **fresh}, tg)

    freed_by_venue = []
    for venue in VENUES:
        slots = fresh.get(venue["key"])
        if slots is None:
            continue
        old_slots = state["venues"].get(venue["key"]) or {}
        freed = {
            key: count
            for key, count in slots.items()
            if count > 0 and old_slots.get(key) == 0
            and slot_passes(key, state["filters"])
        }
        if freed and old_slots:
            freed_by_venue.append((venue, freed, slots))
        state["venues"][venue["key"]] = keep_zero_baselines(old_slots, slots, now)

    if freed_by_venue:
        message = format_freed_message(freed_by_venue)
        print(message)
        if state["chat_id"] and not state["paused"]:
            tg.send(state["chat_id"], message)

    state["updated"] = now.astimezone(TZ).isoformat(timespec="seconds")
    save_state(state)


if __name__ == "__main__":
    main()
