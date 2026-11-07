# East Asia Weather Daily

The 05:00 JST data-collection pass for Hong Kong, Taipei, Tokyo Minato Ward and Takamatsu. One existing ChatGPT automation performs calculation, charting and Japanese reporting at 06:00 JST.

## Run and handoff

- One GitHub Actions workflow: `.github/workflows/collect.yml`.
- Daily cron: `0 20 * * *` UTC, corresponding to 05:00 JST the following day. GitHub can delay scheduled jobs; this is not a guaranteed start time.
- Manual runs and collector-code pushes also run a live collection test.
- Collection uses bounded HTTPS retries and independent requests per city/provider. NOAA binary files use the pinned ecCodes/netCDF4 decoders in `requirements-collection.txt`; temporary full files are deleted after point extraction.
- Full raw responses are retained as Actions artifacts for 14 days.
- A separate `data` branch contains `latest/manifest.json`, four city JSON files, a regional forecast bundle and an official-source bundle. Data updates do not trigger another collection run.
- The manifest records the report date, exact 24-hour window, source status, per-field completeness, checksums and the workflow run. A successful workflow does **not** mean every requested forecast is available.

The report window is 06:00 JST to the next 06:00 JST (05:00 local time in Hong Kong and Taipei). The long-range window ends exactly 384 hours after report start.

```bash
python3 scripts/collect.py --target-date-jst 2026-11-08 --out-dir output
python3 -m unittest discover -s tests -v
```

Install `requirements-collection.txt` before collection. The collector exits unsuccessfully if required weather, air-quality, snowfall ensemble, hourly tide, surge or regional long-range input checks fail. It still writes diagnostics. The original requirements remain the acceptance scope; a successful migration or workflow does not make unfinished requirements optional.

## Numerical sources

| Source | Use | Important qualification |
|---|---|---|
| Open-Meteo ECMWF IFS, explicitly selected | Four-city hourly weather, WBGT inputs, wind, pressure, fog/frost inputs, precipitation and snow | Each required variable must contain 24 finite values; preceding-day pressure is checked separately |
| Open-Meteo CAMS global | AQI components, pollutants, dust and UV | CAMS grid-scale forecasts, not local observations |
| ECMWF IFS plus EC46 ensemble mean | Hong Kong and Tokyo temperature/weather through 384 hours | Native six-hour, 36 km EC46 values extend beyond IFS. Each temperature sample identifies its model; no fabricated hourly tail or silent non-ECMWF substitution |
| ECMWF IFS 0.25° ensemble | Independent snowfall probability inputs, 51 members for all four cities | At 06:00 calculate the fraction of members with snowfall in the exact 24-hour window; never substitute rain probability |
| ECMWF EC46 regional grids | Seven requested regions, nine locations each, 384-hour temperature/pressure/wind/rain/snow guidance and spread | Coarse regional samples cannot resolve cyclone cores; combine with official tracks and the dedicated WNP watch |
| HKO, CWA and JMA | Official forecasts, warnings and tides | Old warning issue times are flagged; astronomical tide datums are kept separate |
| JMA R8 warning, probability and timeline endpoints | Current warnings, future warning-level potential and local risk timelines | The legacy JSON endpoints stopped updating around the May 2026 transition; retain per-product issue times, including older unchanged cancellations |
| NOAA STOFS-2D-Global | Actual 24-hour surge residual and combined water-level series at all four coastal references | Hong Kong uses the nearby verification station; Taipei/Tokyo/Takamatsu use fixed wet grid cells. This is NOAA ADCIRC/GFS guidance, not GTSM; preserve coordinates, distances and datum |
| CWA Danshuei station 1102 | Actual hourly astronomical tide across the report window | Keep this station reference/units separate from town-level Tamsui high/low events |
| Weathernews Japanese pollen | Current hourly backend arrays and publication status | Public pages are closed while preparing spring 2027. Retain that operational applicability caveat; do not silently treat the backend as an all-pollen operational forecast |
| NOAA SWPC and NICT | Space-weather source material | SWPC issue times are parsed; unverified page freshness is not labelled current |
| GloFAS and marine API | Raw river discharge and coastal sea-level reference | River grids are **unvalidated**; marine mean-sea-level heights are **not** GTSM surge |

Open-Meteo responses do not identify the initialization cycle. It remains null, with an explanation; `generationtime_ms` is never used as an issue time. HTTP fetch time, official issue time and forecast valid times remain distinct.

## 06:00 reader

```bash
python3 scripts/read_bundle.py --target-date-jst 2026-11-08 --out output/report_raw.json
```

This pins reads to one `data` commit, rejects another report date or a collection older than three hours, checks every file's byte length and SHA-256, and reconstructs the report's raw input. Its success does not override `quality.ready`.

When direct GitHub HTTPS access is unavailable, the connected GitHub reader can retrieve the same manifest and files at a single pinned commit. The raw Actions artifact is available for detailed source investigation.

## 06:00 calculation

```bash
python3 -m pip install -r requirements-compute.txt
python3 scripts/compute.py --input output/report_raw.json --output output/calculated.json
```

This implements physically based Liljegren WBGT using ECMWF thermofeel 2.2.0, instantaneous forecast radiation and solar position; conditional wind chill; same-hour 24-hour pressure changes; AQI components; and independent ensemble snowfall probability. Accumulations use `(start,end]` because each source timestamp represents the preceding hour. Instantaneous variables use `[start,end)`.

The 05:00 collector does not run these report calculations.

## Chart

`scripts/render_chart.py` preserves the requested date semantics: a daytime high sits at its date tick; the minimum from that noon to the following noon sits halfway between those dates. Each city has one solid curve and continuous translucent high/low shading. Actual extrema hours never move the points. Missing data is not filled from daily summaries.

```bash
python3 -m pip install -r requirements-chart.txt
# Install Noto Sans CJK JP and Noto Sans Symbols2 fonts in the rendering environment.
python3 scripts/render_chart.py --input output/report_raw.json --target-date-jst 2026-11-08 --output output/chart.png
```

The collector does not compute WBGT, assess weather risk or draw charts at 05:00. Those remain 06:00 tasks.

## Original requirements still requiring resolution

- GloFAS river reach and flood-threshold applicability; urban inundation forecasts.
- Operational applicability of Japanese pollen outside the provider's published season. Current backend numbers and the closed-page notice are both retained.
- The 06:00 regional severe-weather assessment must combine the actual 384-hour regional inputs with current cyclone warnings and the dedicated watch; sparse grids alone are not complete spatial cyclone coverage.

Surge retrieval, independent snow inputs, Taipei hourly tides and the obsolete Japanese warning endpoints have implemented retrieval routes. GLOSSIS/BlueEarth remains unavailable; NOAA is explicitly identified as the alternate surge source. These are all parts of the original request.

`quality.ready` must stay false while these report requirements are unresolved. No missing forecast is converted to zero or a low-risk assessment.

Optional `CWA_API_KEY` can be supplied as a repository secret for the official datastore. It is never committed or included in saved URLs/errors. Public CWA tide and warning pages do not require this secret.

## Source documentation

- https://open-meteo.com/en/docs/ecmwf-api
- https://open-meteo.com/en/docs/seasonal-forecast-api
- https://open-meteo.com/en/docs/air-quality-api
- https://www.data.jma.go.jp/kaiyou/db/tide/suisan/readme.html
- https://data.gov.hk/en-data/dataset/hk-hko-rss-hourly-heights-of-tides
- https://www.cwa.gov.tw/V8/C/M/Fishery/tide_30day_MOD/T500010.html
- https://services.swpc.noaa.gov/
- https://www.jma.go.jp/jma/press/2604/14b/20260414_taikeiseiri.html
- https://polar.ncep.noaa.gov/estofs/glo.htm
- https://nomads.ncep.noaa.gov/txt_descriptions/STOFS_Global_txt.html
- https://github.com/ecmwf/thermofeel

Weather data attribution: ECMWF via Open-Meteo; CAMS via Open-Meteo; HKO; CWA; JMA; NOAA STOFS/SWPC; NICT; Weathernews. Preserve source attribution when reusing the data. Check the applicable source terms for redistribution.
