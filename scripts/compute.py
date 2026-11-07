#!/usr/bin/env python3
"""Deterministic numerical calculations, run by GitHub after collection."""
import argparse
from collections import Counter
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import statistics
from zoneinfo import ZoneInfo

from collect import CITIES, iso_now, parse_time, numeric


COMPASS16 = ('北', '北北東', '北東', '東北東', '東', '東南東', '南東', '南南東',
             '南', '南南西', '南西', '西南西', '西', '西北西', '北西', '北北西')


def compass16(degrees):
    if degrees is None:
        return None
    return COMPASS16[int(((degrees % 360) + 11.25) // 22.5) % 16]


def hazard_flags(pm2_5_max, pm10_max, surface_temperature_min):
    """Report hazard flags whose thresholds are fixed rules, not judgement.

    高PM: CAMS PM2.5 daily maximum >= 55 µg/m³ or PM10 >= 155 µg/m³.
    霜: model ground-surface (skin) temperature daily minimum <= 0 °C.
    """
    return {
        'high_pm': (pm2_5_max is not None and pm2_5_max >= 55) or
                   (pm10_max is not None and pm10_max >= 155),
        'frost': surface_temperature_min is not None and surface_temperature_min <= 0,
    }


def grade_from_spread(spread):
    """Chart confidence from the ensemble spread of daily maximum temperature."""
    if spread is None:
        return '低'
    if spread <= 1.2:
        return '高'
    if spread <= 2.2:
        return '中'
    return '低'


def chart_confidence_for_city(city, day0, days=16):
    """Per-date chart confidence grades from IFS ensemble temperature members.

    Returns {} when the city carries no temperature ensemble (older bundles),
    so callers can fall back to an explicit 'not evaluated' marker instead of
    inventing a grade. Dates beyond the ensemble's native span are graded 低
    with basis 'outside_ensemble_coverage'.
    """
    block = city.get('temperature_ensemble') or {}
    hourly = block.get('hourly') or {}
    fields = [k for k in hourly if re.fullmatch(r'temperature_2m(?:_member\d+)?', k)]
    times = hourly.get('time') or []
    if not fields or not times or any(len(hourly[k]) != len(times) for k in fields):
        return {}
    tz = ZoneInfo(city.get('timezone', 'UTC'))
    stamps = [parse_time(t).astimezone(tz) for t in times]
    result = {}
    for offset in range(days):
        day = day0 + dt.timedelta(days=offset)
        maxima = []
        for field in fields:
            values = [v for t, v in zip(stamps, hourly[field])
                      if t.date() == day and numeric(v)]
            if values:
                maxima.append(max(values))
        if len(maxima) >= 10:
            spread = statistics.pstdev(maxima)
            result[day.isoformat()] = {'grade': grade_from_spread(spread),
                                       'spread_c': round(spread, 2),
                                       'members': len(maxima), 'basis': 'ensemble_spread'}
        else:
            result[day.isoformat()] = {'grade': '低', 'spread_c': None,
                                       'members': len(maxima),
                                       'basis': 'outside_ensemble_coverage'}
    return result


def parse_jtwc_abpw(text):
    """Digest of the JTWC western North Pacific advisory (ABPW).

    Systems are parsed from the tropical-cyclone summary only; positions and
    winds are the advisory's own estimates at its stated valid time.
    """
    empty = {'systems': [], 'no_other_cyclones': False, 'disturbances_none': False,
             'subtropical_none': False, 'monsoon_mentions': []}
    if not isinstance(text, str) or not text:
        return empty
    segment = text.split('2. SOUTH PACIFIC AREA')[0]
    systems = []
    pattern = re.compile(
        r'(SUPER TYPHOON|TYPHOON|TROPICAL STORM|TROPICAL DEPRESSION)\s+(\d{2}[A-Z])'
        r'\s+\(([A-Z][A-Z\- ]*)\)\s+WAS LOCATED\s+NEAR\s+([\d.]+)([NS])\s+([\d.]+)([EW])')
    for match in pattern.finditer(segment):
        tail = segment[match.end():match.end() + 700]
        move = re.search(r'TRACKED\s+([A-Z\-]+)\s+AT\s+(\d+)\s+KNOTS', tail)
        winds = re.search(r'ESTIMATED AT\s+(\d+)\s+KNOTS GUSTING TO\s+(\d+)\s+KNOTS', tail)
        systems.append({
            'category': match.group(1), 'id': match.group(2), 'name': match.group(3),
            'latitude': float(match.group(4)) * (1 if match.group(5) == 'N' else -1),
            'longitude': float(match.group(6)) * (1 if match.group(7) == 'E' else -1),
            'movement_text': move.group(1) if move else None,
            'movement_kt': int(move.group(2)) if move else None,
            'max_winds_kt': int(winds.group(1)) if winds else None,
            'gusts_kt': int(winds.group(2)) if winds else None})
    return {'systems': systems,
            'no_other_cyclones': 'NO OTHER TROPICAL CYCLONES' in segment,
            'disturbances_none': bool(re.search(r'TROPICAL DISTURBANCE SUMMARY:\s*NONE', segment)),
            'subtropical_none': bool(re.search(r'SUBTROPICAL SYSTEM SUMMARY:\s*NONE', segment)),
            'monsoon_mentions': [line.strip() for line in segment.splitlines()
                                 if 'MONSOON' in line.upper()][:5]}


def select(hourly, field, start, end, accumulated=False):
    times = hourly.get('time', [])
    values = hourly.get(field, [])
    if len(times) != len(values):
        return []
    result = []
    for text, value in zip(times, values):
        t = parse_time(text)
        inside = start < t <= end if accumulated else start <= t < end
        if inside and numeric(value):
            result.append((t, float(value)))
    return sorted(result)


def value_range(points):
    if len(points) != 24:
        return {'ready': False, 'valid_slots': len(points)}
    values = [v for _, v in points]
    return {'ready': True, 'minimum': round(min(values), 3), 'maximum': round(max(values), 3),
            'time_of_minimum_utc': min(points, key=lambda p: p[1])[0].isoformat(),
            'time_of_maximum_utc': max(points, key=lambda p: p[1])[0].isoformat()}


def cosine_zenith(stamp, latitude, longitude):
    # NOAA fractional-year solar position approximation, evaluated in UTC.
    t = stamp.astimezone(dt.timezone.utc)
    hour = t.hour+t.minute/60+t.second/3600
    gamma = 2*math.pi/365*(t.timetuple().tm_yday-1+(hour-12)/24)
    decl = (.006918-.399912*math.cos(gamma)+.070257*math.sin(gamma)
            -.006758*math.cos(2*gamma)+.000907*math.sin(2*gamma)
            -.002697*math.cos(3*gamma)+.00148*math.sin(3*gamma))
    equation = 229.18*(.000075+.001868*math.cos(gamma)-.032077*math.sin(gamma)
                       -.014615*math.cos(2*gamma)-.040849*math.sin(2*gamma))
    angle = math.radians(((hour*60+equation+4*longitude) % 1440)/4-180)
    lat = math.radians(latitude)
    return max(-1, min(1, math.sin(lat)*math.sin(decl)+math.cos(lat)*math.cos(decl)*math.cos(angle)))


def wbgt(city, start, end):
    import numpy as np
    try:
        import thermofeel
        calculator = thermofeel.calculate_wbgt_liljegren
    except ImportError:
        # Offline runtimes can use the same unmodified, licensed ECMWF solver.
        from vendor import liljegren
        def calculator(t, rh, pressure, wind, solar, fraction, zenith):
            wind = np.maximum(wind, liljegren.MIN_WIND_10M)
            speed = liljegren.wind_speed_2m(wind, zenith, solar)
            return liljegren.wbgt(t, rh, pressure, speed, solar, fraction, zenith)
    h = city['hourly']
    names = ['temperature_2m', 'relative_humidity_2m', 'surface_pressure', 'wind_speed_10m',
             'shortwave_radiation_instant', 'direct_radiation_instant']
    arrays = [select(h, field, start, end) for field in names]
    if any(len(v) != 24 for v in arrays) or any([t for t, _ in v] != [t for t, _ in arrays[0]] for v in arrays):
        return {'ready': False, 'reason': 'Incomplete or misaligned instantaneous Liljegren inputs'}
    temp, rh, pressure, wind, solar, direct = [np.array([v for _, v in a]) for a in arrays]
    zenith = np.array([cosine_zenith(t, city['latitude'], city['longitude']) for t, _ in arrays[0]])
    fraction = np.divide(direct, solar, out=np.zeros_like(solar), where=solar > 0)
    values = calculator(temp+273.15, rh, pressure, wind, solar, fraction, zenith)-273.15
    valid = [(t, float(v)) for (t, _), v in zip(arrays[0], values) if np.isfinite(v)]
    result = value_range(valid)
    result.update(method='Liljegren 2008 via ECMWF thermofeel 2.2.0; instantaneous radiation; NOAA solar-position approximation',
                  units='°C', hourly=[{'time_utc': t.isoformat(), 'value': round(v, 3)} for t, v in valid])
    return result


def snow_probability(city, start, end):
    source = city.get('snow_probability_inputs', {})
    h = source.get('hourly', {})
    fields = [k for k in h if k == 'snowfall' or k.startswith('snowfall_member')]
    totals = []
    for name in fields:
        values = select(h, name, start, end, accumulated=True)
        if len(values) == 24:
            totals.append(sum(v for _, v in values))
    if len(totals) != 51:
        return {'ready': False, 'valid_members': len(totals), 'required_members': 51}
    return {'ready': True, 'percent': round(100*sum(v > 0 for v in totals)/51, 2),
            'members': 51, 'event': '>0 cm accumulated snowfall during the exact 24h window',
            'model': source.get('model'), 'method': 'ensemble member fraction, independently of rain probability'}


def compute_city(city, start, end):
    h = city['hourly']
    result = {v: value_range(select(h, v, start, end)) for v in (
        'temperature_2m', 'apparent_temperature', 'relative_humidity_2m', 'pressure_msl',
        'wind_speed_10m', 'visibility', 'surface_temperature', 'snow_depth')}
    for field in ('precipitation', 'rain', 'snowfall'):
        rows = select(h, field, start, end, accumulated=True)
        result[field] = {'ready': len(rows) == 24, 'total': round(sum(v for _, v in rows), 3) if len(rows) == 24 else None,
                         'interval_convention': '(start,end], because the source values cover the preceding hour'}
    result['precipitation_probability'] = value_range(select(h, 'precipitation_probability', start, end, accumulated=True))
    result['wind_gusts_10m'] = value_range(select(h, 'wind_gusts_10m', start, end, accumulated=True))
    current = dict(select(h, 'pressure_msl', start, end))
    previous = dict(select(h, 'pressure_msl', start-dt.timedelta(hours=24), start))
    changes = [(t, v-previous[t-dt.timedelta(hours=24)]) for t, v in current.items() if t-dt.timedelta(hours=24) in previous]
    result['pressure_change_24h'] = value_range(changes)
    wind = dict(select(h, 'wind_speed_10m', start, end))
    chill = []
    for t, temp in select(h, 'temperature_2m', start, end):
        kph = wind.get(t, 0)*3.6
        if temp <= 10 and kph > 4.8:
            chill.append((t, 13.12+.6215*temp-11.37*kph**.16+.3965*temp*kph**.16))
    result['wind_chill'] = {'status': 'applicable' if chill else 'not_applicable', 'units': '°C',
                            'minimum': round(min((v for _, v in chill), default=0), 2) if chill else None,
                            'maximum': round(max((v for _, v in chill), default=0), 2) if chill else None,
                            'applicable_hours': len(chill)}
    try:
        result['wbgt'] = wbgt(city, start, end)
    except Exception as exc:
        result['wbgt'] = {'ready': False, 'reason': f'{type(exc).__name__}: {exc}'}
    result['snow_probability'] = snow_probability(city, start, end)
    air = city['raw_air_quality']
    result['aqi'] = value_range(select(air, 'us_aqi', start, end))
    components = []
    for field in air:
        if field.startswith('us_aqi_'):
            values = select(air, field, start, end)
            if len(values) == 24:
                components.extend((value, field, t.isoformat()) for t, value in values)
    result['aqi']['leading_component_at_worst_hour'] = max(components) if components else None
    result['uv_index'] = value_range(select(air, 'uv_index', start, end))
    result['dust'] = value_range(select(air, 'dust', start, end))
    result['pm2_5'] = value_range(select(air, 'pm2_5', start, end))
    result['pm10'] = value_range(select(air, 'pm10', start, end))
    # Report-ready daily digest: the values the 06:00 tables print, computed
    # once here so the report never re-derives them from hourly arrays.
    codes = [int(v) for _, v in select(h, 'weather_code', start, end)]
    humidity = [v for _, v in select(h, 'relative_humidity_2m', start, end)]
    directions = dict(select(h, 'wind_direction_10m', start, end))
    max_wind_time = max(wind, key=wind.get) if wind else None
    wbgt_max = result['wbgt'].get('maximum') if result.get('wbgt', {}).get('ready') else None
    wbgt_band = None
    if wbgt_max is not None:
        wbgt_band = next((label for limit, label in
                          ((21, 'ほぼ安全'), (25, '注意'), (28, '警戒'), (31, '厳重警戒'))
                          if wbgt_max < limit), '危険')
    result['report_digest'] = {
        'dominant_weather_code': Counter(codes).most_common(1)[0][0] if codes else None,
        'humidity_mean': round(sum(humidity) / len(humidity), 1) if len(humidity) == 24 else None,
        'pressure_mean_hpa': round(sum(current.values()) / len(current), 1) if len(current) == 24 else None,
        'pressure_mean_change_24h': round(sum(v for _, v in changes) / len(changes), 1) if len(changes) == 24 else None,
        'wind_max_time_utc': max_wind_time.isoformat() if max_wind_time else None,
        'wind_direction_at_max_deg': directions.get(max_wind_time) if max_wind_time else None,
        'wind_direction_at_max_compass': compass16(directions.get(max_wind_time)) if max_wind_time else None,
        'wbgt_band': wbgt_band,
        'hazard_flags': hazard_flags(
            result['pm2_5'].get('maximum') if result['pm2_5'].get('ready') else None,
            result['pm10'].get('maximum') if result['pm10'].get('ready') else None,
            result['surface_temperature'].get('minimum') if result['surface_temperature'].get('ready') else None),
    }
    return result


def compute_bundle(raw):
    start, end = parse_time(raw['window_start_utc']), parse_time(raw['window_end_utc'])
    cities = {}
    for name in (v[0] for v in CITIES.values()):
        try:
            cities[name] = compute_city(raw['cities'][name], start, end)
        except Exception as exc:
            cities[name] = {'error': f'{type(exc).__name__}: {exc}'}
    checks = {}
    for name, city in cities.items():
        checks[name] = not city.get('error') and all(
            v.get('ready') is True for k, v in city.items()
            if 'status' not in v and k != 'report_digest')
    sources = raw.get('sources', {})
    ecmwf_probe = sources.get('ecmwf/cyclone_product', {})
    ecmwf_response = ecmwf_probe.get('response')
    if isinstance(ecmwf_response, str):
        ecmwf_response = ecmwf_response[:2000]
    warning_names = sorted(n for n in sources if n.startswith('jtwc/warning_'))
    cyclone_watch = {
        **parse_jtwc_abpw(sources.get('jtwc/outlook', {}).get('response')),
        'warning_sources': warning_names,
        'warnings_all_ok': all(sources[n].get('ok') for n in warning_names) if warning_names else None,
        'ecmwf_cyclone_product': {'ok': ecmwf_probe.get('ok'),
                                  'http_status': ecmwf_probe.get('http_status'),
                                  'fetched_at_utc': ecmwf_probe.get('fetched_at_utc'),
                                  'response': ecmwf_response},
        'note': 'ABPW digest covers the advisory valid time; forecast tracks are in the '
                'jtwc/warning_* source texts. The ECMWF entry is the OpenCharts cyclone '
                'product probe result, retained verbatim (data or the API\'s own '
                'availability error); it never silently replaces JTWC warnings.'}
    day0 = dt.date.fromisoformat(raw['target_date_jst'])
    chart_confidence = {}
    for key in ('hong_kong', 'tokyo_minato'):
        name = CITIES[key][0]
        if name in raw.get('cities', {}):
            chart_confidence[name] = chart_confidence_for_city(raw['cities'][name], day0)
    return {'schema': 'east_asia_weather_calculated_v1',
            'target_date_jst': raw['target_date_jst'], 'window_start_utc': start.isoformat(),
            'window_end_utc': end.isoformat(), 'collection_utc': raw['collection_utc'],
            'collector_commit': raw.get('collector_commit'),
            'workflow_run_id': raw.get('workflow_run_id'),
            'calculation_commit': os.getenv('GITHUB_SHA', raw.get('collector_commit')),
            'calculated_at_utc': iso_now(), 'raw_quality': raw['quality'],
            'calculation_quality': {'ready': all(checks.values()), 'cities': checks,
                                    'note': 'Calculation success does not resolve missing original report requirements.'},
            'cities': cities, 'cyclone_watch': cyclone_watch,
            'chart_confidence': chart_confidence}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.input.read_text())
    result = compute_bundle(raw)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps(result['calculation_quality'], ensure_ascii=False))


if __name__ == '__main__':
    main()
