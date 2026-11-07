"""Original report inputs: ensemble snowfall, Japanese pollen, regional grids."""
import datetime as dt
import re
from zoneinfo import ZoneInfo

REGIONS = {
    'pearl_river_delta': ('Hong Kong / Pearl River Delta', (21, 24), (111, 116)),
    'taiwan': ('Taiwan', (21, 26), (119, 123)),
    'luzon_strait': ('Luzon Strait', (18, 23), (119, 125)),
    'tokaido': ('Tokaido and adjacent Pacific', (32, 36), (135, 141)),
    'ulaanbaatar': ('Ulaanbaatar / winter monsoon', (46, 50), (104, 110)),
    'manchuria': ('Manchuria', (39, 52), (119, 135)),
    'sea_of_japan': ('Sea of Japan', (34, 44), (129, 140)),
}
REGIONAL_FIELDS = ('temperature_2m', 'temperature_2m_spread', 'pressure_msl',
                   'pressure_msl_spread', 'wind_speed_10m', 'wind_gusts_10m',
                   'precipitation', 'snowfall')


def build_specialist_jobs(cities):
    jobs = {}
    for key, (_, lat, lon, _) in cities.items():
        jobs[f'{key}/snow_ensemble'] = ('https://ensemble-api.open-meteo.com/v1/ensemble', {
            'latitude': lat, 'longitude': lon, 'models': 'ecmwf_ifs025',
            'hourly': 'snowfall', 'forecast_days': 3, 'timezone': 'GMT'}, True)
    # Temperature members for the two chart cities: the 06:00 chart confidence
    # grades come from the ensemble spread of daily maximum temperature.
    for key in ('hong_kong', 'tokyo_minato'):
        _, lat, lon, _ = cities[key]
        jobs[f'{key}/temperature_ensemble'] = ('https://ensemble-api.open-meteo.com/v1/ensemble', {
            'latitude': lat, 'longitude': lon, 'models': 'ecmwf_ifs025',
            'hourly': 'temperature_2m', 'forecast_days': 16, 'timezone': 'GMT'}, True)
    for key, code in [('tokyo_minato', '13103'), ('takamatsu', '37201')]:
        jobs[f'{key}/pollen'] = (f'https://site.weathernews.jp/site/pollen/json/fcst/city_r5/{code}.json', None, False)
    jobs['pollen/publication_config'] = ('https://weathernews.jp/pollen/html_resources/city/bundle.min.js', None, False)
    for key, (_, lats, lons) in REGIONS.items():
        grid = [(lat, lon) for lat in (lats[0], sum(lats)/2, lats[1]) for lon in (lons[0], sum(lons)/2, lons[1])]
        jobs[f'regional/{key}'] = ('https://seasonal-api.open-meteo.com/v1/seasonal', {
            'latitude': ','.join(str(v[0]) for v in grid), 'longitude': ','.join(str(v[1]) for v in grid),
            'models': 'ecmwf_ec46_ensemble_mean', 'hourly': ','.join(REGIONAL_FIELDS),
            'forecast_days': 19, 'timezone': 'GMT', 'wind_speed_unit': 'ms', 'cell_selection': 'nearest'}, True)
    return jobs


def extract_snow(source, start, audit):
    raw = source.get('response')
    hourly = raw.get('hourly', {}) if source['ok'] and isinstance(raw, dict) else {}
    fields = [k for k in hourly if re.fullmatch(r'snowfall(?:_member\d+)?', k)]
    check = audit(hourly, fields, start+dt.timedelta(hours=1))
    check.update(member_count=len(fields), ready=check['ready'] and len(fields) == 51)
    return {'model': 'ecmwf_ifs025_ensemble', 'hourly': hourly,
            'units': raw.get('hourly_units', {}) if isinstance(raw, dict) else {}, 'check': check,
            'method': '06:00: fraction of all 51 members with >0 cm snow accumulated within the exact 24h window. Retain the threshold/denominator. Do not infer this from rain probability.'}


def extract_temperature_ensemble(source, start, end):
    """Keep the IFS ensemble temperature members for the chart cities.

    The 06:00 chart grades each day's confidence from the spread of the
    members' daily maximum temperature. Members are retained verbatim; the
    ensemble's native span (about 15 days) is recorded, never extended.
    """
    raw = source.get('response')
    hourly = raw.get('hourly', {}) if source['ok'] and isinstance(raw, dict) else {}
    fields = [k for k in hourly if re.fullmatch(r'temperature_2m(?:_member\d+)?', k)]
    times = []
    for text in hourly.get('time', []):
        try:
            times.append(dt.datetime.fromisoformat(str(text).replace('Z', '+00:00')))
        except ValueError:
            continue
    in_span = [t for t in times if start <= t < end]
    return {'model': 'ecmwf_ifs025_ensemble', 'hourly': hourly,
            'units': raw.get('hourly_units', {}) if isinstance(raw, dict) else {},
            'check': {'member_fields': len(fields), 'members_ready': len(fields) >= 40,
                      'first_utc': times[0].isoformat() if times else None,
                      'last_utc': times[-1].isoformat() if times else None,
                      'samples_in_chart_span': len(in_span)},
            'method': 'Per local date, take each member\'s daily maximum temperature; '
                      'confidence grade from the population standard deviation across members.'}


def extract_pollen(source, start, audit, config):
    raw = source.get('response')
    values = raw.get('srf_1h', {}) if source['ok'] and isinstance(raw, dict) else {}
    pairs = []
    for text, value in values.items():
        try:
            t = dt.datetime.strptime(text, '%Y%m%d_%H').replace(tzinfo=ZoneInfo('Asia/Tokyo'))
            pairs.append((t.astimezone(dt.timezone.utc).isoformat(), value))
        except ValueError:
            continue
    pairs.sort()
    hourly = {'time': [v[0] for v in pairs], 'pollen_category_code': [v[1] for v in pairs]}
    check = audit(hourly, ['pollen_category_code'], start)
    bundle = config.get('response') or ''
    closed = bool(re.search(r'CLOSE_MODE\s*:\s*!0', bundle)) if isinstance(bundle, str) else None
    return {'provider': 'Weathernews', 'hourly': hourly, 'numeric_coverage': check,
            'city_info': raw.get('city_info', {}) if isinstance(raw, dict) else {}, 'public_page_closed': closed,
            'status': 'retrieved_but_public_page_closed' if check['ready'] and closed else 'retrieved' if check['ready'] else 'not_retrieved',
            'applicability_verified': check['ready'] and not closed,
            'publication_note': 'Public site says the 2027 spring information is being prepared. Current backend arrays are retained; do not silently call these an operational all-pollen forecast.'}


def extract_region(key, source, start, end, parse, numeric):
    name, lats, lons = REGIONS[key]
    response = source.get('response')
    rows = response if source['ok'] and isinstance(response, list) else []
    checks = []
    for row in rows:
        h = row.get('hourly', {})
        try:
            times = [parse(t) for t in h.get('time', [])]
        except (ValueError, TypeError):
            times = []
        idx = [i for i, t in enumerate(times) if start-dt.timedelta(hours=6) <= t <= end+dt.timedelta(hours=6)]
        selected = [times[i] for i in idx]
        gaps = [(b-a).total_seconds()/3600 for a, b in zip(selected, selected[1:])]
        cover = bool(selected) and selected[0] <= start and selected[-1] >= end and all(0 < g <= 6 for g in gaps)
        fields = {v: len(h.get(v, [])) == len(times) and all(numeric(h[v][i]) for i in idx) for v in REGIONAL_FIELDS}
        checks.append({'latitude': row.get('latitude'), 'longitude': row.get('longitude'),
                       'time_coverage': cover, 'fields': fields, 'ready': cover and all(fields.values())})
    return {'name': name, 'bounds': {'latitude': lats, 'longitude': lons},
            'model': 'ecmwf_ec46_ensemble_mean_36km', 'sampling': '3x3 regional grid; native six-hour means and temperature/pressure spread',
            'limitations': 'Coarse guidance cannot resolve cyclone cores or establish precise 16-day event timing. Combine with official tracks, warnings and uncertainty.',
            'ready': len(rows) == 9 and all(c['ready'] for c in checks), 'checks': checks, 'grid_forecasts': rows}
