# VIIRS статистика за заштићена подручја Србије

Ово је додатак за постојећи систем `rajac-light-data`. Не мења постојеће карте за ПИО „Рајац”, већ додаје нови излаз:

`public/results/protected-areas/`

## Шта ради

Скрипт `protected_areas_processor.py` преко Google Earth Engine-а чита месечни VIIRS DNB композит `NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG`, бенд `avg_rad`, и рачуна статистику за сва заштићена подручја из `zasticena_podrucja_srbije_gee.geojson`.

За свако подручје прави:

- `avg_rad_mean`, `avg_rad_median`, `avg_rad_p90`, `avg_rad_p95`, `avg_rad_max`;
- `cf_cvg_mean` као контролу cloud-free покривености;
- `valid_km2` и `valid_pct_of_area`;
- површине и проценте по класама радијансе;
- посебан ред за ПИО „Рајац”, ако је у GeoJSON-у `rajac=true` или ако се назив препозна као Рајац.

## Резултати

После обраде добијају се:

```text
public/results/protected-areas/index.json
public/results/protected-areas/latest.json
public/results/protected-areas/rajac.json
public/results/protected-areas/months/YYYY-MM.json
public/results/protected-areas/years/YYYY.json
public/results/protected-areas/areas/<pa_id>.json
```

## Прво покретање

На GitHub-у отвори **Actions → VIIRS protected areas Serbia → Run workflow** и изабери:

```text
mode = all
start = 2014-01
end = auto
overwrite = false
```

То прави комплетну серију од 2014. године до најновијег доступног VIIRS месеца.

## Редовно освежавање

Workflow је подешен да се покреће 8, 18. и 28. дана у месецу у 05:30 UTC. Тада користи `mode=latest` и обрађује само најновији доступан месец, па је брз и не преписује старе резултате.

## GitHub secrets

У репозиторијуму треба да постоје ова три secrets:

```text
EE_PROJECT_ID
EE_SERVICE_ACCOUNT
EE_SERVICE_ACCOUNT_KEY_JSON
```

За твој постојећи систем то ће највероватније бити:

```text
EE_PROJECT_ID = deft-epigram-414409
EE_SERVICE_ACCOUNT = rajac-light-monitor@deft-epigram-414409.iam.gserviceaccount.com
```

`EE_SERVICE_ACCOUNT_KEY_JSON` је цео JSON кључ service account-а. Не уписује се у код и не поставља се јавно.

## Локално тестирање

```bash
pip install -r requirements-protected-areas.txt
python protected_areas/protected_areas_processor.py \
  --mode latest \
  --yearly \
  --areas-geojson protected_areas/zasticena_podrucja_srbije_gee.geojson \
  --results-dir public/results/protected-areas \
  --project deft-epigram-414409 \
  --service-account rajac-light-monitor@deft-epigram-414409.iam.gserviceaccount.com \
  --key-file /putanja/do/ee-key.json
```
