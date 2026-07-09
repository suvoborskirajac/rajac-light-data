# VIIRS raster maps v2 — raw + smooth

Ова допуна додаје две варијанте PNG карте за свако заштићено подручје и изабрани период:

- `raw` — изворни VIIRS пиксели (стручни приказ)
- `smooth` — глатка презентациона карта (bicubic resampling + благо smoothing)

## Излази

Месец:

- `public/results/protected-areas/rasters/months/YYYY-MM/<pa_id>.png` → raw
- `public/results/protected-areas/rasters/months/YYYY-MM/<pa_id>_smooth.png` → smooth

Година:

- `public/results/protected-areas/rasters/years/YYYY/<pa_id>.png` → raw
- `public/results/protected-areas/rasters/years/YYYY/<pa_id>_smooth.png` → smooth

Workflow параметар `style_mode`: `raw`, `smooth` или `both`.
