# Jak číst výstupní soubory

## Nejdůležitější věc dopředu

**`listings.csv` neobsahuje cenu.** Vůbec, ani sloupec. Je to schválně:
cena je jediná věc, která se mění často, a kdyby byla v `listings.csv`,
musel by se každou hodinu přepsat každý aktivní řádek. To zabije gitu delta
kompresi a repozitář by rostl zhruba o celý soubor každou hodinu.

Cena tedy žije jinde:

| chci | kouknu do |
|---|---|
| aktuální cenu | `data/csv/aktivni_inzeraty.csv` (už spojené) |
| historii ceny | `data/observations/<YYYY-MM>.csv` |
| vlastnosti nemovitosti | `data/listings.csv` |

## Jak se zapisuje změna ceny

Při každém běhu se u každého nalezeného inzerátu porovná `(cena, stav)`
proti poslední známé hodnotě v `data/state/last_observation.json`:

```python
changed = prev is None or prev["price"] != price or prev["status"] != status
if not changed:
    return                      # nic se nezapíše
```

**Nový řádek v `observations/` vznikne jen tehdy, když se cena nebo stav
skutečně liší.** Hodina, kdy se nezměnilo nic, nezapíše nic — proto má
dataset po ~12 hodinách provozu 3 232 pozorování a ne 3 000 × 12.

Skutečný příklad z dat:

```
internal_id      d5d6bcf04d22dfa0
adresa           Záběhlická, Záběhlice, Praha | 2+kk | 56 m²

2026-09-15T19:01:33+00:00   7 490 000 Kč   133 750 Kč/m²   active
2026-09-16T04:55:10+00:00   7 350 000 Kč   133 750 Kč/m²   active
                            ^^^^^^^^^^^^ zlevněno o 140 000
```

Mezi těmi dvěma řádky proběhlo devět hodinových běhů. Žádný z nich nic
nezapsal, protože se nic neměnilo.

### Zmizení se zapisuje stejným kanálem

Stav prochází `active → missing_1 → missing_2 → removed`. Inzerát se
označí za zmizelý až po **třech** po sobě jdoucích úspěšných bězích, ve
kterých ho zdroj neviděl — jeden výpadek portálu tak nesmaže půl datasetu.
Řádek se nikdy nemaže, jen mění stav.

U těchto řádků je `price` **prázdná**, protože se žádná cena nepozorovala.
Poslední známou cenu tedy neber z posledního řádku, ale z posledního řádku
**s neprázdnou cenou**.

## Co je který soubor

### `data/listings.csv` — kdo je kdo
Jeden řádek na každý kdy viděný inzerát, klíč `internal_id`. Pomalu se
měnící vlastnosti: adresa, dispozice, plocha, patro, GPS, URL, zdroj,
`first_seen_at`, `last_seen_at`, `status`, `cluster_id`,
`dedup_confidence`, `priority_zone`.

`last_seen_at` je schválně **jen datum**, ne čas — ze stejného důvodu, proč
tu není cena.

### `data/observations/<YYYY-MM>.csv` — co se kdy stalo
Jen přírůstky, nikdy se nepřepisuje. Sloupce: `internal_id`, `observed_at`,
`price`, `price_per_m2`, `status`. Tohle je zdroj pravdy pro jakoukoli
časovou řadu.

### `data/state/last_observation.json` — pracovní stav
Poslední `(cena, stav)` u každého inzerátu. **Není to datová vrstva**, je
to jen to, proti čemu se porovnává, aby se nemusel číst celý observations
soubor. Klidně smaž — příští běh ho obnoví (za cenu jedné vlny zbytečných
pozorování).

### `data/csv/` — odvozené pohledy, ať nemusíš joinovat
Přegenerovatelné z předchozích dvou (`python -m tools.export_csv`):

- `aktivni_inzeraty.csv` — co je právě na trhu, **s aktuální cenou**
- `priority_zona.csv` — totéž, jen Spořilov + Hostivař
- `vse_vcetne_zmizelych.csv` — každý kdy viděný inzerát
- `souhrn.csv` — počty podle kategorií

**Pro běžné otázky („co je na trhu a za kolik") použij tohle.** Join je
už udělaný.

### `data/raw/` — auditní stopa
Syrové odpovědi portálů, gzip. K rekonstrukci datasetu není potřeba;
existuje, aby šlo dohledat, co portál skutečně poslal, když parser udělá
chybu. Není v týdenních zálohách (viz `tools/backup.py`).

### `logs/<YYYY-MM-DD>.jsonl` — co dělal který běh
Jeden JSON objekt na běh: počty na zdroj, chyby, spotřebovaný rozpočet.

## Na co si dát pozor

**`price_per_m2` může chybět.** Když index portálu pošle cenu bez plochy,
nedá se spočítat. V živých datech to bylo 17 z 3 054 pozorování (0,6 %),
skoro všechno iDNES, které plochu často neuvádí vůbec. Dva z nich šly
dopočítat z plochy uložené v `listings.csv` a od
commitu, který přidal `docs/data.md`, se dopočítávají. **Při analýze si ho raději
počítej sám** z `price` a `listings.area_m2` — vždycky vyjde konzistentně.

**`cluster_id` spojuje duplicity, nemaže je.** Stejný byt od tří realitek
zůstane třemi řádky se společným `cluster_id`. Když počítáš, kolik je na
trhu bytů, seskup podle `cluster_id` — jinak napočítáš trojnásobek. Dívej
se přitom na `dedup_confidence`; projekt raději nechá odkaz navíc, než by
sloučil dva různé byty.

**Ceny jsou celá čísla v Kč**, prázdná hodnota znamená „nepozorováno", ne
nula.
