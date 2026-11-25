#!/usr/bin/env python3
"""Structured validation for every reported variable and hazard.

Built by prepare_report from the raw collection and the calculated
bundle, published as validation.json next to the other products. The
06:00 run reads it BEFORE composing the report: a variable or hazard
marked 'invalid' or 'missing' is never presented as a valid assessment,
and a 'caution' carries the exact caveat that applies (review fix,
2026-11-08 — the overarching change: a forecast that is numerically
available but scientifically inapplicable, stale, incomplete or based
on incompatible definitions must not pass as valid).
"""
import datetime as dt

STANDING_LIMITATIONS = [
    {'id': 'glofas_reach_validation',
     'text': 'GloFAS river reaches and return-period thresholds are unvalidated: no riverine flood flag is derived from GloFAS in either the bundle path or the fallback path.'},
    {'id': 'urban_inundation_validation',
     'text': 'Urban (pluvial) inundation has no validated local model: rainfall-rate screening only, kept distinct from river flooding.'},
    {'id': 'weathernews_pollen_applicability',
     'text': 'Weathernews pollen numeric arrays are retrieved, but operational applicability is unverified while the public pages are closed.'},
    {'id': 'ecmwf_opencharts_endpoint',
     'text': 'OpenCharts has returned HTTP 404. The IFS medium-range Open Data BUFR stream provides model trajectories. JTWC-guided GFS and IFS grid checks provide separate evidence.'},
    {'id': 'regional_grid_coverage',
     'text': 'The regional grids are sparse 3x3 samples: they cannot exclude a storm passing between sampling points, and no verdict of safety rests on them alone.'},
    {'id': 'stofs_datum',
     'text': 'STOFS levels sit on the NOAA model datum: combined levels are compared only with the STOFS model\'s own astronomical tide, never with official tide-table datums.'},
]

STANDING_FAILED_SOURCES = {'ecmwf/cyclone_product'}


def _item(status, detail):
    return {'status': status, 'detail': detail}


def _ready(block):
    return isinstance(block, dict) and block.get('ready') is True


def build_validation(raw, calculated):
    cities = {}
    for name, c in (calculated.get('cities') or {}).items():
        if c.get('error'):
            cities[name] = {'calculation': _item('invalid', c['error'])}
            continue
        items = {}
        for var, key in (('temperature', 'temperature_2m'),
                         ('apparent_temperature', 'apparent_temperature'),
                         ('humidity', 'relative_humidity_2m'),
                         ('pressure', 'pressure_msl'),
                         ('uv', 'uv_index'), ('dust', 'dust')):
            items[var] = _item('ok', '24/24 hourly values in the window') if _ready(c.get(key)) \
                else _item('missing', f'{key} not ready for the window')
        items['wbgt'] = _item('ok', 'Liljegren calculation on complete instantaneous inputs') \
            if _ready(c.get('wbgt')) else _item('missing', (c.get('wbgt') or {}).get('reason', 'WBGT not ready'))
        wind_ok = _ready(c.get('wind_speed_10m'))
        gust_ok = _ready(c.get('wind_gusts_10m'))
        items['wind'] = _item('ok', 'sustained and gust series complete; mean, maximum and gust maximum are distinct statistics') if (wind_ok and gust_ok) else \
            _item('caution', 'gust series incomplete; report sustained values only') if wind_ok else \
            _item('missing', 'wind series not ready')
        items['precipitation'] = _item('ok', '(start,end] preceding-hour accumulations, 24/24') \
            if _ready(c.get('precipitation')) else _item('missing', 'precipitation not ready')
        snow_amt = _ready(c.get('snowfall'))
        snow_prob = _ready(c.get('snow_probability'))
        items['snow'] = _item('ok', 'snowfall amount, independent ensemble snowfall probability and snow depth carried as separate quantities') if (snow_amt and snow_prob) else \
            _item('caution', 'independent snowfall probability unavailable; snowfall amount alone must not be read as the snow risk') if snow_amt else \
            _item('missing', 'snowfall not ready')
        aqi = c.get('aqi') or {}
        items['aqi'] = _item('ok', 'leading and second pollutants are read at the worst-AQI hour itself') \
            if (_ready(aqi) and aqi.get('leading_component_at_worst_hour')) else \
            _item('caution', 'AQI ready but no component breakdown at the worst hour') if _ready(aqi) else \
            _item('missing', 'AQI not ready')
        fog = c.get('fog') or {}
        if not fog.get('ready'):
            items['fog'] = _item('missing', 'visibility series incomplete')
        elif fog.get('classification') == 'visibility_reduced_cause_undetermined':
            items['fog'] = _item('caution', 'visibility below 1,000 m WITHOUT saturation corroboration at those hours: report reduced visibility, never fog')
        elif fog.get('classification') == 'fog':
            items['fog'] = _item('ok', 'low visibility corroborated by humidity / dew-point spread / low cloud at the same hours')
        else:
            items['fog'] = _item('ok', f"classification: {fog.get('classification')}")
        items['frost'] = _item('ok', 'IFS ground-surface (skin) temperature; the GFS 0–10 cm soil limb is non-equivalent and contextual only') \
            if _ready(c.get('surface_temperature')) else _item('missing', 'surface temperature not ready')
        coast = c.get('coastal_assessment') or {}
        if coast.get('exceedance_over_model_tide_m') is not None and coast.get('aligned_hours') == 24:
            items['storm_surge'] = _item('ok', 'STOFS combined level compared with the STOFS model\'s own astronomical tide (same model datum); surge residual reported separately; official tide tables never subtracted')
        elif coast.get('stofs_ready'):
            items['storm_surge'] = _item('caution', 'no same-datum comparison available for this city: surge residual stands alone; any storm-surge flag needs an official product')
        else:
            items['storm_surge'] = _item('missing', 'STOFS coastal forecast not ready')
        tide = coast.get('official_tide') or {}
        items['tide'] = _item('ok', 'official tide events on their own stated datum (timing and heights; never mixed with STOFS levels)') \
            if tide.get('ready') else _item('missing', 'official tide events not ready')
        urban = c.get('urban_inundation') or {}
        if not urban.get('ready'):
            items['urban_inundation'] = _item('missing', 'rainfall-rate screen unavailable')
        elif urban.get('screen_positive'):
            items['urban_inundation'] = _item('caution', 'rainfall-rate screen POSITIVE: assess urban inundation distinctly from river flooding')
        else:
            items['urban_inundation'] = _item('ok', 'rainfall-rate screen negative; a negative screen — like an empty warning feed — does not establish safety')
        raw_city = (raw.get('cities') or {}).get(name) or {}
        pollen = raw_city.get('pollen')
        if pollen is None:
            items['pollen'] = _item('missing', 'no pollen block for this city (CAMS pollen is Europe-only: no claim either way)')
        else:
            items['pollen'] = _item('caution', 'pollen state (in season / out of season / not yet published / unavailable) must be read from the source itself; none of these states is a zero')
        # Official warnings are normalized in GitHub, not inferred from
        # a prefectural headline at report-delivery time.
        city_key = (raw_city.get('key') or
                    {'Taipei': 'taipei', 'Tokyo Minato Ward': 'tokyo_minato',
                     'Takamatsu': 'takamatsu'}.get(name))
        official = ((calculated.get('official_hazards') or {}).get('cities') or {}).get(city_key)
        if isinstance(official, dict):
            status = official.get('status')
            if status == 'ready':
                active = official.get('active') or []
                items['official_warning'] = _item(
                    'ok', f"{len(active)} exact-area official alerts; "
                          "zero is a current warning snapshot, not a 24h safety forecast")
            else:
                items['official_warning'] = _item(
                    'caution' if status in ('caution', 'unassessed', 'stale') else 'missing',
                    f"municipal warning applicability: {status}; "
                    f"{official.get('reason', 'not independently validated')}")
            if city_key == 'taipei':
                for field, target in (('urban_inundation', 'wra_urban_warning'),
                                      ('river_level', 'wra_river_warning')):
                    wra = official.get(field) or {}
                    code = 'ok' if wra.get('status') == 'ready' else (
                        'caution' if wra.get('status') in ('caution', 'unassessed') else 'missing')
                    items[target] = _item(
                        code, f"WRA {field}: {wra.get('status')}; "
                              f"{len(wra.get('warnings') or [])} alerts explicitly scoped to Taipei; "
                              "separate from rainfall and GloFAS point screens")
        elif city_key in ('taipei', 'tokyo_minato', 'takamatsu'):
            items['official_warning'] = _item('missing', 'no computed official municipal warning assessment')
        cities[name] = items

    watch = calculated.get('cyclone_watch') or {}
    systems = watch.get('systems') or []
    advisory_status = watch.get('advisory_status')
    if systems:
        advisory = _item('ok', f'{len(systems)} active systems parsed from the WNP ABPW advisory')
    elif advisory_status == 'quiet':
        advisory = _item('ok', 'the WNP ABPW advisory explicitly reports no active systems')
    elif advisory_status in ('expired', 'not_yet_valid', 'validity_unverified'):
        advisory = _item('invalid', f'JTWC ABPW validity at report time: {advisory_status}; '
                                     'no current advisory conclusion')
    elif advisory_status == 'unrecognized':
        advisory = _item('invalid', 'JTWC ABPW response could not be recognized; no quiet conclusion')
    else:
        advisory = _item('missing', 'JTWC ABPW advisory unavailable or not assessed; no quiet conclusion')
    assessments = [v.get('assessment') or {} for v in (watch.get('track_checks') or {}).values()]
    retrieved = [a for a in assessments if a.get('retrieved')]
    consistent = [a for a in retrieved if a.get('agreement') == 'consistent_with']
    screened = [a for a in retrieved if a.get('centre_identified')]
    if any(v.get('status') == 'available' for v in (watch.get('ifs_cyclone_tracks') or {}).values()):
        cyclone = _item('caution', 'Regional IFS medium-range BUFR model trajectories retrieved; agreement with official warning tracks and EC46 ensemble tracks remain unverified')
    elif not systems:
        cyclone = (_item('ok', 'no active systems in the ABPW advisory; verification rows still apply to the next system')
                   if advisory_status == 'quiet'
                   else _item('missing', 'no verified JTWC advisory or independent-model track check available'))
    elif screened:
        cyclone = _item('caution', 'IFS pressure and 10-m wind yield JTWC-guided local centre candidates; basin-wide identification and EC46 ensemble track spread are still NOT verified')
    elif consistent:
        cyclone = _item('caution', 'independent-model pressure samples are coherent with JTWC positions but do not identify model cyclone centres')
    elif retrieved:
        cyclone = _item('caution', 'independent-model samples retrieved but no model centre/track identification is confirmed')
    else:
        cyclone = _item('missing', 'no independent-model track check retrieved for an active system')
    space = calculated.get('space_weather') or {}
    ok_flags = space.get('sources_ok') or {}
    if space.get('geomagnetic_forecast_status') == 'unverified':
        space_item = _item('caution', 'SWPC geomagnetic forecast is stale or unverified (unparsed); no current G-scale forecast verified')
    elif space.get('alert_feed_status') in ('stale', 'unverified') and space.get('alerts_count') is not None:
        space_item = _item('caution', 'SWPC alert feed freshness is stale/unverified; no active-warning claim can be verified from it')
    else:
        space_item = _item('ok', 'SWPC and NICT sources current; R/S/G scales are reported as distinct quantities') \
            if all(v for v in ok_flags.values()) else \
            _item('caution', f'some space-weather sources failed: {ok_flags}')
    new_failures = sorted(
        name for name, source in (raw.get('sources') or {}).items()
        if isinstance(source, dict) and source.get('ok') is False
        and name not in STANDING_FAILED_SOURCES
        and (source.get('error') or {}).get('code') != 'IFS_CYCLONE_UNPUBLISHED')
    official_block = calculated.get('official_hazards') or {}
    official_summary = {}
    for kind, item in (official_block.get('regional') or {}).items():
        status = item.get('status') if isinstance(item, dict) else None
        official_summary[kind] = _item(
            'ok' if status == 'ready' else
            'caution' if status in ('caution', 'unassessed', 'stale') else 'missing',
            f"{status}: {item.get('reason', 'exact-area alerts and issue/valid times retained')}"
            if isinstance(item, dict) else 'missing source assessment')
    return {'schema': 'east_asia_weather_validation_v1',
            'generated_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'target_date_jst': calculated.get('target_date_jst'),
            'cities': cities,
            'hazards': {'jtwc_advisory': advisory, 'cyclone_verification': cyclone, 'space_weather': space_item,
                        'official_warnings': official_summary},
            'coverage': {'standing_limitations': STANDING_LIMITATIONS,
                         'new_source_failures': new_failures,
                         'quality_state': (raw.get('quality') or {}).get('state'),
                         'quality_reasons': (raw.get('quality') or {}).get('reasons')}}


def check_products(products, latest):
    """Mechanical product checks: presence, size, signatures, table shape."""
    out = {}
    for key in ('chart', 'chart_aifs'):
        entry = products.get(key) or {}
        path = latest / ('chart.png' if key == 'chart' else 'chart_aifs.png')
        status, detail = 'missing', 'product absent'
        if path.exists():
            blob = path.read_bytes()
            sig = blob[:8] == b'\x89PNG\r\n\x1a\n'
            status = 'ok' if (sig and len(blob) >= 5000) else 'invalid'
            detail = f'PNG signature {"ok" if sig else "BAD"}, {len(blob)} bytes'
        out[key] = _item(status, detail)
        svg = path.with_suffix('.svg')
        if svg.exists():
            head = svg.read_bytes()[:2000].decode('utf-8', 'replace')
            out[key + '_svg'] = _item('ok' if '<svg' in head else 'invalid', 'SVG root element present' if '<svg' in head else 'no <svg> root in first 2 KB')
        else:
            out[key + '_svg'] = _item('missing', 'SVG twin absent')
    for key in ('table1', 'table2', 'table3', 'table4'):
        path = latest / f'{key}.md'
        if not path.exists():
            out[key] = _item('missing', 'table file absent')
            continue
        text = path.read_text(encoding='utf-8')
        lines = [ln for ln in text.splitlines() if ln.strip()]
        pipe_rows = sum(1 for ln in lines if ln.startswith('|'))
        problems = []
        if '<br>' in text:
            problems.append('<br> present')
        if key in ('table1', 'table2'):
            # City-column tables: the fixed city header must be present.
            if pipe_rows < 3:
                problems.append(f'only {pipe_rows} pipe rows')
            if '香港' not in text:
                problems.append('city header 香港 absent')
        elif key == 'table3':
            # These are mechanical checks only: the explicit unverified line
            # is a valid shape, not a valid official cyclone assessment.
            if text.strip() not in ('熱帯低気圧：なし', '熱帯低気圧：JTWCの情報を確認できず'):
                if pipe_rows < 2:
                    problems.append(f'only {pipe_rows} pipe rows')
                if '名称' not in text:
                    problems.append('cyclone header 名称 absent')
        else:  # table4
            if pipe_rows < 3:
                problems.append(f'only {pipe_rows} pipe rows')
            if '項目' not in text:
                problems.append('outlook header 項目 absent')
        out[key] = _item('invalid' if problems else 'ok',
                         '; '.join(problems) if problems
                         else f'{pipe_rows} pipe rows; shape ok; no <br>')
    return out
