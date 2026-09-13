# VIIRS Србија v1.2 — detailed mask + preview grid

Ова допуна задржава NASA Black Marble VNP46A2 500 m производ, али исправља национални приказ:

- национална маска се при сваком workflow run-у освежава из детаљних geoBoundaries ADM0 геометрија;
- стара маска од само ~40 темена више није основа за clipping;
- 500 m пиксели се и даље узоркују само унутар националне маске;
- `index.json` сваког периода добија лаган `preview` grid од 0.04° за брз национални heatmap;
- детаљни 500 m JSON tiles остају непромењени за zoom и очитавање тачке.

## После постављања patch-а у корен `rajac-light-data`

Actions → **NASA Black Marble VNP46A2 — Serbia national grid** → Run workflow

За прву v1.2 реконструкцију:

- mode: `baseline`
- Stable annual baseline: `2025`
- **Overwrite existing national period: укључити**

Тако ће се поново направити `years/2025` и последњи завршени месец са новом маском и preview grid-ом.

VIIRS радијанса је `nW/cm²/sr`; није SQM мерење `mag/arcsec²`.
