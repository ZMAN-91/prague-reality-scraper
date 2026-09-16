# Jak moc tenhle projekt jde proti podmínkám portálů

Krátká odpověď: **u sreality.cz jednoznačně, a to na třech nezávislých úrovních.
U bezrealitky.cz je cesta, kterou data bereme, výslovně povolená, a jejich
podmínky o sbírání dat nemluví vůbec.** Detaily a přesné citace níže.

> Tohle není právní rada. Jsem program, ne advokát. Je to poctivé shrnutí toho,
> co si oba portály samy zveřejnily (stažené a uložené v `docs/robots/`), plus
> změřená čísla z reálných běhů tohohle scraperu. Rozhodnutí je tvoje; smyslem
> dokumentu je, abys ho dělal na základě skutečného textu, ne mého dojmu.

---

## 1. Co tenhle scraper doopravdy dělá (měřeno, ne odhadem)

| | sreality.cz | bezrealitky.cz |
|---|---|---|
| Vstupní bod | interní JSON API `/api/v1/estates/search` | `sitemap.xml`, který portál sám zveřejňuje |
| Požadavků na jeden kompletní sken Prahy | **33** (změřeno: `data/raw/sreality/2026-09-15/index-13.json.gz`) | procházka sitemapou + 1 stránka na nově viděný inzerát |
| Detail inzerátu | jednou za život inzerátu, nikdy znovu | jednou za život inzerátu, nikdy znovu |
| Pauza mezi požadavky | 1–2 s | 1–2 s |
| User-Agent | pravdivý, jmenuje projekt i účel; nepředstírá prohlížeč | totéž |
| Frekvence | každou hodinu → ~800 požadavků na index za den | totéž |
| Co neděláme | fotky, kontakty, přihlašování, obcházení captchy, paralelní stahování | totéž |

Pro srovnání: kdyby stejných ~12 500 pražských inzerátů chtěl jednou projít
člověk ve webovém rozhraní (20 inzerátů na stránku), je to zhruba **625 načtení
stránky** — a každá je mnohonásobně těžší než jedna JSON odpověď. Jeden náš sken
je tedy pro server řádově *lehčí* než totéž udělané ručně. Jenže my ho děláme
24× denně, což ručně nikdo nedělá.

**Tohle je ale ta méně důležitá část.** Zátěž serveru je věc, kterou umíme
technicky ladit donekonečna. Zákazy níže na zátěž vůbec necílí — cílí na
*výsledek*, tedy na to, že vzniká trvalá, systematicky obnovovaná kopie
podstatné části jejich databáze. Žádné zjemnění footprintu s tímhle nepohne.

---

## 2. sreality.cz

### 2.1 robots.txt — zakázáno

Celý soubor je v `docs/robots/www.sreality.cz.robots.txt` (979 řádků). Začíná
takto:

```
User-agent: *
Disallow: /

############################
#    neplecha ukončena     #
############################
```

Pak následuje 18 jmenovitých výjimek (Googlebot, SeznamBot, Bingbot, Applebot,
Baiduspider, YandexBot, DuckDuckBot, Slurp, msnbot, Naverbot, ia_archiver,
Mediapartners-Google, facebot, facebookexternalhit, Twitterbot, BingPreview,
vkShare, Mail.RU_Bot) — tedy vyhledávače a náhledy odkazů na sociálních sítích.

Čteno doslovně: **kdokoli, kdo není jeden z těch osmnácti, nemá povolenou ani
jednu jedinou URL.** Není tam žádná povolená cesta k datům — ani pomalá, ani
slušná. Ten komentář „neplecha ukončena“ navíc není generovaný text; někdo ho
tam napsal ručně, nejspíš právě po nějaké vlně scrapování.

Tenhle projekt to obchází jedinou řádkou v `.github/workflows/scrape.yml`:

```yaml
SCRAPER_ROBOTS_OVERRIDE_HOSTS: "www.sreality.cz"
```

Smazáním té řádky se přísné dodržování robots.txt vrátí a sreality z projektu
vypadne. Je to schválně jedno místo a schválně vypnuté by default.

### 2.2 Smluvní podmínky — zakázáno, a jmenovitě scrapování

Plný text: `docs/robots/www.sreality.cz.terms-1.1.txt`
(*Smluvní podmínky pro vkládání inzerce do databáze serveru Sreality.cz,
účinné od 14. 8. 2026*), sekce **Autorská práva a licenční ujednání**:

> „Sreality.cz, její obsah a jiné obsahové a technické komponenty […]
> představuje **databázi** ve smyslu příslušných ustanovení platných a účinných
> právních předpisů, zejména zákona č. 121/2000 Sb., o právu autorském […]“

> „Do databáze je **zakázáno jakkoli zasahovat**. Zejména je zakázáno databázi
> Sreality.cz **vytěžovat** nebo kvalitativně nebo kvantitativně **zužitkovávat**
> obsah databáze nebo její část, činit tak **systematicky či opakovaně**, a
> porušovat oprávněné zájmy pořizovatele databáze.“

> „Je zakázáno: […] (ii) užívat softwarových či jiných počítačových programů za
> účelem vytěžení databáze služby Sreality.cz (zejména prostřednictvím
> **scrapování dat** nebo jiným obdobným způsobem) […]“

Tohle není výklad ani analogie. Je to popis přesně toho, co tenhle program dělá,
včetně slova „scrapování“ a včetně „systematicky či opakovaně“.

**Jedna poctivá výhrada k tomu:** ty Podmínky jsou smlouva pro inzerenty.
Sami v sekci „Pro koho jsou tyto podmínky určeny?“ říkají, že jsou „určeny
zejména pro tzv. realitní zprostředkovatele“, a vztah vzniká jejich odsouhlasením
při registraci. Nepřihlášený návštěvník je neodsouhlasil, takže jestli ho
*smluvně* zavazují, je diskutabilní. Co ale diskutabilní není, je bod 2.3.

### 2.3 Zákon — platí i bez smlouvy

Zvláštní právo pořizovatele databáze (§ 88 a násl. autorského zákona,
implementace směrnice 96/9/ES) nevzniká souhlasem se smluvními podmínkami —
vzniká ze zákona. Dvě věci z něj jsou pro nás podstatné:

* chrání se vytěžování a zužitkování **podstatné části** obsahu databáze;
* a chrání se i **opakované a systematické** vytěžování *nepodstatných* částí,
  pokud to není běžné a přiměřené užití a poškozuje to oprávněné zájmy
  pořizovatele.

Ten druhý bod je napsaný přesně proti postupu „ber pokaždé kousek, ale ber ho
každou hodinu navždycky“. Tvoje formulace — *„jakobych celý den klikal a
pamatoval si výsledky“* — bohužel není obhajoba, ale docela přesný popis toho
jednání, na které ta úprava míří. A pozor: výjimka pro **osobní potřebu**
(§ 92) se podle převažujícího výkladu vztahuje jen na *neelektronické* databáze,
takže „je to jen pro mě“ tady nezachrání tolik, kolik by se čekalo.

*(Zákonná ustanovení cituji z paměti, ne ze staženého textu — na rozdíl od
citací výše, které jsou ověřené. Kdyby na tom mělo něco stát, ověř si znění
§§ 88–94 v aktuálním zákoně.)*

### 2.4 Verdikt pro sreality

| Úroveň | Stav |
|---|---|
| robots.txt | ❌ proti — a bez jakékoli povolené alternativy |
| smluvní podmínky | ❌ proti, jmenovitě („scrapování“, „systematicky či opakovaně“) |
| zákon (právo pořizovatele databáze) | ❌ proti, a platí nezávisle na smlouvě |

Není to šedá zóna. Je to informované rozhodnutí jít proti jasně vyjádřené vůli
provozovatele — jen s co nejmenší škodou (slušný footprint, žádné fotky, žádné
předstírání prohlížeče, pravdivá identifikace). To za tebe nemůžu rozhodnout a
ani se to nesnažím zabalit do měkčích slov.

---

## 3. bezrealitky.cz

### 3.1 robots.txt — povoleno

Celý soubor (`docs/robots/www.bezrealitky.cz.robots.txt`, 10 řádků):

```
#CS robots.txt
User-agent: *
Disallow: /vyhledat*
Disallow: /moje-bezrealitky/*
Disallow: /*callbackUrl=*
Disallow: /centrum-sluzeb/bezrealitky-hypoteka/form*
Disallow: /centrum-sluzeb/bezrealitky-hypoteka*price=*
Disallow: /centrum-sluzeb/bezrealitky-hypoteka?*
Disallow: /search*
Sitemap: https://www.bezrealitky.cz/sitemap/sitemap.xml
```

Je to opak sreality: zakázané je jen vyhledávání, uživatelský účet a formuláře
hypotéky. Detaily inzerátů (`/nemovitosti-byty-domy/*`) povolené jsou a portál
sám zveřejňuje sitemapu, tedy seznam těch stránek. Přesně tou cestou je scraper
napsaný — proto u bezrealitky **žádný override není a není potřeba**.

Jejich GraphQL API na `api.bezrealitky.cz` má naopak `User-agent: * / Disallow: /`
(`docs/robots/api.bezrealitky.cz.robots.txt`). Sběr dat se ho proto nedotýká —
původní verze scraperu na něj mířila a byla přepsaná právě kvůli tomuhle.
*(Výjimka pro úplnost: PDF se smluvními podmínkami, která jsou citovaná níže,
leží na tom hostu a stáhl jsem je jednorázově, abych je mohl přečíst. Je to
čtyři soubory jednou, ne sběr dat, ale píšu to sem, ať to není schované.)*

### 3.2 Smluvní podmínky — o sbírání dat mlčí

Bezrealitky zveřejňují čtyři dokumenty, všechny jako PDF
(`docs/robots/www.bezrealitky.cz.terms-1.*.pdf`, vytažený text vedle nich jako
`.pdf.txt`, přehled odkazů v `…terms-1.links.txt`):

1. Obchodní podmínky serveru Bezrealitky
2. Obchodní podmínky služby Inzerce
3. Obchodní podmínky o zajištění služby KOMFORT, NEMO REPORT a dalších
4. Zásady zpracování osobních údajů

Prohledal jsem je na `databáz`, `vytěž`, `zužitk`, `scrap`, `robot`, `automat`,
`rozmnož`, `kopírov`, `autorsk`, `duševn`, `licenc`:

* **žádný zákaz vytěžování databáze, scrapování ani automatizovaného přístupu tam
  není** — na rozdíl od sreality, kde je přesně takový zákaz doslova napsaný;
* jediné ustanovení o duševním vlastnictví (čl. 4 Obchodních podmínek služby
  Inzerce) jde opačným směrem: inzerent uděluje licenci Bezrealitky, aby jeho
  obsah mohly zveřejnit;
* podmínky navíc zavazují „Uživatele“ (majitele registrovaného účtu) a
  „Objednatele“ (inzerenta). Nepřihlášený čtenář není ani jedno.

### 3.3 Verdikt pro bezrealitky

| Úroveň | Stav |
|---|---|
| robots.txt | ✅ cesta, kterou bereme data, je povolená; zakázané cesty nepoužíváme |
| smluvní podmínky | ➖ o téhle věci mlčí; nezakazují ji, ale ani nepovolují |
| zákon (právo pořizovatele databáze) | ⚠️ platí i tady, robots.txt ho nevypíná |

Tohle je poctivá šedá zóna, ne porušení. Ale pozor na jednu věc: to, že je
*přístup* povolený, neznamená, že je automaticky v pořádku i *co s daty děláme
potom*. Zákonná ochrana databáze z bodu 2.3 platí i pro bezrealitky, jen ji
neopakují ve svých podmínkách.

---

## 4. Věc, která je proti tvému vlastnímu zadání

Řekl jsi: *„Nemám v plánu data přeprodat nebo jinak šířit. Je to čistě pro
soukromé účely.“* Jenže scraper commituje `data/` do **veřejného** GitHub
repozitáře. Každý běh publikuje popisy, adresy, GPS a cenovou historii
tisíců cizích inzerátů na veřejnou adresu, kterou umí najít i vyhledávač.

To už není soukromé užití — to je šíření. A je to zároveň ta jediná část celé
téhle situace, která se dá opravit jedním kliknutím, bez ztráty dat a bez
kompromisu na funkčnosti:

* **repozitář přepnout na private** — GitHub Actions i cron fungují dál stejně;
* nebo přestat commitovat `data/` a držet dataset jinde (Actions artifacts,
  vlastní privátní repo).

Kdyby sis měl z tohohle dokumentu odnést jednu jedinou věc, tak tuhle. Ze všech
bodů výše je tohle ten s nejlepším poměrem „dopad / námaha“.

---

## 5. Čím se dá rizikovost snížit (seřazeno podle účinku)

1. **Repozitář na private.** Odstraňuje šíření. Nula nákladů. Viz výše.
2. **Omezit sreality jen na sledované čtvrti.** Místo celé Prahy sbírat jen
   Hostivař / Horní Měcholupy / Záběhlice / Zahradní Město / Spořilov a okolí.
   Z „podstatné části databáze“ se stane malý výřez a z 33 požadavků asi 8.
   Cena: přijdeš o celoměstský kontext, proti kterému se ty čtvrti porovnávají.
3. **Napsat Seznamu.** V podmínkách uvádějí `info@sreality.cz`. Souhlas pro
   osobní nekomerční sledování trhu je věc, kterou ti buď dají, nebo nedají —
   ale zeptat se je jediný způsob, jak se z bodu 2 dostat legálně ven.
4. **Vypnout override.** Smazat řádku `SCRAPER_ROBOTS_OVERRIDE_HOSTS` z
   workflow. sreality vypadne, bezrealitky běží dál beze změny, protože ta
   nikdy na override nestála.

---

## 6. Shrnutí jednou tabulkou

| | sreality.cz | bezrealitky.cz |
|---|---|---|
| robots.txt | zakazuje úplně všechno všem kromě 18 vyhledávačů | povoluje cestu, kterou používáme |
| podmínky | zakazují scrapování a vytěžování jmenovitě | o tom nic neříkají |
| komu podmínky patří | inzerentům (nás nutně nezavazují smluvně) | uživatelům s účtem a inzerentům |
| zákonná ochrana databáze | platí | platí |
| náš footprint | 33 požadavků / sken, 1–2 s pauzy, pravdivý UA | sitemapa + 1 stránka na nový inzerát |
| **celkově** | **vědomě proti jejich vůli** | **šedá zóna, přístup povolený** |

Zdroje pro všechno výše jsou v `docs/robots/`. Stáhl je
`tools/probe_sources.py` přes workflow `.github/workflows/probe.yml`, doslovně
a s časovým razítkem, takže se dá ověřit, že jsem si nic nedomyslel — a když
portál podmínky změní, ukáže se to jako diff.
