# VIIRS protected areas — pixel JSON / SVG Rajac style

Овај patch додаје извоз `pixels[]` JSON фајлова за сва заштићена подручја.
Ти JSON фајлови су намењени за SVG приказ у старом Рајац стилу:
`radialGradient + circle + clipPath + feGaussianBlur`.

Излазни фајлови:

- `public/results/protected-areas/pixels/months/YYYY-MM/<pa_id>.json`
- `public/results/protected-areas/pixels/years/YYYY/<pa_id>.json`
- `public/results/protected-areas/pixels/index.json`

Први тест:

- `range_mode = single`
- `period_kind = month`
- `period = 2026-04`
- `area = rajac`
- `max_areas = 60`
- `overwrite = true`

За сва подручја за април 2026:

- `range_mode = single`
- `period_kind = month`
- `period = 2026-04`
- `area = all`
- `max_areas = 60`
- `overwrite = true`
