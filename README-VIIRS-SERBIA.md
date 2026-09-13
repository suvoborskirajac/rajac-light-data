# VIIRS Србија — национални Black Marble grid

Овај модул проширује постојећи `rajac-light-data` систем са заштићених подручја на целу територију Србије.

## Извор

NASA Black Marble VNP46A2 Collection 2, бенд `DNB_BRDF_Corrected_NTL`, 500 m. Користи исти quality filter као `black_marble/build_black_marble.py`.

**Јединица:** `nW/cm²/sr`. Ово није SQM `mag/arcsec²` и не сме се представљати као директно теренско мерење светлине неба.

## Зашто су подаци подељени на плочице

Цела Србија на 500 m садржи стотине хиљада валидних пиксела. Један велики JSON би био непрактичан. Зато је земља подељена на географске плочице од 0,4° и свака плочица садржи компактне тројке:

```json
[longitude, latitude, radiance]
```

## Излаз

```text
public/results/serbia/index.json
public/results/serbia/years/2025/index.json
public/results/serbia/years/2025/tiles/rXX-cYY.json
public/results/serbia/months/YYYY-MM/index.json
public/results/serbia/months/YYYY-MM/tiles/rXX-cYY.json
```

## Прво покретање

Actions → **NASA Black Marble VNP46A2 — Serbia national grid** → Run workflow:

- `mode = baseline`
- `baseline_year = 2025`
- `overwrite = false`

То прави стабилни годишњи слој 2025. и најновији завршени месец.

## Редовно освежавање

Scheduled workflow покушава више пута месечно да направи најновији завршени месец. Тренутни календарски месец се никада не објављује као завршен.

## Класе

Национални систем користи исте статистичке класе као постојећи Black Marble процесор:

- 0–0,25: врло тамно
- 0,25–0,50: тамно
- 0,50–1,00: ниско
- 1–3: умерено
- 3–10: повишено
- ≥10: високо

Напомена: стари `pixel_class()` у `build_black_marble.py` имао је другачије границе за legacy pixel JSON. Национални модул намерно користи `CLASS_DEFS`, јер су то класе које се већ користе у статистичким табелама за заштићена подручја.
