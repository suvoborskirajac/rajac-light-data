# Rajac Light Pollution — GitHub Actions processor v0.4.0

Ovo je paket za **prvu fazu**: automatska izrada stvarnih VIIRS mesečnih JSON rezultata za WordPress prikaz.

## Šta se dobija

- `public/results/index.json`
- `public/results/YYYY-MM.json` za poslednjih 12 dostupnih meseci
- stvarne satelitske vrednosti noćne radijanse `avg_rad` u `nW/cm²/sr`
- sirovi pikseli u granici PIO Rajac i statistika po periodu

## Dataset

Podrazumevano se koristi:

`NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG`

To je mesečni VIIRS DNB kompozit sa stray-light korekcijom. Rezolucija je oko 463,83 m po pikselu. Za detaljnu kartu izvora svetla od 10–40 m treba poseban modul, jer ovo nije taj tip podatka.

## Pokretanje

U GitHub repo ubaciti ceo sadržaj ovog paketa u root repozitorijuma.

Zatim:

1. Settings → Secrets and variables → Actions
2. Secret mora da se zove: `GEE_SERVICE_ACCOUNT_JSON`
3. Actions → Build Rajac light pollution data → Run workflow
4. U input `months` upisati `12`

Kada se workflow završi, WordPress plugin čita:

`https://raw.githubusercontent.com/suvoborskirajac/rajac-light-data/main/public/results/index.json`

## Važno

Ako `index.json` ne postoji, WordPress viewer neće prikazivati demo podatke. Prikazaće samo granicu PIO i poruku da rezultati još nisu napravljeni.
