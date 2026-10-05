# Inventura sběru — postup

Úplná kontrola všeho, co tenhle projekt dělá. Soubor popisuje **postup**:
co zkontrolovat, co znamená „běží správně“ a čím se to ověří. Neobsahuje
žádná čísla z dat. Výsledky patří do privátního datového repozitáře
(`inventura/<datum>.md`), ne sem, protože tohle repo je veřejné.

Fakta sbírá `tools/inventory.py` jedním během (čte, nic nemění):

    python -m tools.inventory --data-dir <data>/data --logs-dir <data>/logs \
        --repo <data> --github --out <mimo toto repo>

## 0. Pravidla

- **Nic se neopravuje naslepo.** Nález se nejdřív reprodukuje na skutečných
  datech nebo lozích a oprava se prokáže přehráním téhož. Každá oprava má
  test, který bez ní selže. Opravuje se až po inventuře.
- **Neplést si „nevím“ s „v pořádku“.** Co se ověřit nedá (například kód
  Cloudflare workeru, který není v repu), se zapíše jako neověřené i
  s důvodem.
- **U každého čísla velikost vzorku.** Medián z pěti inzerátů není zjištění.
- **Kritika smyslu.** U každého výstupu se ptát: odpovídá na otázku, kterou
  si vlastník dat klade („jakobych celý den klikal a pamatoval si výsledky“)?
  Nebo jen vypadá jako odpověď?

## 1. Běží každá funkce správně?

U každé položky: co má dělat, kde je důkaz, že to dělá, a jak by vypadalo,
kdyby to tiše nedělala.

### Plánování a zámky

| součást | správně znamená | ověření |
|---|---|---|
| Cloudflare worker (mimo repo) | budí `scrape.yml` každou hodinu, noc 02–05 Praha, Po/Út report + backup, 1.–7. RÚIAN | rozestupy `created_at` u `workflow_dispatch` běhů v Actions za 7 dní; díry > 75 min |
| `tools/scrape_guard.sh` | nejvýš 1 sweep / 50 min, respektuje noční okno | log: `sweep_gaps_hours`; běhy ukončené guardem vs. skutečné sweepy |
| `tools/night_due.py` + krok `still_due` | právě jeden noční průchod za pražský den | logy: počet `scope=city` bez `kind` a `sreality city walk` za den |
| `tools/backup_due.sh` | jeden archiv za uzavřený týden, pojmenovaný tím týdnem | releases: tagy, `created_at` assetů; kolik backup jobů proběhlo po prvním úspěchu (má být 0) |
| `tools/report_due.py` | jeden report za uzavřený týden | `reports/<týden>.md` v gitu s „Data k“ = neděle týdne; skipped běhy po úspěchu |
| `tools/ruian_due.py` | index se staví 1.–7. v měsíci, jen když je starší | `data/ruian_praha.csv.gz.json` datum; běhy build-ruian-index |
| `heartbeat.yml` / `STATUS.md` | veřejné repo má commit častěji než 60 dní | datum posledního heartbeatu |
| concurrency `scrape-data` | nic, co zapisuje listings, neběží souběžně | překryvy start–konec běhů zapisujících data; report.yml má jinou skupinu, a proto do listings nesmí zapisovat (test `test_the_weekly_report_does_not_write_listings`) |

### Sběr

| součást | správně znamená | ověření |
|---|---|---|
| sreality, hodinově Praha 4+10 | index celý, detail jen tam, kde chybí GPS/plocha/dispozice | `fetched` vs. `total` z paginace v raw; `needed a detail fetch` v logu |
| sreality, noční procházka města | 4/4 scope, značí nepřítomnost po celé Praze | log `sreality city walk`: `scopes_absence_marked`, `reactivated` |
| iDNES, noc (celé město) + hodinově (Praha 4+10) | celý index za noc; tolerance jednoho průchodu | stránky v raw vs. `total_pages`; `missing_marked` noc po noci; **pondělí** (oprava c5ed1ca) |
| bezrealitky | sitemap + detaily v rozpočtu | `planned_stops` „budget exhausted“: kolik detailů zbývá, dožene se to? |
| rozpočty a přerušení | běh skončí včas a plánované stopy nejsou chyby | `duration_minutes` p100 vs. timeout workflow |
| robots/VOP (`common/robots.py`, `docs/podminky.md`) | tempo a rozsah odpovídají tomu, co bylo dohodnuto | počet requestů za hodinu na portál; změny robots.txt proti `docs/robots/` |

### Zpracování

| součást | správně znamená | ověření |
|---|---|---|
| `run.merge_source` | nový → řádek, známý → aktualizace, návrat → reaktivace | lifecycle: `returned_after_removal`, `returned_while_missing` |
| absence → `missing_N` → `removed` | odstraněno = skutečně zmizelo | **podíl odstraněných, které se vrátily** (míra falešného odstranění) po zdrojích |
| relisting (`relisted_from`) | nový inzerát téhož bytu se naváže na starý | počet, vzorek 10 ručně |
| párování (`common/dedup.py`) | stejný byt na dvou portálech = jeden cluster | cross-portal clustery; **„near misses“**: stejné místo, dispozice a plocha, jiná cena → nespárováno |
| GPS půjčování (`coords`, lend_gps) | vypůjčená souřadnice sedí | quality: shoda obvodů; vzorek 10 ručně |
| RÚIAN čísla (`common/ruian.py`) | číslo popisné/PSČ z registru | `number_pct`, `cislo_vzdalenost_m` rozdělení, vzorek 10 ručně |
| pozorování (`observations/`) | řádek jen při změně ceny/stavu | pozorování/den vs. změny; žádné duplicitní řádky |
| změny atributů (`changes/`) | každá změna plochy/dispozice/popisu | počet/den, nejčastější pole |
| rozdělení listings (d1e8bde) | živé v `listings.csv`, removed v archivu, nic dvakrát | `duplicates_across_files`, velikosti souborů |
| `tools/reconcile.py` + `commit_data.sh` | dva běhy neztratí data | commity s „merged“ v logu Actions; test proti gitu |

### Výstupy

| součást | správně znamená | ověření |
|---|---|---|
| `export_csv` / `episodes` / `market` | data/csv jednou denně, řada bez děr | health `check_days`, `check_exports_fresh` |
| `report` | týdenní report čitelný a pravdivý | přečíst celý W40: sedí čísla s `trh_denne.csv`? |
| `quality` | měří a selže při kolapsu | `data/state/quality.json` historie |
| `health` | ozve se, když něco nejede | projít každou kontrolu: dá se tiše obejít? |
| `backup` | obnovitelný archiv celé tabulky | stáhnout, `--verify`, porovnat s gitem bajt po bajtu |

### Opravné a jednorázové nástroje

`repair_absence`, `repair_prices`, `backfill_*`, `filter_street`,
`probe_*`: u každého rozhodnout, jestli je ještě potřeba, jestli po změnách
(rozdělení listings, celoměstská procházka) nedává chybné rady a jestli ho
nepoužívá žádný workflow bez důvodu. Mrtvý kód smazat jen se souhlasem.

## 2. Slabá místa

Aktivně hledat, co se rozbije tiše:

- Co se stane, když portál změní HTML/API. Který test to chytí? Za jak
  dlouho to health uvidí (počet hodin)?
- Co když worker přestane budit? Jak dlouho trvá, než přijde e-mail?
- Co když vyprší nebo se odvolá deploy key, token workeru nebo GitHub token?
- Závislosti: verze Pythonu, akcí (`actions/checkout@v4`,
  `setup-python@v5`), pinning `requirements*.txt`, deprecace Node runtime
  v akcích.
- Jediné body selhání: jeden účet, jeden deploy key, jeden worker.
- Reconcile („ours wins“) u souběžného reportu a hodinového běhu.
- Časová pásma a přelomy: DST (konec října), přelom roku (ISO týden 53),
  přelom měsíce (nový soubor observations/archiv).

## 3. Za tři roky

Z `growth` v inventuře, extrapolace s předpoklady napsanými vedle čísla:

- řádky a MB celé tabulky, archivu, observations, raw, csv, logů;
- velikost repozitáře vs. limit GitHubu (~1 GB doporučeno, 5 GB tvrdě) a
  soubor nad 100 MB;
- paměť a čas načtení všech inzerátů v každém běhu (`load_all_listings`);
  dedup, clustering a relisting jsou nad celou historií, jak škálují?
- minuty Actions (veřejné repo zdarma, ale dá se na to spolehnout?);
- runtime hodinového běhu vs. 50min rozestup;
- záloha: velikost archivu za 3 roky, limit 2 GB na asset release.

## 4. Co by mělo hodnotu, ale neukládá se

Z `raw` v inventuře (každé pole, které portál posílá, s mírou vyplnění)
proti `common/schema.LISTING_FIELDS`:

- vlastnictví (OV/DV), konstrukce (cihla/panel), stav, PENB, rok výstavby,
  výtah, balkon/terasa/sklep/parkování, počet podlaží domu;
- makléř vs. přímý majitel, RK;
- „sleva“ podle portálu (`discount_show`), topování, datum vložení podle
  portálu (tj. stáří inzerátu před začátkem sběru);
- poplatky a kauce u nájmu, cena za energie;
- raw index iDNES a bezrealitky ukládá jen počty, ne obsah. Co tím chybí
  pro audit?

U každého: užitek pro otázku uživatele, cena (bytů/den, requestů), a jestli
se dá doplnit zpětně (z raw archivu), nebo jen dopředu.

## 5. Statistická analýza a kritika

- Rozdělení cen a Kč/m² po segmentech a obvodech s n a 95% intervalem
  (bootstrap mediánu). Kde je interval tak široký, že číslo nic neříká?
- Denní řada: pohyb mezi dny bez události = šum. Nejmenší změna, kterou
  řada umí odlišit. Jsou týdenní změny v reportu nad ním?
- Doba na trhu: cenzura zleva (sběr běží krátce) a zprava (aktivní). Nejsou
  mediány v reportu zkreslené?
- Nabídka: počítá se byt na dvou portálech jednou? Kolik near misses
  zdvojuje nabídku?
- Falešná odstranění: jak zkreslují „zmizelé“ a absorpci?
- Nesmyslné hodnoty (plocha, cena, Kč/m², souřadnice mimo Prahu): kolik,
  odkud, filtrují je výstupy?
- Konzistence mezi portály: stejný byt, stejná plocha a dispozice? Pokud ne,
  který portál se plete?

## 6. Iterace

Nález → reprodukce → oprava s testem → přehrání na datech → znovu
`tools/inventory.py` → porovnat. Opakovat, dokud nejsou všechna zjištění
buď opravená, nebo vědomě přijatá s důvodem. Výsledek: `inventura/<datum>.md`
v datovém repu a shrnutí uživateli česky.
