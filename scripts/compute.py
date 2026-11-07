#!/usr/bin/env python3
"""Deterministic numerical calculations, run by GitHub after collection."""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path

from collect import CITIES, iso_now, parse_time, numeric


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
            v.get('ready') is True for v in city.values() if 'status' not in v)
    return {'schema': 'east_asia_weather_calculated_v1',
            'target_date_jst': raw['target_date_jst'], 'window_start_utc': start.isoformat(),
            'window_end_utc': end.isoformat(), 'collection_utc': raw['collection_utc'],
            'collector_commit': raw.get('collector_commit'),
            'workflow_run_id': raw.get('workflow_run_id'),
            'calculation_commit': os.getenv('GITHUB_SHA', raw.get('collector_commit')),
            'calculated_at_utc': iso_now(), 'raw_quality': raw['quality'],
            'calculation_quality': {'ready': all(checks.values()), 'cities': checks,
                                    'note': 'Calculation success does not resolve missing original report requirements.'},
            'cities': cities}


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
