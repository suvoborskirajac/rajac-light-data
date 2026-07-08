VIIRS RASTER MAPS — GitHub patch

Овај patch додаје Earth Engine извоз PNG растер-карата за заштићена подручја.

ГДЕ СЕ УБАЦУЈЕ:
Корен GitHub репозиторијума:
https://github.com/suvoborskirajac/rajac-light-data

ФАЈЛОВИ:
/protected_areas/protected_areas_raster_exporter.py
/.github/workflows/viirs-protected-areas-rasters.yml
/README-VIIRS-RASTER-MAPS.md

ПРВА ПРОБА:
Actions → VIIRS protected areas raster maps → Run workflow
range_mode: single
period_kind: month
period: 2026-04
area: rajac
max_images: 10
overwrite: false

СВА ПОДРУЧЈА ЗА ЈЕДАН МЕСЕЦ:
range_mode: single
period_kind: month
period: 2026-04
area: all
max_images: 60

СВА ПОДРУЧЈА ЗА СВЕ ГОДИНЕ:
range_mode: all_years
period_kind: year
period: auto
start_year: 2014
area: all
max_images: 700

НЕ ПРЕПОРУЧУЈЕ СЕ ОДМАХ:
Сви месеци × сва подручја = преко 6500 PNG слика. То је могуће, али треба радити у серијама, због времена извршавања и величине GitHub репозиторијума.
