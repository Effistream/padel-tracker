# Padel tracker — Reenio → Telegram (design)

Datum: 2026-07-07 · Stav: schváleno uživatelem

## Cíl

Sledovat obsazenost padelových kurtů v rezervačním systému Reenio
(https://areal-cisarska-louka.reenio.cz/cs/service/hriste-padel-48086)
a poslat Telegram notifikaci, když se dříve plně obsazený slot uvolní
(někdo zrušil rezervaci).

## Zjištěná fakta o Reenio API

- `POST https://areal-cisarska-louka.reenio.cz/cs/api/Term/List` — veřejné, bez přihlášení.
- Parametry (multipart form): `date=YYYY-MM-DD`, `viewMode=7-days`, `page=0`,
  `filter.resource[0].id=48086`, `filter.resource[0].type=3`,
  `includeColors=false`, `findNearestAvailable=false`.
- Odpověď: `data.events[]`, každý event má `start`/`end` (UTC ISO),
  `maxCapacity` (= 3 kurty), `reservationIntervalSize: 30` (minut),
  `reservations[] = {start, end, capacity, resourceId}` a `isVisible`/`isEnabled`.
- Volná kapacita 30min slotu = maxCapacity − součet kapacit rezervací,
  které slot překrývají.

## Architektura

- **Veřejné GitHub repo** (neomezené Actions minuty) pod účtem `danielslovacek-lang`.
- **GitHub Actions cron** každých 5 minut (reálný rozptyl ~5–12 min),
  `workflow_dispatch` pro ruční spuštění, `concurrency` proti souběhu.
- **`tracker.py`** — Python 3, pouze stdlib (`urllib`, `json`, `zoneinfo`).
- **`state.json`** commitovaný v repu = persistentní stav mezi běhy
  (snapshot obsazenosti, filtry, chat id, telegram offset).
- **Secrets:** `TELEGRAM_BOT_TOKEN` v GitHub Secrets. V repu nic tajného.

## Logika jednoho běhu

1. Stáhne termíny na 7 dní dopředu (jedno volání `Term/List`, `viewMode=7-days`).
2. Spočítá mapu slotů `{lokální ISO čas → počet volných kurtů}` po 30 minutách
   (jen viditelné/aktivní eventy, časová zóna Europe/Prague).
3. Zpracuje Telegram příkazy (`getUpdates` s uloženým offsetem).
4. Porovná se snapshotem z minulého běhu: slot, který měl 0 volných kurtů
   a nyní má ≥1, je „uvolněný". Sloty nové (přibylý horizont) nebo zaniklé
   se nehlásí. První běh jen uloží baseline.
5. Sousední uvolněné 30min sloty sloučí do intervalů; všechna uvolnění
   z jednoho běhu pošle v jedné zprávě. Aplikují se uživatelské filtry.
6. Uloží nový snapshot + stav do `state.json`; workflow ho commitne.

## Telegram bot

- Bot se uzamkne na první chat, který pošle `/start`; zprávy odjinud ignoruje.
- Příkazy (latence odpovědi = interval cronu, ~5–12 min):
  - `/filtr po-pa 16:00-22:00`, `/filtr so,ne 9:00-21:00`, `/filtr vikend` — přidá filtr; více filtrů = OR
  - `/filtry` — vypíše aktivní filtry
  - `/smazfiltry` — smaže všechny (chodí pak vše)
  - `/volno` — přehled aktuálně volných slotů na 7 dní (respektuje filtry)
  - `/pauza` / `/start` — pozastavení / obnovení notifikací
  - `/help` — nápověda
- Dny: `po ut st ct pa so ne` (i s diakritikou), rozsah `po-pa`, výčet `so,ne`,
  aliasy `vikend`, `vsedni`. Bez času = celý den.

## Chybové stavy

- Selhání API / neplatná odpověď → skript skončí nenulově, GitHub pošle
  e-mail o selhání workflow; další běh pokračuje z posledního stavu.
- Souběh běhů řeší `concurrency` skupina; push stavu s `pull --rebase -X theirs`
  a retry smyčkou (race s ručním pushem do main).
- Chyba při zpracování jednoho Telegram příkazu se odchytí (jinak by
  neposunutý offset přehrával tentýž příkaz každý běh — crash-loop).
- Sloty se počítají jen do uzávěrky rezervací (`reservationDueDate`,
  30–60 min před začátkem) — nenotifikujeme nezarezervovatelné sloty.
- 0-baseline slotů dočasně zmizelých z feedu (deaktivace, výpadek) se drží,
  aby se návrat s volnou kapacitou nahlásil.
- Chybějící `TELEGRAM_BOT_TOKEN` v CI: warning dokud se bot nepřipojil,
  poté tvrdé selhání (tichý dry-run by byl neviditelný výpadek).

## Akceptované trade-offy

- Latence reakcí bota = interval cronu (~5–12 min).
- Doručení at-least-once: selže-li uložení stavu po odeslání zpráv, další
  běh může zprávu/odpověď poslat podruhé. Duplicita je přijatelnější než
  ztráta notifikace.
- Veřejné repo: ve `state.json` je vidět chat_id a filtry (bez bot tokenu
  nezneužitelné); token je jen v GitHub Secrets.
- Commit historie roste (~1 commit na běh se změnou obsazenosti).

## Implementační plán

1. Scaffold: `tracker.py`, `.github/workflows/tracker.yml`, `README.md`, `.gitignore`.
2. Lokální test proti živému API (dry-run bez Telegramu) — ověřit výpočet slotů.
3. Vytvořit veřejné repo `gh repo create`, push.
4. Review kód (nezávislá verifikace logiky slotů a příkazů).
5. Od uživatele: token od @BotFather → `gh secret set TELEGRAM_BOT_TOKEN`,
   uživatel pošle botovi `/start`, end-to-end test.
