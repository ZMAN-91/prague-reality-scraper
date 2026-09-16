# Proč je projekt ve dvou repozitářích

```
prague-reality-scraper          VEŘEJNÝ    kód, testy, docs, workflows sběru
prague_reality_sector_analysis  PRIVÁTNÍ   data/, logs/, workflow zálohy
```

## Důvod

Minuty GitHub Actions se účtují podle repozitáře, kde workflow **běží**, ne
kam zapisuje. Veřejné repo má standardní runnery zdarma a bez limitu;
privátní má na Free plánu 2 000 minut měsíčně. Tenhle sběr spotřebuje
~14 500 minut měsíčně (~163 běhů prodeje týdně po ~19 minutách, plus
pronájmy), takže privátně by stál ~$100 měsíčně a veřejně nic.

Data ale veřejná být nesmí. Nejde o utajení — jsou to veřejné inzeráty —
ale o to, že veřejný repozitář s kompletním datasetem **je** šíření dat,
kterému se projekt cíleně vyhýbá (`podminky.md`).

## Jak to drží pohromadě

Workflow ve veřejném repu si privátní data naklonuje deploy keyem do `store/`:

```yaml
- uses: actions/checkout@v4                       # kód
- uses: actions/checkout@v4                       # data
  with:
    repository: ZMAN-91/prague_reality_sector_analysis
    ssh-key: ${{ secrets.DATA_REPO_KEY }}
    path: store
- run: python run.py --data-dir store/data --logs-dir store/logs
```

**Deploy key, ne PAT.** Platí pro jeden jediný repozitář (PAT pro celý účet)
a **neexpiruje**, což u něčeho, co má běžet roky bez zásahu, není detail.

Ověřit ho jde kdykoli: workflow `verify-access.yml`, ruční spuštění, nesahá
na data (zápis testuje přes `git push --dry-run`). Rozliší chybějící klíč od
klíče bez „Allow write access" — druhá varianta je zákeřná, protože čtení
funguje a sběr by běžel zeleně, jen by nic neukládal.

## Proč záloha běží jinde

`deploy/backup.yml` **se odsud nespouští**. Publikuje release s celým
datasetem, takže musí vzniknout v privátním repu. Navíc deploy key neumí
API, takže by odsud release nešel vytvořit bez PAT.

Běží tedy v privátním repu — stojí ~12 minut měsíčně, což se do free kvóty
vejde s obrovskou rezervou — a kód si naklonuje odsud (veřejné, bez klíče).

Soubor je ale **kód**, takže zdroj pravdy je tady, v `deploy/backup.yml`,
kde ho vidí testy. Nasazená kopie je v privátním repu
v `.github/workflows/backup.yml`. Při změně je potřeba ji zkopírovat.

## Proč se nepřepnulo tohle repo na veřejné

Původní repozitář obsahoval data od začátku a přepnutí na veřejné by
zveřejnilo **celou historii**: 77 commitů, `.git` 73 MB, z toho ~45 MB
syrových API odpovědí sreality. Kód se proto přestěhoval do nového čistého
repozitáře — data tak historii nikdy neopustila a nic se nemuselo přepisovat.

## Co tím není vyřešené

Data i zálohy leží na GitHubu. Proti ztrátě účtu to nechrání — to by
potřebovalo externí úložiště (R2, B2).
