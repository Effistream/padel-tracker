# Padel tracker 🎾

Hlídá volné kurty na padel v areálu Císařská louka
([rezervační systém Reenio](https://areal-cisarska-louka.reenio.cz/cs/service/hriste-padel-48086))
a pošle Telegram zprávu, když se dříve plně obsazený termín uvolní —
tedy když někdo zruší rezervaci.

Běží zdarma na GitHub Actions každých ~5–12 minut (cron `*/5` s rozptylem
GitHub scheduleru). Stav mezi běhy se ukládá do `state.json` v tomto repu.

## Zprovoznění

1. **Založte Telegram bota:** napište [@BotFather](https://t.me/BotFather)
   příkaz `/newbot`, zvolte jméno a username. Dostanete token
   (`1234567:AA...`).
2. **Uložte token do GitHub Secrets:**
   ```
   gh secret set TELEGRAM_BOT_TOKEN --repo <owner>/<repo>
   ```
   (nebo v repu: Settings → Secrets and variables → Actions).
3. **Napište svému botovi `/start`** v Telegramu. Při nejbližším běhu se bot
   uzamkne na váš chat a pošle nápovědu. Zprávy z jiných chatů ignoruje.

## Ovládání bota

Filtry nejpohodlněji nastavíte tlačítkem **⚙️ Filtry** u pole zprávy —
otevře Mini App (hostovanou na GitHub Pages z `docs/`) s výběrem dnů
a časů. Uložení nahradí celý seznam filtrů.

Textové příkazy:

| Příkaz | Význam |
|---|---|
| `/filtr po-pa 16:00-22:00` | přidat časový filtr notifikací |
| `/filtr so,ne` | filtr bez času = celý den |
| `/filtr vikend` | aliasy: `vikend`, `vsedni` |
| `/filtry` | vypsat aktivní filtry |
| `/smazfiltry` | smazat filtry (chodí pak vše) |
| `/volno` | přehled volných slotů na 7 dní |
| `/pauza` / `/start` | pozastavit / obnovit notifikace |
| `/help` | nápověda |

Více filtrů se sčítá (OR). Bez filtrů chodí notifikace na všechny časy.
Bot odpovídá při pravidelné kontrole, tj. s latencí až ~10 minut.

## Jak funguje detekce

Každý běh stáhne termíny na 7 dní dopředu (`POST /cs/api/Term/List`,
veřejné API bez přihlášení), spočítá volnou kapacitu po 30minutových
slotech (3 kurty, volno = kapacita − překrývající se rezervace) a porovná
s minulým snapshotem. Slot, který měl 0 volných kurtů a nyní má ≥1,
se nahlásí. Sousední sloty se slučují do jednoho intervalu, všechna
uvolnění z jednoho běhu přijdou v jedné zprávě. Nově otevřené termíny
na konci horizontu se nehlásí.

## Lokální spuštění (dry-run)

```
python3 tracker.py
```

Bez `TELEGRAM_BOT_TOKEN` v prostředí skript nic neposílá, jen vypisuje.
Pozor: i dry-run přepíše `state.json`.

## Konfigurace

Konstanty na začátku [tracker.py](tracker.py): URL areálu, `RESOURCE_ID`
(48086 = Hřiště Padel), `HORIZON_DAYS`, `SLOT_MINUTES`, časová zóna.
Návrhová specifikace: [docs/superpowers/specs/2026-07-07-padel-tracker-design.md](docs/superpowers/specs/2026-07-07-padel-tracker-design.md).
