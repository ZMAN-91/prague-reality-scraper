# Prague Reality Sector Analysis

Víceletý, bez zásahu běžící sběrač dat o realitním trhu v Praze a jejím
těsném okolí ze **sreality.cz**, **bezrealitky.cz** a **Reality.iDNES.cz**.
Prodej běží každou hodinu, pronájem jednou týdně v neděli ráno, obojí přes
GitHub Actions, ukládá vše do plain-text CSV/JSON a je navržený tak, aby
přežil roky provozu bez jediného lidského zásahu.

**Tady je kód. Data jsou jinde** — v privátním repozitáři, kam tenhle sběr
zapisuje přes deploy key. Důvod a mechanika jsou v `docs/rozdeleni.md`;
krátce: minuty Actions jsou zdarma podle repozitáře, kde běh probíhá, ale
veřejný repozitář s kompletním datasetem by byl šíření dat, kterému se
projekt vyhýbá.

## Stav: ověřeno proti živým portálům

Na rozdíl od prvních verzí tohohle README už nejde o dohady. Scraper běžel
na GitHub Actions proti skutečným portálům a níže uvedené věci jsou
**ověřené reálnou odpovědí**, ne odvozené ze zdrojáků třetích stran:

| co | stav |
|---|---|
| sreality `/api/v1/estates/search` | funguje (HTTP 200); hlásí ~5 500 bytů k prodeji v Praze |
| sreality `/api/cs/v2/estates` (staré) | **mrtvé, 404** |
| bezrealitky `/api/record/markers` | **mrtvé, 404** (i tento web je dnes Next.js) |
| bezrealitky GraphQL `api.bezrealitky.cz/graphql/` | funguje, schéma pročtené introspekcí |
| robots.txt obou portálů | staženo doslovně, uloženo v `docs/robots/` |

První ostrý běh nasbíral 402 inzerátů z obou zdrojů (GPS u 100 %, adresa
u 100 %, dispozice u 99 %).

## robots.txt: co říká a jak se k tomu projekt staví

Doslovné kopie jsou v `docs/robots/` (stahuje je `tools/probe_sources.py`,
takže případná budoucí změna pravidel se objeví jako diff).

**`www.bezrealitky.cz`** zakazuje jen `/vyhledat*`, `/search*`,
`/moje-bezrealitky/*` a hypoteční formuláře. Detailové stránky
`/nemovitosti-byty-domy/*` zakázané **nejsou** a soubor sám nabízí
`Sitemap: …/sitemap.xml`. Scraper proto jde přesně touto cestou, kterou web
sám nabízí — sitemap → povolené detailové stránky → strukturovaný objekt
inzerátu, který Next.js stejně vkládá do `__NEXT_DATA__`. Zakázaný host
`api.bezrealitky.cz` (`Disallow: /`) se při sběru vůbec nepoužívá.

**`www.sreality.cz`** má 979 řádků a 19 bloků. Osmnáct z nich jsou jmenovitě
vypsané vyhledávače (SeznamBot, Googlebot, …) s `Allow: /`. Blok platný pro
kohokoliv jiného — tedy i pro tenhle projekt — je celý tento:

```
User-agent: *
Disallow: /
```

Povolená automatizovaná cesta k datům sreality tedy **neexistuje**.
Zacházení s robots.txt je proto explicitní per-host politika
(`SCRAPER_ROBOTS_OVERRIDE_HOSTS` v `common/net.py`), která je **výchozím
stavem prázdná** a nastavuje se na jediném komentovaném řádku v
`.github/workflows/scrape.yml`. Smazáním toho řádku se všude vrátí striktní
dodržování.

Tam, kde ten řádek platí, se projekt drží stopy odpovídající tomu, k čemu
skutečně slouží (soukromé sledování trhu, žádné přeprodávání ani šíření):
poctivý sebeidentifikující User-Agent (žádné předstírání prohlížeče),
1 s mezi požadavky, detail každého inzerátu se stahuje **jednou za celou
dobu jeho života**, a jeden kompletní sken Prahy stojí **33 požadavků** —
protože se čte index, ne každý inzerát zvlášť.

robots.txt ale není celý obrázek. Podmínky užití obou portálů jsou taky
stažené a přečtené a **[`docs/podminky.md`](docs/podminky.md) je hodnotí
vedle sebe**: co přesně zakazují, komu jsou určeny, kde je zákonná ochrana
databáze nezávislá na smlouvě, jak velká je naše skutečná stopa proti
ručnímu klikání — a co se s tím dá dělat. Sreality zakazují scrapování
jmenovitě; bezrealitky o něm nemluví vůbec.

## Zdroje dat

### sreality.cz

Nové API (staré `/api/cs/v2/` bylo zrušeno):
`https://www.sreality.cz/api/v1/estates/search` (výpis, offset/limit) a
`/api/v1/estates/{id}` (detail). Implementace: `scrapers/sreality.py`,
jejíž docstring rozepisuje, co je ověřené a co dohad.

- Filtr: `category_main_cb` × `category_type_cb` × `locality_district_id`
  (`47` = Praha, `56`/`57` = Praha-východ/Praha-západ, tedy „těsné okolí").
- **Výpis nese většinu polí rovnou** (`locality`, `category_sub_cb`,
  `advert_name`, `usable_area`, cena). Původně jsem předpokládal opak a
  stahoval detail ke každému inzerátu — to je ~12 000 požadavků na pokrytí
  Prahy místo ~30. Detail se teď stahuje **jen u řádku, kterému opravdu
  chybí GPS/plocha/dispozice**. U zdroje, který tenhle provoz v robots.txt
  nezve, je nejmenší dostačující stopa jediná obhajitelná volba.
- Geografická brána je GPS z výpisu: okresy 56/57 sahají mnohem dál než
  „těsné okolí", takže o zařazení rozhoduje až bounding box, ne dotaz.
- URL inzerátu se skládá přesně stejným algoritmem jako na webu, včetně
  kódovníku `category_sub_cb` — vyčteno z reálného zdroje, ne odhadnuto.

### bezrealitky.cz

Sitemap → povolené detailové stránky → `__NEXT_DATA__` (viz sekce
o robots.txt výše). Názvy polí pocházejí z živého GraphQL typu `Advert`,
získaného introspekcí, takže jsou skutečné: `street`, `houseNumber`,
`city`, `cityDistrict`, `gps`, `disposition`, `etage`, `price`, `surface`.

Praktický důsledek: bezrealitky teď dává **skutečnou strukturovanou adresu
včetně ulice**, zatímco předchozí (mrtvá) cesta ji musela hádat z URL slugu.
Právě díky tomu funguje filtrování podle ulice.

Cena: jeden požadavek na inzerát. Proto se předfiltrovává už podle slugu
v sitemapě (`praha`, `jesenice`, `roztoky`, …), aby se vůbec nestahovalo
něco z Brna, a zbytek hlídá rozpočet běhu.

### Společné pro všechny zdroje

- Popisný `User-Agent` přiznávající osobní nekomerční sledování trhu.
- Zdvořilé zpoždění **1 s** mezi požadavky (bylo 1–2 s s jitterem; ten
  stál zhruba třetinu práce za hodinu, a limitem je hodina, ne kapacita
  portálů).
- Retry s exponenciálním backoffem na síťové chyby a 5xx/429; 4xx (kromě
  429) se neopakuje, protože to znamená špatný parametr, ne výpadek.
- **Rozpočet běhu** (`--max-seconds`, `--max-new-details`): běh se zastaví
  včas a **stejně zapíše všechno, co stihl**. Bez toho by první běh, který
  musí stáhnout detail ke každému neznámému inzerátu, nikdy nedoběhl do
  timeoutu jobu a projekt by se nikdy nenastartoval.

## Nástroje: jak se data čtou

Dataset je normalizovaný kvůli správnosti, ne kvůli čtení — cena je
schválně jinde než inzerát. Proto jsou tu hotové pohledy:

```bash
python -m tools.export_csv              # -> data/csv/
python -m tools.filter_street "Nurniho" # -> data/ulice/nurniho/
python -m tools.filter_street "Svehlova" --property-type byt
python -m tools.probe_sources           # diagnostika portálů (read-only)
```

`data/csv/` obsahuje `aktivni_inzeraty.csv` (aktuálně na trhu, s poslední
známou cenou), `priority_zona.csv` (jen Spořilov + Hostivař),
`vse_vcetne_zmizelych.csv` a `souhrn.csv` (počty po kategoriích).

`data/ulice/<ulice>/` obsahuje `inzeraty.csv`, `cenova_historie.csv`
(všechna pozorování těch inzerátů) a `souhrn.txt`. Když filtr nic nenajde,
souhrn **sám přizná, jak velké je pokrytí datasetu** — nulový výsledek při
3% pokrytí totiž neznamená „nic tam není", ale „ještě jsme tam nedošli".

## Jak si to pustit lokálně

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # requests + pytest

# jednotkové testy (běží offline, bez sítě)
python -m pytest -q

# jeden běh scraperu (zapíše do data/ a logs/ v repu)
python run.py

# jen jeden zdroj, např. při ladění bezrealitky
python run.py --sources bezrealitky

# testovací běh mimo repo (nešahá na skutečná data)
python run.py --data-dir /tmp/test-data --logs-dir /tmp/test-logs
```

`run.py` vrací exit kód `0`, pokud proběhly všechny zdroje bez chyby, jinak `1`.
**Plánované zastavení (vyčerpaný časový rozpočet) chybou není** — viz
`common/interruptions.py`; dokud se to rozlišovalo, posílal každý zdravý běh
e-mail o selhání a skutečný výpadek vypadal úplně stejně
(záměrně — viz sekce o GitHub Actions níže).

## Co se sbírá odkud a jak často

**Prodej a pronájem se sbírají zvlášť**, na jiném rozvrhu a v jiném rozsahu.

### Prodej — každou hodinu mimo nedělní okno (`.github/workflows/scrape.yml`)

| zdroj | rozsah | jak |
|---|---|---|
| **sreality.cz** | **jen sledovaná oblast** (Spořilov ↔ Horní Měcholupy, `common/collection_area.py`) | index API, ~35 požadavků na kompletní sken |
| **Reality.iDNES.cz** | všechny pražské **byty** | nejnovější stránky, sledovaná oblast, pak **celý index** — každou hodinu |
| **bezrealitky.cz** | **celá Praha** | sitemapa + detailové stránky, které robots.txt povoluje |

Celý index iDNES každou hodinu je změna oproti původní dvacetičtvrtině
za hodinu. Ta pochází z doby, kdy se prodej a pronájem sbíraly společně
a kompletní průjezd vypadal draze. Samotný prodej je ~156 stránek, tedy
~2,6 minuty při jednom požadavku za vteřinu, proti 22 minutám rozpočtu,
které poslední běh nevyužil. Znamená to, že se cena každého inzerátu
obnoví každou hodinu místo jednou za půl dne — a hlavně že průjezd
skutečně **doběhne**, což je jediné, co dovoluje prohlásit zmizelý
inzerát za zmizelý.

Kurzor se **v pondělí vrací na stránku jedna** (ISO týden v
`data/progress.json`). Týden, který kompletní průjezd nestihl, prostě
skončí; další začne odpředu, ne tam, kde se náhodou zastavil.

### Nájem — každou hodinu, spolu s prodejem

Samostatný týdenní průchod nájmů **už neexistuje** (`scrape-rent.yml` byl
zrušen). Existoval proto, že procházel celou Prahu a potřeboval na to čtyři
hodiny. Hodinový sken dnes prochází jen Prahu 4 a 10, což se vejde do jedné
hodiny i s nájmy — takže **nájem se sbírá každou hodinu místo jednou týdně**.

Nájem jede ve dvou průchodech, stejně jako prodej:

- **hodinový**, Praha 4 + 10 (`--scope area`)
- **noční**, celá Praha (`--scope city`)

Zmizení se pořád vyhodnocuje **jen z nočního celoměstského** průchodu — ten
jediný vidí celou populaci. Kdyby se počítalo z hodinového skenu, všechno
mimo Prahu 4 a 10 by vypadalo jako zmizelé.

S průchodem odešel i `tools/rent_due.py`: odpovídal na otázku „je už nájem za
tenhle týden posbíraný", a ta při hodinovém sběru nedává smysl.

### 1. Raw archiv — `data/raw/<source>/<YYYY-MM-DD>/<kind>-<HH>.json.gz`

Syrové odpovědi, gzipované. Nic se nefiltruje — i inzeráty mimo cílovou
oblast se sem uloží, protože geografický filtr se aplikuje až při
normalizaci. Je to jediná vrstva, ze které jde zpětně cokoliv dopočítat,
kdyby se ukázalo, že parsing něco propásl.

**Jedna vědomá odchylka od zadání:** `index-*` (výpis celého trhu) se
archivuje **jednou denně**, ne při každém běhu. Kompletní výpis je při
každém běhu celý aktivní trh, tedy desítky GB za rok v git repozitáři —
narazilo by to na limity GitHubu během měsíců a archiv by se tím sám
znemožnil. Přitom výpis nese jen `id` a cenu a každá změna ceny je beztak
bezeztrátově v `observations/`. Naproti tomu `detail-*` (payload konkrétního
inzerátu) se stahuje jednou za život inzerátu, je nenahraditelný, a proto se
archivuje **vždy**.

### 2. `data/listings.csv` — jeden řádek na unikátní inzerát

Klíč: `source` + `source_id` (odvozeně: `internal_id`, viz níže). Sloupce:

| sloupec | popis |
|---|---|
| `internal_id` | `sha1(source:source_id)[:16]` — deterministické, stabilní napříč běhy |
| `source` | `sreality` \| `bezrealitky` |
| `source_id` | ID inzerátu na zdrojovém webu |
| `url` | odkaz na inzerát |
| `property_type` | `byt` \| `dum` |
| `transaction_type` | `prodej` \| `pronajem` |
| `disposition` | např. `2+kk` (normalizováno na malá písmena) |
| `area_m2`, `floor`, `lat`, `lon`, `address`, `description` | viz zdroj |
| `priority_zone` | `True`/`False` — leží v okruhu Spořilova nebo Hostivaře |
| `first_seen_at`, `last_seen_at` | ISO-8601 UTC |
| `status` | `active` \| `missing_1` \| `missing_2` \| `removed` (viz níže) |
| `cluster_id` | sdílené ID pro pravděpodobně duplicitní inzeráty napříč zdroji/makléři |
| `relisted_from` | `internal_id` staršího `removed` inzerátu, kterého toto je pravděpodobně nový inzerát |

Přepisuje se celý při každém běhu (bezpečné/levné v očekávaném řádu desítek
tisíc řádků — viz sekce Designová rozhodnutí).

### 3. `data/observations/<YYYY-MM>.csv` — append-only log změn

Nový řádek jen když se něco skutečně změnilo (nová cena, změna statusu,
první výskyt) — ne při každém hodinovém běhu, pokud je vše stejné. Sloupce:
`internal_id, observed_at, price, price_per_m2, status`.

Rozdělené po měsících (`common.storage.observations_path_for`), ať
jednotlivé soubory nerostou donekonečna.

### Interní stav (`data/state/last_observation.json`)

Malá JSON cache "poslední zapsaná cena/status pro každý `internal_id`" —
**není** to čtvrtá veřejná datová vrstva, je to čistě bookkeeping, aby
`run.py` nemusel při každém běhu proskenovat všechny měsíční observation
soubory jen kvůli zjištění "změnilo se to?". Commituje se do repa spolu se
zbytkem `data/`, protože GitHub Actions runner je při každém běhu čerstvý —
bez perzistovaného stavu by systém nevěděl, co se změnilo od minula.

## Deduplikace a re-listing

Implementace: `common/dedup.py`. Nic se nikdy neslučuje ani nemaže — jen se
označí, ať se dá při analýze rozhodnout.

**Shlukování (`cluster_listings`)**: dva inzeráty dostanou stejné
`cluster_id`, pokud mají **stejný** `property_type`, `transaction_type` a
`disposition` (řetězcová shoda, žádná tolerance) a zároveň si odpovídají
plochou a GPS podle jedné ze tří úrovní:

| confidence | GPS vzdálenost | rozdíl plochy |
|---|---|---|
| `exact` | ≤ 25 m | ≤ max(1 m², 2 %) |
| `high` | ≤ 60 m | ≤ 6 % |
| `medium` | ≤ 200 m | ≤ 15 % |

Zdůvodnění čísel: oba weby geokódují nejlépe na úroveň domu/vchodu, takže
desítky metrů šumu mezi dvěma nezávislými inzeráty **téže** jednotky jsou
normální — ale ve velkém sídlišti může být několik metrů od sebe i několik
**různých** bytů. Proto GPS nikdy nerozhoduje samo — disposition a plocha
musí sedět vždy. Hodnoty jsou záměrně konzervativní (raději propásnout
duplicitu než spojit dvě různé jednotky), protože chybějící shluk nic
nerozbije (analyzuje se to prostě jako dvě nezávislé řady), zatímco falešný
shluk by mohl zkreslit analýzu ceny.

Shlukování běží jako plný přepočet nad celou `listings.csv` při každém
běhu (s prostorovým bucketováním, aby to nebylo O(n²) na celý dataset) —
u očekávaného objemu dat (pražské byty/domy za několik let, řádově nízké
desítky tisíc řádků, ne miliony) je to v řádu sekund. Existující
`cluster_id` se nikdy nepřegenerovává, jen doplňuje o nové členy — takže ID
jsou stabilní napříč lety provozu.

**Zmizení a `removed` stav**: inzerát se označí jako `removed` až po **3**
po sobě jdoucích *úspěšných* bězích, kdy chybí (`MAX_MISSING_STREAK` v
`common/schema.py`, zakódováno přímo v `status` jako `missing_1` →
`missing_2` → `removed`, takže není potřeba samostatný čítač). Zvoleno 3
(horní hranice zadaného rozmezí 2–3): při hodinové kadenci je to pořád jen
2–3hodinové zpoždění v odhalení skutečného smazání, ale bezpečně to přežije
jednu špatnou odpověď API nebo blokaci trvající déle než jeden běh — přesně
proti tomu má tahle ochrana chránit. Navíc: pokud běh skončí s chybou
(`errors` neprázdné), nebo pokud počet aktivních inzerátů u zdroje, který
jich dřív měl aspoň 20, klesne o více než polovinu oproti minulému běhu
(`SUSPICIOUS_DROP_*` v `run.py`), **žádné** absence se ten běh neoznačují —
ochrana přesně proti scénáři "dočasný výpadek API vypadá jako hromadné
smazání", který zadání výslovně zmiňuje.

**Re-listing (`relisted_from`)**: pro každý nový `internal_id` se prohledá
množina `removed` inzerátů (napříč oběma zdroji) se stejným podpisem
(property/transaction/disposition/GPS/plocha dle tabulky výše), které byly
odstraněny **před** vznikem nového inzerátu a zároveň v posledních
**365 dnech**. Zvoleno 12 měsíců záměrně velkoryse: cílem projektu je
víceletá analýza trhu, kde je horší propásnout skutečný re-listing (makléř
zkusí znovu po půl roce) než jednou za čas propojit dva nesouvisející
inzeráty — a špatné propojení nic neničí, jen přidá jeden odkaz navíc.
Nový inzerát se **vždy** uloží jako samostatný řádek s vlastní historií;
`relisted_from` je jen odkaz, nikdy merge.

## Geografie

Implementace: `common/geo.py`.

**Cílová oblast** (`PRAGUE_BBOX`): jednoduchý obdélník
lat 49.90–50.20, lon 14.15–14.75. Administrativní hranice Prahy je zhruba
lat 49.94–50.18, lon 14.22–14.71; obdélník ji obaluje o ~7–8 km na každou
stranu, aby zahrnul obce těsně za hranicí (Jesenice, Průhonice, Zličín,
Klecany, Roztoky, Brandýské okolí, Jirny…). Obdélník místo přesného polygonu
je vědomá volba: je triviální ověřit i doladit (čtyři čísla), a "trochu
příliš velkorysý" je tady bezpečný směr chyby — pár navíc středočeských
vesnic v datech nevadí, zatímco chyba v ručně kresleném polygonu, která by
tiše vyřadila kus skutečné Prahy, by byla mnohem horší a mnohem hůř
odhalitelná až za roky provozu.

**`priority_zone`** (Spořilov, Hostivař): kruhy kolem přibližných středů obou
čtvrtí (souřadnice z obecné geografické znalosti, ne z geodetických dat —
sandboxované vývojové prostředí nemělo možnost je ověřit proti živé mapě):

| čtvrť | střed | poloměr |
|---|---|---|
| Spořilov | 50.0270 N, 14.4780 E | 1.3 km |
| Hostivař | 50.0440 N, 14.5250 E | 1.8 km |

Poloměry zvolené tak, aby pokryly zastavěnou obytnou plochu čtvrti bez
velkého přesahu do sousedních. `priority_zone` **nijak neomezuje sběr** —
je to jen booleovský příznak pro pozdější filtrování/prioritizaci.

## Známá omezení

- **Pokrytí roste postupně.** Sběr je omezený rozpočtem běhu. Než pokrytí
  doroste k celému trhu (sreality hlásí ~12 450 inzerátů v Praze + obou
  okresech), je **nulový výsledek slabý důkaz** — proto ho
  `tools/filter_street.py` sám takhle okomentuje místo suchého „0".
- **Adresa je „nejlepší dostupná", ne katastrální.** U sreality vzniká
  z `*_seo_name` polí (bez diakritiky, slugovaná), u bezrealitky ze
  strukturovaných `street`/`houseNumber`/`city`. Filtr podle ulice proto
  porovnává bez diakritiky a kouká i do URL.
- **sreality: `locality_district_id=56`/`57` (Praha-východ/Praha-západ)**
  jsou doložené nepřímo (konzistentní číslovací schéma napříč dvěma
  nezávislými zdroji), živě neověřené. `47` = Praha ověřené je. Pokud 56/57
  neplatí, projeví se to jako chyba daného slice v logu a scraper pokračuje
  se zbytkem — a díky tomu, že se absence značí po jednotlivých kategoriích,
  to **nevypne detekci zmizení** u zbytku (dřív by vyplo).
- **bezrealitky stojí jeden požadavek na inzerát.** Je to daň za povolenou
  cestu (sitemap + detailové stránky) místo zakázaného GraphQL hostu.
  Předfiltr podle slugu to drží v rozumných mezích, ale plné pokrytí
  bezrealitky trvá víc běhů než u sreality.
- **Číslo popisné je odhad, ne údaj z inzerátu.** Žádný z portálů ho
  nezveřejňuje. `cislo_popisne` a `cislo_orientacni` se dopočítávají ze
  státního registru adres (RÚIAN, 134 627 adresních bodů pro Prahu): hledá
  se nejbližší adresní bod na téže ulici k GPS pinu inzerátu. Jak moc tomu
  věřit, říkají sloupce vedle: `cislo_zdroj` (že jde o dopočet),
  `cislo_vzdalenost_m` (jak daleko byl pin od toho bodu), `cislo_kandidatu`
  (kolik různých domů bylo zhruba stejně blízko) a `cislo_typ` (`č.p.` nebo
  `č.ev.` — 2,81 % pražských adresních bodů je číslo **evidenční**, což je
  jiná řada: evidenční 163 není dům 163). Řádek se 4 m a jedním
  kandidátem je budova, kterou portál napinoval přesně; řádek s 90 m a šesti
  je název ulice a pokrčení rameny. Filtrujte podle těch dvou čísel —
  **samotné číslo popisné nečtěte jako fakt.**
- **`priority_zone` je kruh, ne katastrální hranice.** Souřadnice středů
  Spořilova/Hostivaře jsou přibližné, ne geodeticky ověřené.
- **`relisted_from` a `cluster_id` jsou heuristiky, ne pravda.** Nic se
  neslučuje ani nemaže, takže špatný odhad nanejvýš přidá odkaz navíc —
  ale při analýze je dobré se na `dedup_confidence` dívat.
- **Popis u bezrealitky může být v `title`, ne `description`.** Parser bere
  první neprázdný; u některých inzerátů tak popis bude kratší.

## Automatizace (GitHub Actions)

`.github/workflows/scrape.yml` — cron `0 * * * 1-6` a `0 5-23 * * 0`
(každou hodinu kromě nedělního okna pronájmů), plus `workflow_dispatch`
pro ruční spuštění s vlastním rozpočtem. Kroky:

1. Checkout, instalace závislostí, **spuštění testů** (scraper, který
   zapisuje do sdíleného datasetu, nemá běžet, když je rozbitý).
2. `python run.py` (s `continue-on-error`, viz níže).
3. Commit + push všeho, co `run.py` stihl zapsat do `data/`/`logs/` —
   **vždy**, i když `run.py` skončil chybou. Částečný běh nikdy nezahodí
   data, která se podařilo získat.
4. Teprve pak: pokud `run.py` skončil s nenulovým exit kódem, job se
   záměrně shodí (`exit 1`). GitHub sám pošle e-mail o selhání běhu — to je
   zvolený notifikační mechanismus (žádný vlastní Telegram/email zatím).
5. `concurrency: group: scrape-data` (sdílená s `scrape-night.yml`)
   zajišťuje, že se nikdy nepřekryjí dva běhy — ani dva prodejní, ani
   prodej s pronájmem. Oba zapisují do stejného `listings.csv`.
   `cancel-in-progress: false`, protože rozdělaný běh má data, která
   ještě nejsou v gitu.

**Pozor na minuty.** Repozitář je *privátní* (přepnuto na žádost, protože
`data/` se commituje a jde o soukromý sběr). Privátní repo ale na Actions
minuty zdarma nárok nemá tak jako veřejné — Free plán dává 2 000 minut
měsíčně, Pro 3 000. Hodinový prodejní běh stojí ~20 minut (poslední reálný
běh: 1 078 s sběru + checkout, instalace a testy), takže ~163 běhů týdně
je řádově 14 000 minut měsíčně, plus ~250 minut za nedělní pronájmy.
To se do free kvóty nevejde. Možnosti, až to bude aktuální: vrátit repo
na veřejné (a data buď přijmout jako veřejná, nebo je nekomitovat),
zaplatit overage, nebo prodej pustit jednou za dvě až tři hodiny místo
každou hodinu. Rozvrh je jediný řádek cronu v `scrape.yml`.

## Zálohy: každý týden jeden ověřený archiv

`.github/workflows/backup.yml` — cron `30 5 * * 0`, tedy **neděle 7:30
pražského času**. Záměrně až po nedělním běhu pronájmů (ten startuje v 0:00
UTC, má čtyřhodinový rozpočet a timeout 4 h 40 min), aby týdenní archiv
obsahoval i čerstvá data o pronájmech. Hodinový prodejní běh v tu dobu
klidně může běžet: záloha **jen čte**, a to z checkoutu na jednom commitu,
takže není o co se prát. Proto taky nesdílí `concurrency` skupinu se
scrapery — kdyby ji sdílela, dlouhý běh pronájmů by ji mohl vytlačit
z okna úplně.

**Proč vůbec.** Data se pushují do GitHubu každou hodinu, takže proti
pokaženému běhu chrání už git — dá se vrátit k libovolné hodině. Týdenní
archiv řeší to, co git neřeší: ztrátu samotného repozitáře (smazání,
ztráta přístupu, problém s účtem) a případ, kdy chcete dataset jako jeden
otevřitelný soubor, ne jako klon plus checkout.

**Co v archivu je:** `data/listings.csv`, `data/observations/`,
`data/state/`, `data/progress.json`, `data/csv/` (odvozené pohledy, aby byl
restore hned použitelný) a `logs/`.

**Co v něm není: `data/raw/`** — a to je jediné rozhodnutí v celém souboru,
které stojí za vysvětlení. Je to archiv skutečných HTTP odpovědí portálů
a tvoří ~80 % objemu (13 MB po čtyřech bězích, přirůstá megabajt až dva
denně a nikdy se nemaže). Kopírovat ho do nového archivu každý týden
znamená nahrát tytéž neměnné gzip soubory 52× ročně a přesáhnout gigabajt
do roka. Přitom je append-only, takže git historie už drží každou verzi
každého z těch souborů, a k rekonstrukci datasetu není potřeba —
`listings.csv` a `observations/` jsou zpracovaný výsledek, ne jeho cache.
Raw je auditní stopa: stojí za to ji držet v repozitáři, nestojí za 52
kopií. Manifest to říká výslovně, aby restore nevypadal jako ztráta dat.

**Ověření.** SHA-256 každého souboru jde do manifestu a `--verify` archiv
skutečně **rozbalí** do dočasného adresáře, přepočítá všechny otisky a pak
přečte `listings.csv` projektovým vlastním readerem a porovná počet řádků
s tím, co manifest tvrdí. Workflow to pouští proti každému archivu, který
postaví, *před* publikací. Archiv, který nikdo nikdy neobnovil, není záloha,
je to soubor.

**Kam.** Jeden GitHub **release** týdně, tag `backup-YYYY-Www`, s archivem
a manifestem jako assety. Ne Actions artifact — ten expiruje nejpozději po
90 dnech, což z něj dělá přenosový mechanismus, ne zálohu. Release assety
vydrží, dokud je někdo nesmaže, a na rozdíl od commitnutého archivu
nezvětšují klon. Nic se automaticky nemaže; při ~1,4 MB na archiv je to
~75 MB ročně.

Ručně: `python -m tools.backup --out backup` (postaví i ověří),
`python -m tools.backup --verify backup/prague-reality-2026-W38.tar.gz`.

**Čím to není chráněné:** release leží na stejném GitHub účtu jako
repozitář. Proti ztrátě účtu to nepomůže. Skutečně mimo-platformní záloha
(S3, Backblaze, disk doma) potřebuje přihlašovací údaje, které projekt
nemá — až je budeš chtít přidat, je to jeden krok navíc v tomhle workflow.

## Proč žádný pandas / SQLite (zatím)

Záměrně minimální závislosti (`requests` jediná runtime závislost) — systém
má běžet bez zásahu roky, a čím méně závislostí, tím méně věcí se může
rozbít při update Pythonu/knihoven na GitHub Actions runnerech. Stdlib `csv`
a `json` na tenhle objem dat bohatě stačí. Normalizované CSV (ne jeden
plochý soubor) je zvolené právě proto, aby šlo později bez přepisování
historie postavit SQLite/dashboard, jak zadání požaduje — `data/listings.csv`
+ `data/observations/*.csv` se dají naimportovat do SQLite jedním
`csv-to-sql` skriptem, až přijde čas.

## Struktura kódu

```
common/schema.py         # datový model, normalizace, stavový automat status
common/geo.py            # bounding box + priority_zone
common/collection_area.py # co se ze sreality vůbec sbírá (kruhy + pás)
common/dedup.py          # shlukování duplicit + detekce re-listingu
common/storage.py        # raw archiv, CSV I/O (atomické zápisy), run log
common/net.py            # HTTP session, retry/backoff, deadline běhu
common/robots.py         # robots.txt podle RFC 9309 (stdlib ignoruje wildcardy)
common/budget.py         # rozpočet běhu (čas + počet detailů)
common/progress.py       # kurzor: nedokončený průjezd navazuje, nerestartuje
common/interruptions.py  # plánované zastavení vs. skutečná chyba
scrapers/sreality.py     # sreality.cz — v1 API, jen sledovaná oblast
scrapers/idnes.py        # Reality.iDNES.cz — karty ve výsledcích + og:description
scrapers/bezrealitky.py  # bezrealitky.cz — sitemap + povolené detailové stránky
run.py                   # orchestrace jednoho běhu
tools/export_csv.py      # čitelné pohledy -> data/csv/
tools/filter_street.py   # filtr podle ulice -> data/ulice/<ulice>/
tools/probe_sources.py   # obnova pravidel, podmínek a souřadnic (read-only)
tools/listing_query.py   # sdílené čtení/joinování datasetu
tools/backup.py          # týdenní archiv datasetu + ověření obnovou
tools/episodes.py        # historie po epizodách nabízení -> data/csv/
tools/market.py          # deset ukazatelů trhu, denní řada -> data/csv/
docs/podminky.md         # hodnocení: jak moc projekt jde proti podmínkám portálů
docs/robots/             # doslovné robots.txt + smluvní podmínky všech portálů
docs/geo/                # rozřešené referenční souřadnice, i se zdrojem
docs/sources/            # skutečný markup, proti kterému jsou psané parsery
.github/workflows/scrape.yml   # sběr prodeje, každou hodinu
.github/workflows/backup.yml   # týdenní ověřený archiv datasetu
.github/workflows/probe.yml    # obnova docs/, jen ručně
tests/                   # 324 testů, běží plně offline
```

## Testy

`python -m pytest -q` — **452 testů, plně offline** (žádná síť). Kromě
jednotlivých funkcí testují i to, co se nejhůř hledá:

- **Celý běh od začátku do konce** (`tests/test_run_end_to_end.py`) proti
  falešné síti a skutečnému dočasnému `data/` adresáři: že se zapíšou
  všechny tři vrstvy, že druhý nezměněný běh **nezmění listings.csv ani
  o bajt**, že výpadek zdroje nesmí označit nic za zmizelé, a že re-listing
  propojí nový inzerát se starým odstraněným.
- **Round-trip přes skutečné CSV**, ne jen dict v paměti — právě tím se
  odhalilo, že se `dedup_confidence` tiše zahazoval.
- **Rozpočty**: že limit detailů inzeráty neztratí, jen odloží.
- **Míchání naivních a aware datetime** — `last_seen_at` je datum,
  `first_seen_at` timestamp, a jejich porovnání by jinak spadlo.
- **Švy mezi `run.py` a scrapery** (`tests/test_run_contract.py`): dispatch
  rozbaluje jiný počet hodnot u resumable zdroje, což je jinak výjimka
  v celou hodinu na úloze, kterou nikdo nesleduje.
- **Plánované zastavení není chyba** (`tests/test_interruptions.py`) — test
  čte zdrojáky scraperů a hledá neoznačená zastavení kvůli rozpočtu.
- **robots.txt včetně wildcardů** (`tests/test_robots_matching.py`)
  a kontrola, že každá URL, kterou scraper staví, je povolená
  (`tests/test_robots_policy.py`) proti uloženým snapshotům.
- **Deadline běhu drží i uvnitř jednoho requestu**
  (`tests/test_net_deadline.py`) — jinak retry s `Retry-After` vynese běh
  za hodinu o neomezenou dobu.
- **Zálohu nestačí postavit** (`tests/test_backup.py`): archiv se přebalí
  s jedním změněným bajtem, s chybějícím souborem a s manifestem, který
  lže o počtu řádků — a ověření to musí ve všech třech případech odmítnout.
  Plus že prázdný strom skončí chybou, ne tichým 200bajtovým „úspěchem",
  a že se `data/raw/` do archivu nedostane.

## Co budí sběr

GitHub's own scheduler does not deliver: across two days it ran 4 of roughly
150 scheduled attempts, including a stretch of nine hours with none, and
moving the cron off minute 0 and then to `*/15` changed nothing. The schedule
event is documented as best effort and on this repository it behaves like it.

So the collection is woken from outside, by a Cloudflare Worker on the free
plan that fires every fifteen minutes and does one thing:

    POST /repos/ZMAN-91/prague-reality-scraper/actions/workflows/scrape.yml/dispatches
    {"ref":"main","inputs":{"as_schedule":"true"}}

All the work still happens here, where the Actions minutes are free. What
crosses the network from outside is one request an hour's worth of bytes.

`as_schedule` is the load-bearing part: it sends the waker through the guard
(`tools/scrape_guard.sh`), which allows one sweep per fifty minutes. Without
it every firing would scrape and the portals would see four sweeps an hour
instead of the one this project takes.

The `*/15` cron stays in the workflow. It costs nothing, and on the rare
occasion GitHub does deliver, the guard rations it the same way - a delivered
attempt 35 minutes after a sweep is stopped in eight seconds.

The waker holds a fine-grained token scoped to this repository with
`Actions: write` and no expiry. It cannot reach the private dataset.

### Co waker budí

Od nasazení pražské verze budí waker všechna okna sám:

| pražský čas | workflow |
|---|---|
| 02:00–04:59 denně | `scrape-night.yml` (celá Praha) |
| zbytek dne, každou hodinu | `scrape.yml` (Praha 4 + 10) |
| Po a Út 05:00 | `report.yml` |
| Po a Út 06:00 | `backup.yml` (datový repozitář) |
| 1.–7. den v měsíci, 07:00 | `build-ruian-index.yml` |

Dřív tu stálo, že waker nebudí týdenní průchod nájmů a ten proto jako jediný
stojí na plánovači GitHubu. Obojí je překonané: nájem se sbírá každou hodinu
spolu s prodejem a samostatný workflow byl zrušen.

Crony ve workflow souborech zůstávají jako záloha pro případ, že waker
nedoručí. Jsou UTC-only, a tedy půl roku o hodinu vedle — což je přesně ten
důvod, proč okna počítá waker v `Europe/Prague`.
