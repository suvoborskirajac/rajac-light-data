# VIIRS raster karte za zaštićena područja Srbije

Овај додатак проширује постојећи систем `rajac-light-data` и додаје три врсте PNG карата:

- `raw` — изворни VIIRS пиксели
- `smooth` — глатка карта
- `heatmap` — топлотна мапа

## Излазни фајлови

Месечни:
- `public/results/protected-areas/rasters/months/YYYY-MM/<pa_id>.png`
- `public/results/protected-areas/rasters/months/YYYY-MM/<pa_id>_smooth.png`
- `public/results/protected-areas/rasters/months/YYYY-MM/<pa_id>_heatmap.png`

Годишњи:
- `public/results/protected-areas/rasters/years/YYYY/<pa_id>.png`
- `public/results/protected-areas/rasters/years/YYYY/<pa_id>_smooth.png`
- `public/results/protected-areas/rasters/years/YYYY/<pa_id>_heatmap.png`

## GitHub workflow

Workflow: `Actions → VIIRS protected areas raster maps`

Најважнији улазни параметар:
- `style_mode = heatmap` за нову топлотну мапу
- `style_mode = all` за raw + smooth + heatmap
