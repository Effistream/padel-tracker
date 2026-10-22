# Padel tracker 🎾

Hlídá volné kurty na padel ve dvou areálech:

- **Císařská louka** — [rezervační systém Reenio](https://areal-cisarska-louka.reenio.cz/cs/service/hriste-padel-48086)
- **Padel Powers Smíchov** — [PadelOS](https://player.padelos.co/company/217/bookings?locale=cs)

Když se dříve plně obsazený termín uvolní — tedy když někdo zruší
rezervaci — přijde Telegram zpráva. Oba areály jedou v jednom kanálu:
co se uvolní v jednom běhu, přijde v jedné zprávě rozdělené po areálech.

Běží zdarma na GitHub Actions každých ~5–12 minut (cron s rozptylem
GitHub scheduleru). Stav mezi běhy se ukládá do `state.json` v tomto repu.

Protože GitHub u nových repozitářů spouští cron se zpožděním (i hodiny),
existuje záložní [bridge.yml](.github/workflows/bridge.yml) — samořetězící
workflow, které kontroluje kurty po 5 minutách a samo se ukončí, jakmile
nativní cron začne běhat. Zastavení: zrušit jeho aktuální run v Actions.

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
otevře Mini App (hostovanou na GitHub Pages z `docs/` přes
[pages.yml](.github/workflows/pages.yml) — přestaví se jen při změně
`docs/`, ne při každém commitu stavu) s výběrem dnů
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

Každý běh stáhne stav obou areálů na 7 dní dopředu a převede ho na
společný tvar: `{30minutový slot: počet rezervovatelných kurtů}`.
Slot, který měl 0 a nyní má ≥1, se nahlásí. Sousední sloty se slučují
do jednoho intervalu a interval se roztáhne na celé navazující volno,
aby zpráva ukazovala hratelné okno, ne jen tu půlhodinu, která se
změnila. Nově otevřené termíny na konci horizontu se nehlásí.

**Rezervovatelný** znamená, že na slot navazuje celých 60 nebo 90 minut
volna. Kratší mezeru si pronajmout nelze, takže se nehlásí; 120 minut
se nesleduje zvlášť, protože nepokrývá nic navíc oproti 60 a 90.

**Císařská louka** (`POST /cs/api/Term/List`, veřejné API bez přihlášení):
volno = kapacita (3 kurty) − překrývající se rezervace. Den je rozdělený
na cenové termíny (10–12, 12–16, 16–21) a rezervace přes jejich hranici
nejde, takže se 60/90minutové okno hledá vždy uvnitř jednoho termínu.

**Padel Powers Smíchov** (`POST api.padelos.co/customers/searchByDate`,
také bez přihlášení, hlavičky `x-clubos-company: 217` a
`x-clubos-club-info: 216927`): API nevrací rezervace, ale rovnou volné
kurty pro každou délku, a umí jen jeden den na volání — horizont tedy
stojí 7 requestů. Pokrytí 60 a 90 minut se sjednocuje: ve večerní špičce
klub kratší než 90minutovou rezervaci vůbec nenabízí, takže samotný
šedesátiminutový seznam by ty termíny zahodil. Otevírací dobu API
nevrací, bere se napevno 6:00–24:00 (`SMICHOV_HOURS`) — mimo ni by
nebyly nuly, od kterých se uvolnění pozná.

Výpadek jednoho areálu neshodí druhý: jeho snapshot zůstane nedotčený,
aby se po návratu uvolnění pořád poznalo.

## Lokální spuštění (dry-run)

```
python3 tracker.py
```

Bez `TELEGRAM_BOT_TOKEN` v prostředí skript nic neposílá, jen vypisuje.
Pozor: i dry-run přepíše `state.json`.

## Konfigurace

Konstanty na začátku [tracker.py](tracker.py): `HORIZON_DAYS`,
`SLOT_MINUTES`, `DURATIONS` (sledované délky rezervace), časová zóna
a pak blok na areál — `CISARKA_*` (`CISARKA_RESOURCE_ID` 48086 = Hřiště Padel)
a `PADELOS_*` / `SMICHOV_*`. Areály jsou v seznamu `VENUES`; přidat
další znamená dopsat funkci, která vrátí mapu slotů, a jednu položku
do seznamu.
Návrhová specifikace: [docs/superpowers/specs/2026-07-07-padel-tracker-design.md](docs/superpowers/specs/2026-07-07-padel-tracker-design.md).
