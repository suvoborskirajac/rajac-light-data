# VIIRS Србија v1.3 — raster overlay + interpolated point query

Национални NASA Black Marble VNP46A2 производ задржава изворне ~500 m пикселе и детаљну националну маску из v1.2, али додаје лагане WebP raster overlay-е за брз рад карте.

- `surface.webp` — глатки топлотни приказ за јавну карту;
- `raw.webp` — неизглађени приказ изворне ~500 m мреже;
- оба raster-а су усклађена са Web Mercator приказом и одсечена националном маском;
- изворни JSON пиксели остају меродавни за очитавање вредности;
- веб сервис интерполира вредност на тачно кликнутој координати из најближих VIIRS узорака и више не помера избор на центар најближег пиксела.

## Прво v1.3 покретање

Actions → **NASA Black Marble VNP46A2 — Serbia national grid** → Run workflow

- mode: `baseline`
- Stable annual baseline: `2025`
- **Overwrite existing national period: није неопходно за raster допуну ако су v1.2 JSON подаци већ исправни.**

Workflow ће, и када JSON период већ постоји, направити/освежити raster overlay из постојећих 500 m пиксела.

VIIRS радијанса је `nW/cm²/sr`; није SQM мерење `mag/arcsec²`.
