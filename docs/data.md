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

### `data/changes/<YYYY-MM>.csv` — co o sobě inzerát změnil
Jen přírůstky. Sloupce: `internal_id`, `changed_at`, `field`, `old_value`,
`new_value`. Sleduje `disposition`, `area_m2`, `floor`, `address`,
`description`, `priority_zone` — tedy všechno kromě ceny a stavu, které mají
vlastní vrstvu.

Proč to stojí za to: dřív se tyhle hodnoty prostě přepsaly, takže byt
inzerovaný jako 2+kk a později jako 3+1 nezanechal stopu, že kdy říkal něco
jiného. Oprava atributu je přitom obvykle buď re-listing převlečený za
editaci, nebo přeceňování pozice — a přepsaný popis velmi často přichází
spolu se slevou.

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

### `data/csv/historie_nemovitosti.csv` — pro analýzy

**Tohle chceš, když se ptáš na historii.** Jeden řádek na **epizodu
nabízení fyzické nemovitosti** — napříč portály i napříč re-listingy.

Vzniklo to proto, že surové vrstvy sice obsahují všechno, ale každá otázka
je několik joinů daleko a v každé jsou tytéž čtyři pasti. Na živých datech
posunulo zapomenutí jen té první průměrnou cenu o 6,3 %.

Sloupce: `property_key`, `episode`, `first_seen`, `last_seen`,
`days_on_market`, `gap_before_days`, `outcome`, `first_price`, `last_price`,
`min_price`, `max_price`, `price_changes`, `discount_czk`, `discount_pct`,
`price_per_m2_first/last`, plus vlastnosti a `sources`, `listing_count`.

Tvoje dvě otázky jsou pak jeden filtr:

```python
rows = list(csv.DictReader(open("data/csv/historie_nemovitosti.csv")))

# byty zmizelé do týdne
quick = [r for r in rows if r["outcome"] == "removed"
         and int(r["days_on_market"]) <= 7 and r["property_type"] == "byt"]

# nabízené 3+ měsíce a jak zlevňovaly
long_ = [r for r in rows if int(r["days_on_market"]) >= 90 and r["discount_pct"]]
```

**Epizoda** je souvislé období nabízení. Dva inzeráty patří do téže epizody,
pokud jde o tutéž nemovitost a mezera mezi nimi je nejvýš 14 dní; delší
mezera je nový pokus o prodej a nemá se průměrovat s prvním.
`gap_before_days` je ve výstupu, takže když s tou hranicí nesouhlasíš, dá se
přesegmentovat bez počítání čehokoli dalšího (`--gap-days`).

**Co ti to neřekne:** jestli se prodalo, nebo stáhlo. Žádný portál to
nezveřejňuje. `outcome` říká `removed`, nikdy `sold`.

### `data/csv/trh_denne.csv` a `trh_souhrn.csv` — dvanáct ukazatelů trhu

Denní časová řada, protože jedno číslo neříká nic. „Medián 9,1 M" není fakt
o trhu, je to fakt o dnešku; otázka je vždycky, jestli je to víc než minulý
měsíc a jestli to táhnou ceny, nebo složení nabídky.

| ukazatel | co říká |
|---|---|
| `nabidka` | kolik nemovitostí je ten den na trhu |
| `nove` | kolik ten den přibylo |
| `zmizele` | kolik ten den odešlo |
| `absorpce_pct` | odchody / nabídka — **nejlepší „prodává se?" číslo** |
| `cena_median` | medián požadované ceny toho, co je na trhu |
| `cena_m2_median` | totéž na m², jediné srovnatelné přes měnící se mix |
| `cena_zmizelych` | medián ceny toho, co odešlo |
| `cena_rychlych` | totéž, ale jen co odešlo do 14 dní |
| `dnu_na_trhu_median` | medián doby na trhu u toho, co odešlo |
| `zlevnilo_pct` | podíl nabídky, která už aspoň jednou zlevnila |
| `zlevneni_prumer` | průměrný počet zlevnění |
| `sleva_median_pct` | medián hloubky slevy u těch, co zlevnily |
| `upravilo_pct` | podíl, který upravil něco jiného než cenu |
| `uprav_prumer` | průměrný počet takových úprav |

**Jak je číst dohromady.** Rostoucí nabídka + klesající absorpce + rostoucí
zlevňování je trh otáčející se proti prodávajícím — a hýbe se to v tomhle
pořadí. Opačně, ve stejném pořadí, je to obrat pro ně. Medián ceny je
z těch čtyř nejpomalejší a nejvíc zamořený složením nabídky, takže
potvrzuje, nevaruje.

**Rozdíl `cena_zmizelych` vs. `cena_rychlych`** je čtení na to, co je trh
ochoten zaplatit, proti tomu, co se za něj chce. Rychlé odchody jsou
nejsilnější dostupný signál, že se něco skutečně prodalo — kdo to vzdává,
málokdy to vzdá do dvou týdnů.

Segmenty jsou `byt/prodej`, `dum/prodej`, `byt/pronajem`… plus řádek `vse`.
Prodej a pronájem se nikdy neprůměrují dohromady.

**Tenký den nehlásí nic** místo šumu: pod 5 nemovitostí se medián nepočítá.
Dva byty medián mají, ale není to měření trhu, a hlásit ho jako měření dělá
z každého klidného úterý pohyb trhu.

**Report se generuje jednou týdně** (`.github/workflows/report.yml`, pondělí
8:00 pražského času — po nedělním běhu pronájmů a po záloze, takže je to
první věc o kompletním týdnu). Řada pod ním se přepočítává **každý běh**:
záznam musí být aktuální, ale stránka, kterou čte člověk, se v trhu
obracejícím se v měsících nemá měnit každou hodinu — to jen naučí člověka ji
ignorovat.

`trh_souhrn.csv` je totéž jako poslední hodnota vedle hodnoty před 30 dny
a procentní změna — nejmenší věc, která je ještě trend, a ne odečet.

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
