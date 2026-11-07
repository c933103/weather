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
            'hourly': 'temperature_2m,precipitation,cloud_cover',
            'forecast_days': 16, 'timezone': 'GMT'}, True)
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


def _night_key(local):
    """The noon-to-noon night a local stamp belongs to, keyed by the date
    whose noon starts it (the chart's night-low slot for that date)."""
    return local.date() if local.hour >= 12 else local.date() - dt.timedelta(days=1)


def _member_daily(hourly, times, tz, with_rain=True):
    """Per-local-date member statistics shared by the ensemble extractors.

    Extrema are the MEAN OF THE MEMBERS' OWN daily extrema (the expected
    daily high / night low of the ensemble), never extrema of the ensemble
    mean series, which are biased at every lead. Rain probability is the
    share of members whose daily precipitation sum clears 1 mm; the dry-day
    icon grades on the members' mean daytime cloud cover.
    """
    def fields(prefix):
        return [k for k in hourly if re.fullmatch(rf'{prefix}(?:_member\d+)?', k)]

    tfields, pfields, cfields = fields('temperature_2m'), fields('precipitation'), fields('cloud_cover')
    if not tfields or not times:
        return {}
    locals_ = [t.astimezone(tz) if t else None for t in times]
    days, nights = {}, {}
    for i, local in enumerate(locals_):
        if local is None:
            continue
        days.setdefault(local.date(), []).append(i)
        nights.setdefault(_night_key(local), []).append(i)

    def member_extrema(flist, idx, fn):
        vals = []
        for f in flist:
            xs = [hourly[f][i] for i in idx if i < len(hourly[f]) and isinstance(hourly[f][i], (int, float))]
            if xs and len(xs) * 2 >= len(idx):
                vals.append(fn(xs))
        return vals

    out = {}
    all_days = sorted(set(days) | set(nights))
    for d in all_days:
        idx, nidx = days.get(d, []), nights.get(d, [])
        highs = member_extrema(tfields, idx, max) if idx else []
        lows = member_extrema(tfields, nidx, min) if nidx else []
        entry = {'members': len(highs) or len(lows) or None,
                 'temp_high_member_mean': round(sum(highs) / len(highs), 2) if highs else None,
                 'temp_nightlow_member_mean': round(sum(lows) / len(lows), 2) if lows else None,
                 'p_rain_1mm': None, 'daytime_cloud_mean': None}
        if with_rain and pfields and idx:
            sums = []
            for f in pfields:
                xs = [hourly[f][i] for i in idx if i < len(hourly[f]) and isinstance(hourly[f][i], (int, float))]
                if xs:
                    sums.append(sum(xs))
            if sums:
                entry['p_rain_1mm'] = round(sum(1 for v in sums if v >= 1.0) / len(sums), 3)
        if cfields and idx:
            clouds = []
            for f in cfields:
                xs = [hourly[f][i] for i in idx if i < len(hourly[f])
                      and isinstance(hourly[f][i], (int, float)) and 9 <= locals_[i].hour <= 18]
                if xs:
                    clouds.append(sum(xs) / len(xs))
            if clouds:
                entry['daytime_cloud_mean'] = round(sum(clouds) / len(clouds), 1)
        out[d.isoformat()] = entry
    return out


def extract_temperature_ensemble(source, start, end, tz_name=None):
    """Keep the IFS ensemble temperature members for the chart cities.

    The 06:00 chart grades each day's confidence from the spread of the
    members' daily maximum temperature. Members are retained verbatim; the
    ensemble's native span (about 15 days) is recorded, never extended.
    With tz_name, also returns per-date member statistics ('daily': member
    extrema means, rain probability, daytime cloud) for the chart's
    ensemble-sourced days.
    """
    raw = source.get('response')
    hourly = raw.get('hourly', {}) if source['ok'] and isinstance(raw, dict) else {}
    fields = [k for k in hourly if re.fullmatch(r'temperature_2m(?:_member\d+)?', k)]
    times = []
    for text in hourly.get('time', []):
        try:
            parsed = dt.datetime.fromisoformat(str(text).replace('Z', '+00:00'))
        except ValueError:
            continue
        # Open-Meteo returns naive ISO strings for a GMT request; attach UTC
        # so they compare cleanly with the aware report window.
        times.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc))
    in_span = [t for t in times if start <= t < end]
    daily = {}
    if tz_name:
        aligned = []
        for text in hourly.get('time', []):
            try:
                parsed = dt.datetime.fromisoformat(str(text).replace('Z', '+00:00'))
                aligned.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc))
            except ValueError:
                aligned.append(None)
        daily = _member_daily(hourly, aligned, ZoneInfo(tz_name))
    mean_time, mean_temp = [], []
    raw_times = hourly.get('time', [])
    for i, text in enumerate(raw_times):
        values = [hourly[f][i] for f in fields
                  if i < len(hourly[f]) and isinstance(hourly[f][i], (int, float))]
        if values:
            mean_time.append(text)
            mean_temp.append(round(sum(values) / len(values), 3))
    return {'model': 'ecmwf_ifs025_ensemble', 'hourly': hourly,
            'mean_hourly': {'time': mean_time, 'temperature_2m': mean_temp},
            'daily': daily,
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


def extract_ec46_daily(source, tz_name):
    """Per-day EC46 member probabilities for the chart's tail icons.

    The EC46 ensemble MEAN's weather_code is not a categorical forecast: the
    mean state is almost never completely dry, so codes derived from it read
    as rain nearly every day (verified on the 2026-11-07 bundle: 16/16 EC46
    days rain-coded in both chart cities). The defensible daily signal from
    an ensemble is member probability — the share of members whose daily
    precipitation / snowfall clears a threshold — plus the members' mean
    daytime cloud cover for dry days. Precipitation stamps are 6-hour
    accumulations; a stamp is counted toward its local date (one interval of
    boundary bleed, acceptable at probability thresholds).
    Returns {iso_date: {members, p_rain_1mm, p_rain_10mm, p_snow,
    daytime_cloud_mean}} in the city's local time.
    """
    raw = source.get('response')
    hourly = raw.get('hourly', {}) if source.get('ok') and isinstance(raw, dict) else {}
    times = hourly.get('time', [])

    def member_fields(prefix):
        return [k for k in hourly if re.fullmatch(rf'{prefix}_member\d+', k)]

    precip = member_fields('precipitation')
    snow = member_fields('snowfall')
    cloud = member_fields('cloud_cover')
    if not times or not precip:
        return {}
    tz = ZoneInfo(tz_name)
    stamps = []
    for text in times:
        parsed = dt.datetime.fromisoformat(str(text).replace('Z', '+00:00'))
        stamps.append((parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)).astimezone(tz))
    days = {}
    for i, local in enumerate(stamps):
        days.setdefault(local.date(), []).append(i)

    def daily_sums(fields, idx):
        sums = []
        for f in fields:
            vals = [hourly[f][i] for i in idx
                    if i < len(hourly[f]) and isinstance(hourly[f][i], (int, float))]
            if vals:
                sums.append(sum(vals))
        return sums

    out = {}
    for day, idx in days.items():
        ps = daily_sums(precip, idx)
        ss = daily_sums(snow, idx)
        clouds = []
        for f in cloud:
            vals = [hourly[f][i] for i in idx
                    if i < len(hourly[f]) and isinstance(hourly[f][i], (int, float))
                    and 9 <= stamps[i].hour <= 18]
            if vals:
                clouds.append(sum(vals) / len(vals))
        out[day.isoformat()] = {
            'members': len(ps),
            'p_rain_1mm': round(sum(1 for v in ps if v >= 1.0) / len(ps), 3) if ps else None,
            'p_rain_10mm': round(sum(1 for v in ps if v >= 10.0) / len(ps), 3) if ps else None,
            'p_snow': round(sum(1 for v in ss if v >= 0.5) / len(ss), 3) if ss else None,
            'daytime_cloud_mean': round(sum(clouds) / len(clouds), 1) if clouds else None}
    # Member temperature extrema for the same days: the plotted EC46 high /
    # night low is the mean of the members' own extrema, replacing extrema
    # of the ensemble-mean series (and its interval min/max fields).
    utc_stamps = [s.astimezone(dt.timezone.utc) for s in stamps]
    for ds, entry in _member_daily(hourly, utc_stamps, tz, with_rain=False).items():
        if ds in out:
            out[ds]['temp_members'] = entry.get('members')
            out[ds]['temp_high_member_mean'] = entry.get('temp_high_member_mean')
            out[ds]['temp_nightlow_member_mean'] = entry.get('temp_nightlow_member_mean')
    return out
