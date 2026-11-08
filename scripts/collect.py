#!/usr/bin/env python3
"""Collection phase of the GitHub 05:00 JST collection/compute workflow.

Forecast file decoders are specified in requirements-collection.txt. Numerical
coverage, transport success and full report completeness are different states.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
JST = ZoneInfo('Asia/Tokyo')
CITIES = {
    'hong_kong': ('Hong Kong', 22.29, 114.17, 'Asia/Hong_Kong'),
    'taipei': ('Taipei', 25.033, 121.565, 'Asia/Taipei'),
    'tokyo_minato': ('Tokyo Minato Ward', 35.658, 139.745, 'Asia/Tokyo'),
    'takamatsu': ('Takamatsu', 34.343, 134.046, 'Asia/Tokyo'),
}
CORE_FIELDS = (
    'temperature_2m,relative_humidity_2m,dew_point_2m,apparent_temperature,'
    'precipitation_probability,precipitation,rain,snowfall,snow_depth,'
    'wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl,'
    'surface_pressure,visibility,cloud_cover_low,surface_temperature,'
    'shortwave_radiation,direct_radiation,diffuse_radiation,weather_code,'
    'shortwave_radiation_instant,direct_radiation_instant,diffuse_radiation_instant'
).split(',')
AIR_FIELDS = (
    'us_aqi,us_aqi_pm2_5,us_aqi_pm10,us_aqi_ozone,us_aqi_nitrogen_dioxide,'
    'us_aqi_sulphur_dioxide,us_aqi_carbon_monoxide,pm2_5,pm10,ozone,'
    'nitrogen_dioxide,sulphur_dioxide,carbon_monoxide,dust,uv_index'
).split(',')
GFS_FIELDS = (
    'temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,'
    'wind_direction_10m,pressure_msl,soil_temperature_0_to_10cm,weather_code'
).split(',')


def iso_now():
    return dt.datetime.now(UTC).isoformat()


def parse_time(value):
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, UTC)
    result = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (result.replace(tzinfo=UTC) if result.tzinfo is None else result).astimezone(UTC)


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def report_date(now):
    local = now.astimezone(JST)
    return local.date() + dt.timedelta(days=int(local.hour >= 6))


def audit_hourly(hourly, fields, start, hours=24):
    """Require exactly the expected hours, with no duplicates, NaNs or nulls."""
    values = hourly or {}
    raw_times = values.get('time', [])
    try:
        timestamps = [parse_time(v) for v in raw_times]
    except (ValueError, TypeError, OverflowError):
        return {'ready': False, 'error': 'INVALID_TIMESTAMPS', 'variables': {}}
    expected = [start + dt.timedelta(hours=h) for h in range(hours)]
    duplicate = len(timestamps) != len(set(timestamps))
    index = {v: i for i, v in enumerate(timestamps)}
    slots = [index.get(v) for v in expected]
    status = {'ready': not duplicate and all(i is not None for i in slots),
              'expected_slots': hours, 'timestamp_slots': sum(i is not None for i in slots),
              'duplicate_timestamps': duplicate, 'variables': {}}
    for field in fields:
        array = values.get(field, [])
        valid = sum(i is not None and i < len(array) and numeric(array[i]) for i in slots)
        ok = len(array) == len(timestamps) and valid == hours and not duplicate
        status['variables'][field] = {'valid_slots': valid, 'expected_slots': hours, 'ready': ok}
        status['ready'] = status['ready'] and ok
    return status


def request(url, params=None, retries=2, method='GET'):
    if params and method == 'GET':
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    records = []
    for attempt in range(retries + 1):
        result = {'url': url, 'method': method, 'fetched_at_utc': iso_now(), 'ok': False,
                  'http_status': None, 'response': None, 'error': None,
                  'issued_at_utc': None, 'model_initialized_at_utc': None}
        retry_after = None
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode(params or {}).encode() if method == 'POST' else None, headers={
                'User-Agent': 'EastAsiaWeatherDaily/3.0 (github.com/c933103/weather)',
                'Accept': 'application/json,text/plain,text/html;q=0.8,*/*;q=0.5',
                **({'X-Requested-With': 'XMLHttpRequest', 'Referer': 'https://ocean.cwa.gov.tw/V2/sailing_forecast'} if method == 'POST' and url.startswith('https://ocean.cwa.gov.tw/') else {}),
            })
            with urllib.request.urlopen(req, timeout=25) as response:
                body = response.read(12_000_000)
                result['http_status'] = response.status
                result['response_content_type'] = response.headers.get('Content-Type')
                result['last_modified_header'] = response.headers.get('Last-Modified')
                text = body.decode('utf-8-sig', errors='replace')
                try:
                    result['response'] = json.loads(text)
                except json.JSONDecodeError:
                    result['response'] = text
                result['ok'] = True
        except urllib.error.HTTPError as exc:
            result['http_status'] = exc.code
            text = exc.read(3000).decode('utf-8', errors='replace')
            try:
                result['response'] = json.loads(text)
            except json.JSONDecodeError:
                result['response'] = text
            result['error'] = {'code': 'HTTP_ERROR', 'message': str(exc)}
            retry_after = exc.headers.get('Retry-After')
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            result['error'] = {'code': 'NETWORK_ERROR', 'message': str(exc)[:500]}
        records.append({k: v for k, v in result.items() if k != 'response'})
        if result['ok'] or result['http_status'] not in (None, 429, 500, 502, 503, 504):
            break
        if attempt < retries:
            # Bounded provider backoff; never spend the entire collection window waiting.
            try:
                delay = min(20, max(1, int(retry_after))) if retry_after else 2 ** attempt
            except ValueError:
                delay = 2 ** attempt
            time.sleep(delay)
    result['attempts'] = records
    return result


def numerical(url, params):
    """Split rejected requests instead of discarding all accepted variables."""
    result = request(url, params)
    if result['ok'] or result['http_status'] != 400 or 'hourly' not in params:
        return result
    fields = params['hourly'].split(',')
    if len(fields) < 2:
        return result
    left = numerical(url, {**params, 'hourly': ','.join(fields[:len(fields)//2])})
    right = numerical(url, {**params, 'hourly': ','.join(fields[len(fields)//2:])})
    accepted = [r for r in (left, right) if r['ok'] and isinstance(r['response'], dict)]
    if not accepted:
        result['attempts'] += left['attempts'] + right['attempts']
        return result
    merged = dict(accepted[0]['response'])
    merged['hourly'] = dict(merged.get('hourly', {}))
    merged['hourly_units'] = dict(merged.get('hourly_units', {}))
    for part in accepted[1:]:
        data = part['response']
        if data.get('hourly', {}).get('time') != merged['hourly'].get('time'):
            continue
        merged['hourly'].update(data.get('hourly', {}))
        merged['hourly_units'].update(data.get('hourly_units', {}))
    result.update(ok=True, response=merged, partial_request=True)
    result['attempts'] += left['attempts'] + right['attempts']
    result['rejected_fields'] = [v for v in fields if v not in merged['hourly']]
    return result


def forecast_result(source, model, lat, lon):
    data = source.get('response')
    if not source['ok'] or not isinstance(data, dict) or 'hourly' not in data:
        return {'model': model, 'time': [], 'units': {}, 'source_error': source.get('error')}
    returned = [data.get('latitude'), data.get('longitude')]
    if not all(numeric(v) for v in returned) or abs(returned[0]-lat) > .7 or abs(returned[1]-lon) > .7:
        return {'model': model, 'time': [], 'units': {}, 'source_error': 'COORDINATE_MISMATCH'}
    return {**data['hourly'], 'units': data.get('hourly_units', {}),
            'model': model, 'returned_grid_cell': returned,
            'model_initialized_at_utc': None,
            'initialization_note': 'Provider response does not identify the initialization cycle; generationtime_ms is not an issue time.'}


def _daily_extrema(hourly, tz_name):
    """Per-local-date min/max of an hourly temperature series (samples only)."""
    zone = ZoneInfo(tz_name)
    ts = (hourly or {}).get('time') or []
    temps = (hourly or {}).get('temperature_2m') or []
    out = {}
    for t, v in zip(ts, temps):
        if not numeric(v):
            continue
        day = parse_time(t).astimezone(zone).date().isoformat()
        cur = out.setdefault(day, {'high': v, 'low': v})
        cur['high'] = max(cur['high'], v)
        cur['low'] = min(cur['low'], v)
    return out


def combined_temperature(ifs, extended, ens_mean=None,
                         first_marker='ecmwf_ifs_9km',
                         ens_marker='ecmwf_ifs025_ensemble_mean',
                         ext_marker='ecmwf_ec46_ensemble_mean_36km',
                         tail_note=None):
    """Reliability cascade for the 28-day chart series.

    Most reliable source first: ECMWF IFS 9km deterministic while it lasts,
    then the ECMWF IFS ensemble mean, then the EC46 ensemble mean — each tier
    contributes only samples after the previous tier's last timestamp. Every
    sample retains its model marker; no extrapolation, bias correction or
    fabricated tail. The chart labels this cascade beneath the axes from the
    sample_model markers. The AIFS chart reuses this with its own markers:
    the AIFS ensemble mean first, then the same EC46 tail.
    """
    tiers = [(ifs, first_marker)]
    if ens_mean:
        tiers.append((ens_mean, ens_marker))
    tiers.append((extended, ext_marker))
    samples = {}
    used = []
    for source, marker in tiers:
        ts = source.get('time', [])
        temp = source.get('temperature_2m', [])
        weather = source.get('weather_code', [])
        tmins = source.get('temperature_2m_min', [])
        tmaxs = source.get('temperature_2m_max', [])
        if len(ts) != len(temp):
            continue
        cutoff = max(samples) if samples else None
        added = False
        for i, (t, value) in enumerate(zip(ts, temp)):
            stamp = parse_time(t)
            if not numeric(value) or (cutoff is not None and stamp <= cutoff):
                continue
            samples[stamp] = (value, weather[i] if i < len(weather) else None, marker,
                              tmins[i] if i < len(tmins) and numeric(tmins[i]) else None,
                              tmaxs[i] if i < len(tmaxs) and numeric(tmaxs[i]) else None)
            added = True
        if added:
            used.append(marker)
    ordered = sorted(samples)
    return {'time': [t.isoformat() for t in ordered],
            'temperature_2m': [samples[t][0] for t in ordered],
            'weather_code': [samples[t][1] for t in ordered],
            'sample_model': [samples[t][2] for t in ordered],
            'temperature_2m_min': [samples[t][3] for t in ordered],
            'temperature_2m_max': [samples[t][4] for t in ordered],
            'model': '+'.join(used) if used else first_marker,
            'units': {'temperature_2m': '°C', 'weather_code': 'wmo code'},
            'tail_note': tail_note or (
                         'Cascade in reliability order: IFS 9km deterministic, then IFS '
                         'ensemble mean, then EC46 ensemble mean (36km, six-hour). Each '
                         'sample keeps its source model; nothing is extrapolated.')}


_WARNING_POS = re.compile(
    r'(?:WARNING POSITION:|(\d+)\s*HRS, VALID AT:)\s*(\d{2})(\d{2})(\d{2})Z\s*---\s*(?:NEAR\s+)?'
    r'(\d+\.\d)([NS])\s+(\d+\.\d)([EW])')


def parse_warning_track(text, now):
    """JTWC warning positions: the analysis point plus forecast points.

    Each entry carries its TAU and its valid time; the DDHHMM stamps are
    resolved to the month nearest the collection time.
    """
    if not isinstance(text, str):
        return []
    track = []
    for match in _WARNING_POS.finditer(text):
        tau = int(match.group(1)) if match.group(1) else 0
        day, hour, minute = int(match.group(2)), int(match.group(3)), int(match.group(4))
        lat = float(match.group(5)) * (1 if match.group(6) == 'N' else -1)
        lon = float(match.group(7)) * (1 if match.group(8) == 'E' else -1)
        best = None
        for delta in (-1, 0, 1):
            year = now.year + (now.month - 1 + delta) // 12
            month = (now.month - 1 + delta) % 12 + 1
            try:
                valid = dt.datetime(year, month, day, hour, minute, tzinfo=UTC)
            except ValueError:
                continue
            if best is None or abs(valid - now) < abs(best - now):
                best = valid
        if best is not None:
            track.append({'tau_h': tau, 'valid_utc': best.isoformat(),
                          'latitude': lat, 'longitude': lon})
    return track


def _nearest_hour_value(hourly, field, valid):
    best = None
    for stamp_text, value in zip(hourly.get('time', []), hourly.get(field, [])):
        if not isinstance(value, (int, float)):
            continue
        try:
            stamp = parse_time(stamp_text)
        except (TypeError, ValueError):
            continue
        gap = abs(stamp - valid)
        if best is None or gap < best[0]:
            best = (gap, value)
    return best[1] if best and best[0] <= dt.timedelta(hours=2) else None


def gfs_track_check(track):
    """Independent-model check: GFS MSLP/10 m wind along the JTWC track.

    This is the standing alternative when the ECMWF cyclone product cannot
    be obtained: a second model's low-level field sampled at the warning
    positions and valid times. It is a coherence check, not a track.
    """
    points = [p for p in track if p['tau_h'] in (0, 24, 48, 72)]
    if not points:
        return {'ok': False, 'http_status': None, 'response': None,
                'error': {'code': 'NO_TRACK_POINTS', 'message': 'warning text carried no usable positions'}}
    params = {'latitude': ','.join(str(p['latitude']) for p in points),
              'longitude': ','.join(str(p['longitude']) for p in points),
              'hourly': 'pressure_msl,wind_speed_10m', 'models': 'gfs_seamless',
              'forecast_days': 5, 'past_days': 1, 'timezone': 'GMT', 'wind_speed_unit': 'ms'}
    url = 'https://api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode(params)
    source = request(url)
    if not source['ok'] or not isinstance(source.get('response'), list):
        return {'ok': False, 'http_status': source.get('http_status'), 'response': None,
                'fetched_at_utc': iso_now(), 'url': url,
                'error': source.get('error') or {'code': 'GFS_CHECK_FAILED', 'message': 'no per-location response'}}
    samples = []
    for point, location in zip(points, source['response']):
        hourly = location.get('hourly', {}) if isinstance(location, dict) else {}
        valid = parse_time(point['valid_utc'])
        pressure = _nearest_hour_value(hourly, 'pressure_msl', valid)
        wind = _nearest_hour_value(hourly, 'wind_speed_10m', valid)
        samples.append({**point, 'gfs_pressure_msl_hpa': pressure,
                        'gfs_wind_10m_kt': round(wind * 1.94384, 1) if wind is not None else None})
    return {'ok': any(s['gfs_pressure_msl_hpa'] is not None for s in samples),
            'http_status': source.get('http_status'), 'fetched_at_utc': iso_now(), 'url': url,
            'response': {'model': 'noaa_gfs_seamless', 'samples': samples,
                         'method': 'GFS hourly MSLP and 10 m wind sampled at the JTWC warning-track '
                                   'positions and valid times; an independent-model coherence '
                                   'check, not a track forecast.'},
            'error': None}


def _range_get(url, offset, length):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (weather collector)',
        'Range': f'bytes={offset}-{offset + length - 1}'})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


def _grib_nearest(data, lat, lon):
    """Nearest-gridpoint value from one GRIB2 message (repo eccodes idiom)."""
    import eccodes as ec
    with tempfile.NamedTemporaryFile(suffix='.grib2', delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        with open(tmp.name, 'rb') as stream:
            handle = ec.codes_grib_new_from_file(stream)
            if handle is None:
                return None
            try:
                found = ec.codes_grib_find_nearest(handle, lat, lon, npoints=1)
                return found[0]['value'] if found else None
            finally:
                ec.codes_release(handle)


def ecmwf_opendata_track_check(track, now):
    """Alternative ECMWF route inside the workflow: ECMWF open data.

    When the OpenCharts cyclone product cannot be obtained, sample the IFS
    open-data (0.25°) MSLP and 10 m wind at the JTWC track points via the
    per-step .index byte ranges — the same eccodes decoding the coastal
    STOFS limb uses. Failure degrades to a recorded failed source.
    """
    points = [p for p in track if p['tau_h'] in (0, 24, 48, 72)]
    if not points:
        return {'ok': False, 'http_status': None, 'response': None,
                'error': {'code': 'NO_TRACK_POINTS', 'message': 'warning text carried no usable positions'}}
    run = (now - dt.timedelta(hours=6)).replace(minute=0, second=0, microsecond=0)
    run = run.replace(hour=(run.hour // 12) * 12)
    base = f'https://data.ecmwf.int/forecasts/{run:%Y%m%d}/{run:%H}z/ifs/0p25/oper/'
    index_cache = {}
    samples = []
    for point in points:
        valid = parse_time(point['valid_utc'])
        step = max(0, int(round((valid - run).total_seconds() / 10800.0)) * 3)
        stamp = f'{run:%Y%m%d%H}0000-{step}h-oper-fc.grib2'
        if stamp not in index_cache:
            entries = {}
            try:
                req = urllib.request.Request(base + stamp.removesuffix('.grib2') + '.index',
                                             headers={'User-Agent': 'Mozilla/5.0 (weather collector)'})
                with urllib.request.urlopen(req, timeout=30) as response:
                    text = response.read().decode('utf-8', 'replace')
                for line in text.splitlines():
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and entry.get('param'):
                        entries.setdefault(entry['param'], entry)
            except Exception:
                entries = {}
            index_cache[stamp] = entries
        entries = index_cache[stamp]
        sample = {**point, 'run_utc': run.isoformat(), 'step_h': step,
                  'ecmwf_pressure_msl_hpa': None, 'ecmwf_wind_10m_kt': None}
        try:
            msl = entries.get('msl')
            if msl:
                value = _grib_nearest(_range_get(base + stamp, msl['_offset'], msl['_length']),
                                      point['latitude'], point['longitude'])
                if value is not None:
                    sample['ecmwf_pressure_msl_hpa'] = round(value / 100.0, 1)
            u_entry, v_entry = entries.get('10u'), entries.get('10v')
            if u_entry and v_entry:
                u = _grib_nearest(_range_get(base + stamp, u_entry['_offset'], u_entry['_length']),
                                  point['latitude'], point['longitude'])
                v = _grib_nearest(_range_get(base + stamp, v_entry['_offset'], v_entry['_length']),
                                  point['latitude'], point['longitude'])
                if u is not None and v is not None:
                    sample['ecmwf_wind_10m_kt'] = round(math.hypot(u, v) * 1.94384, 1)
        except Exception:
            pass
        samples.append(sample)
    ok = any(s['ecmwf_pressure_msl_hpa'] is not None for s in samples)
    return {'ok': ok, 'http_status': 200 if ok else None, 'fetched_at_utc': iso_now(),
            'url': base,
            'response': {'model': 'ecmwf_ifs_open_data_0p25', 'samples': samples,
                         'method': 'ECMWF open-data IFS (0.25°) MSLP and 10 m wind sampled at the '
                                   'JTWC warning-track positions via per-step index byte ranges; '
                                   'the in-workflow alternative when the OpenCharts cyclone '
                                   'product is unavailable.'} if samples else None,
            'error': None if ok else {'code': 'OPEN_DATA_UNAVAILABLE',
                                      'message': 'no MSLP sample could be decoded from ECMWF open data'}}


def audit_long(hourly, start, end):
    points = [(parse_time(t), v) for t, v in zip(hourly.get('time', []), hourly.get('temperature_2m', [])) if numeric(v)]
    points = sorted((t, v) for t, v in points if start-dt.timedelta(hours=6) <= t <= end+dt.timedelta(hours=6))
    gaps = [(b[0]-a[0]).total_seconds()/3600 for a, b in zip(points, points[1:])]
    ready = bool(points) and points[0][0] <= start and points[-1][0] >= end and all(0 < g <= 6 for g in gaps)
    return {'ready': ready, 'required_start_utc': start.isoformat(), 'required_end_utc': end.isoformat(),
            'first_utc': points[0][0].isoformat() if points else None,
            'last_utc': points[-1][0].isoformat() if points else None,
            'max_gap_hours': max(gaps, default=None), 'model': hourly.get('model'),
            'native_ec46_tail': True}


class TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = []
        elif tag == 'br' and self.cell is not None:
            self.cell.append(' ')

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            if self.row is not None:
                self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def jma_tides(source, start, end, station):
    if not source['ok'] or not isinstance(source['response'], str):
        return {'ready': False, 'error': 'TIDE_REQUEST_FAILED', 'station': station}
    parser = TableRows()
    parser.feed(source['response'])
    selected = []
    for row in parser.rows:
        if any(re.search(r'\d{4}/\d\d/\d\d', cell) for cell in row):
            selected.append(row)
    return {'ready': bool(selected), 'station': station, 'datum': 'JMA tide-table datum (DL)',
            'units': 'cm', 'timezone': 'Asia/Tokyo', 'rows': selected,
            'required_start_utc': start.isoformat(), 'required_end_utc': end.isoformat(),
            'note': 'Official rows retained verbatim; do not mix this datum with mean sea level.'}


def hko_tides(source, year, start, end):
    data = source.get('response')
    points = []
    if source['ok'] and isinstance(data, dict):
        for row in data.get('data', []):
            if len(row) < 26:
                continue
            day = dt.datetime(year, int(row[0]), int(row[1]), tzinfo=ZoneInfo('Asia/Hong_Kong'))
            for hour, value in enumerate(row[2:26], 1):
                t = (day + dt.timedelta(hours=hour)).astimezone(UTC)
                try:
                    height = float(value)
                except (ValueError, TypeError):
                    continue
                if start <= t < end:
                    points.append((t, height))
    points.sort()
    return {'station': 'Quarry Bay', 'datum': 'Hong Kong Chart Datum', 'units': 'm',
            'time': [t.isoformat() for t, _ in points], 'height': [v for _, v in points],
            'ready': len(points) == 24, 'astronomical_only': True}


def source_freshness(source, now, max_age_hours=36):
    data = source.get('response')
    issued = None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        stamps = [(row.get('reportDatetime'), row) for row in data if row.get('reportDatetime')]
        if stamps:
            source['per_product_issue_times'] = [{'product': row.get('dataTypeCode'), 'issued_at': stamp} for stamp, row in stamps]
            data = max(stamps, key=lambda pair: parse_time(pair[0]))[1]
        else:
            data = data[0]
    if isinstance(data, dict):
        for key in ('reportDatetime', 'updateTime', 'issueTime'):
            if data.get(key):
                try:
                    issued = parse_time(data[key])
                except (ValueError, TypeError):
                    pass
                break
    elif isinstance(data, str):
        match = re.search(r':Issued:\s*(\d{4} \w{3} \d{1,2} \d{4}) UTC', data)
        if match:
            issued = dt.datetime.strptime(match[1], '%Y %b %d %H%M').replace(tzinfo=UTC)
        if issued is None:
            match = re.search(r"['\"]issued['\"]\s*:\s*['\"](\d{4}/\d\d/\d\d \d\d:\d\d)", data)
            if match:
                issued = dt.datetime.strptime(match[1], '%Y/%m/%d %H:%M').replace(tzinfo=ZoneInfo('Asia/Taipei')).astimezone(UTC)
        if issued is None and data.lstrip().startswith('<?xml'):
            try:
                tree = ET.fromstring(data)
                stamp = tree.findtext('.//{*}ReportDateTime') or tree.findtext('./{*}updated')
                if stamp:
                    issued = parse_time(stamp)
            except (ValueError, ET.ParseError):
                pass
    source['issued_at_utc'] = issued.isoformat() if issued else None
    source['freshness'] = {'status': 'unverified', 'age_hours': None}
    if issued:
        age = (now-issued).total_seconds()/3600
        source['freshness'] = {'status': 'current' if -1 <= age <= max_age_hours else 'stale', 'age_hours': round(age, 2)}
    return source


def jtwc_system_ids(text):
    """Active western North Pacific system ids (e.g. '31W') named in ABPW.

    Invest areas (90W-99W) are included here; the caller skips warning files
    for them because JTWC issues numbered warnings only for tropical cyclones.
    """
    if not isinstance(text, str):
        return []
    ids = re.findall(r'\b(\d{2}[WE])\s*\([A-Z][A-Z\- ]*\)', text)
    ids += re.findall(r'(?:TYPHOON|TROPICAL STORM|TROPICAL DEPRESSION)\s+(\d{2}[WE])\b', text)
    seen = []
    for value in ids:
        value = value.upper()
        if value not in seen:
            seen.append(value)
    return seen


def warning_feed_entries(text):
    try:
        tree = ET.fromstring(text)
    except (ET.ParseError, TypeError):
        return []
    entries = []
    for item in tree.findall('./{*}entry'):
        title = item.findtext('./{*}title', '')
        content = item.findtext('./{*}content', '')
        if '警報' not in title or not any(v in content for v in ('東京都', '香川県')):
            continue
        for link in item.findall('./{*}link'):
            url = link.attrib.get('href', '')
            if re.fullmatch(r'https://www\.data\.jma\.go\.jp/developer/xml/data/[A-Za-z0-9_]+\.xml', url):
                entries.append({'title': title, 'content': content,
                                'updated': item.findtext('./{*}updated'), 'url': url})
    return entries


def compact_official(sources):
    """Keep report evidence small; complete pages remain in the raw artifact."""
    result = {}
    for name, source in sources.items():
        if name.split('/')[0] in CITIES or name.startswith('regional/') or name == 'pollen/publication_config':
            continue
        normalized = dict(source)
        if name in ('jma/dust', 'cwa/tide_page', 'cwa/tamsui_tides'):
            normalized['response'] = None
            normalized['response_note'] = 'Full page retained in the raw artifact. Parsed CWA tide events are in taipei.json. JMA map HTML alone is not dust forecast data.'
        elif name == 'jma/warning_feed':
            normalized['response'] = {'entries': warning_feed_entries(source.get('response'))[:30]}
        elif name in ('jma/tokyo_timeline', 'jma/kagawa_timeline') and isinstance(source.get('response'), dict):
            data = dict(source['response'])
            city_code = '1310300' if 'tokyo' in name else '3720100'
            region_code = '130010' if 'tokyo' in name else '370000'
            series = []
            for original in data.get('timeSeries', []):
                row = dict(original)
                for key in ('class10Items', 'class15Items', 'class20Items'):
                    if key in row:
                        row[key] = [v for v in row[key] if v.get('areaCode') in (city_code, region_code)]
                series.append(row)
            data['timeSeries'] = series
            normalized['response'] = data
        elif name.endswith('_warning_xml') and isinstance(source.get('response'), str):
            try:
                tree = ET.fromstring(source['response'])
                codes = ('1310300', '130010', '130000') if '/tokyo_' in name else ('3720100', '370010', '370000')
                normalized['response'] = {
                    'head_xml': ET.tostring(tree.find('./{*}Head'), encoding='unicode'),
                    'applicable_items_xml': [ET.tostring(item, encoding='unicode') for item in tree.findall('.//{*}Item')
                                             if item.findtext('./{*}Area/{*}Code') in codes],
                    'note': 'Full original XML retained in the raw artifact; city/region warning items retained here.',
                }
            except (ET.ParseError, TypeError):
                normalized['response_note'] = 'XML normalization failed; use the raw artifact.'
        result[name] = normalized
    return result


def jma_hourly_tides(source, year, start, end, station):
    points = []
    events = []
    text = source.get('response')
    if source['ok'] and isinstance(text, str):
        for line in text.splitlines():
            if len(line) < 136:
                continue
            try:
                if int(line[72:74]) != year % 100:
                    continue
                day = dt.datetime(year, int(line[74:76]), int(line[76:78]), tzinfo=JST)
                if day.astimezone(UTC) >= end or (day+dt.timedelta(days=1)).astimezone(UTC) <= start:
                    continue
                for hour in range(24):
                    t = (day+dt.timedelta(hours=hour)).astimezone(UTC)
                    height = int(line[hour*3:hour*3+3])
                    if start <= t < end and height != 999:
                        points.append((t, height))
                for kind, offset in [('high', 80), ('low', 108)]:
                    for number in range(4):
                        pos = offset+number*7
                        clock = line[pos:pos+4]
                        if clock == '9999':
                            continue
                        t = (day+dt.timedelta(hours=int(clock[:2]), minutes=int(clock[2:]))).astimezone(UTC)
                        if start <= t < end:
                            events.append({'time': t.isoformat(), 'kind': kind, 'height': int(line[pos+4:pos+7])})
            except (ValueError, IndexError):
                continue
    points.sort(); events.sort(key=lambda v: v['time'])
    return {'station': station, 'datum': 'JMA tide-table datum (DL)', 'units': 'cm',
            'time': [t.isoformat() for t, _ in points], 'height': [v for _, v in points],
            'events': events, 'ready': len(points) == 24 and bool(events), 'astronomical_only': True}


def cwa_tides(source, start, end):
    parser = TableRows()
    if source['ok'] and isinstance(source.get('response'), str):
        parser.feed(source['response'])
    day = None
    events = []
    for row in parser.rows:
        if not row:
            continue
        match = re.match(r'^(\d\d)/(\d\d)', row[0])
        if match:
            candidates = []
            for year in (start.year-1, start.year, start.year+1):
                try:
                    candidates.append(dt.datetime(year, int(match[1]), int(match[2]), tzinfo=ZoneInfo('Asia/Taipei')))
                except ValueError:
                    pass
            if not candidates:
                continue
            day = min(candidates, key=lambda t: abs((t-start).total_seconds()))
            row = row[1:]
        if day is None or len(row) != 5 or not re.fullmatch(r'\d\d:\d\d', row[1]):
            continue
        try:
            hour, minute = map(int, row[1].split(':'))
            t = (day+dt.timedelta(hours=hour, minutes=minute)).astimezone(UTC)
            if start <= t < end:
                events.append({'time': t.isoformat(), 'kind': 'high' if row[0] == '滿潮' else 'low' if row[0] == '乾潮' else 'unknown',
                               'height_twvd': int(row[2]), 'height_local_msl': int(row[3]), 'height_chart_datum': int(row[4])})
        except ValueError:
            continue
    events.sort(key=lambda v: v['time'])
    return {'station': 'Tamsui (coastal reference for Taipei)', 'units': 'cm',
            'datums': ['TWVD', 'local mean sea level', 'chart datum'], 'events': events,
            'ready': len(events) >= 2 and {v['kind'] for v in events} == {'high', 'low'},
            'hourly_ready': False, 'note': 'Public CWA product provides exact high/low events; no hourly astronomical series has been validated.'}


def build_jobs(day):
    from specialists import build_specialist_jobs
    jobs = {}
    for key, (_, lat, lon, _) in CITIES.items():
        coord = {'latitude': lat, 'longitude': lon, 'timezone': 'GMT'}
        jobs[f'{key}/ifs'] = ('https://api.open-meteo.com/v1/forecast', {
            **coord, 'models': 'ecmwf_ifs', 'hourly': ','.join(CORE_FIELDS),
            'forecast_days': 3, 'past_days': 2, 'wind_speed_unit': 'ms', 'precipitation_unit': 'mm'}, True)
        jobs[f'{key}/air'] = ('https://air-quality-api.open-meteo.com/v1/air-quality', {
            **coord, 'domains': 'cams_global', 'hourly': ','.join(AIR_FIELDS), 'forecast_days': 3}, True)
        # NOAA GFS as the independent second model for every city: cross-check
        # for the ECMWF IFS values and the frost limb (soil temperature).
        jobs[f'{key}/gfs'] = ('https://api.open-meteo.com/v1/forecast', {
            **coord, 'models': 'gfs_seamless', 'hourly': ','.join(GFS_FIELDS),
            'forecast_days': 3, 'past_days': 2, 'wind_speed_unit': 'ms', 'precipitation_unit': 'mm'}, True)
        if key in ('hong_kong', 'tokyo_minato'):
            jobs[f'{key}/ifs_long'] = ('https://api.open-meteo.com/v1/forecast', {
                **coord, 'models': 'ecmwf_ifs', 'hourly': 'temperature_2m,weather_code',
                'forecast_days': 16, 'past_days': 1}, True)
            jobs[f'{key}/ec46'] = ('https://seasonal-api.open-meteo.com/v1/seasonal', {
                **coord, 'models': 'ecmwf_ec46_ensemble_mean',
                'hourly': 'temperature_2m,weather_code,temperature_2m_min,temperature_2m_max',
                'forecast_days': 31}, True)
            # Individual EC46 members for the tail icons: the ensemble mean's
            # weather_code is not a categorical forecast, so daily icons past
            # the IFS tiers come from member probabilities instead.
            jobs[f'{key}/ec46_members'] = ('https://seasonal-api.open-meteo.com/v1/seasonal', {
                **coord, 'models': 'ecmwf_ec46',
                'hourly': 'precipitation,cloud_cover,snowfall,temperature_2m',
                'forecast_days': 31}, True)
        # Provisional grids are raw evidence, never validated urban flood forecasts.
        jobs[f'{key}/glofas_unvalidated'] = ('https://flood-api.open-meteo.com/v1/flood', {
            'latitude': lat, 'longitude': lon, 'daily': 'river_discharge,river_discharge_mean,river_discharge_max,river_discharge_p25,river_discharge_p75',
            'forecast_days': 3}, False)
    # Use actual coastal coordinates for sea-level reference; not inland Taipei.
    for key, lat, lon in [('hong_kong', 22.291, 114.214), ('taipei', 25.180, 121.410),
                          ('tokyo_minato', 35.650, 139.770), ('takamatsu', 34.365, 134.050)]:
        jobs[f'{key}/marine_reference'] = ('https://marine-api.open-meteo.com/v1/marine', {
            'latitude': lat, 'longitude': lon, 'hourly': 'sea_level_height_msl,wave_height,wave_period',
            'forecast_days': 3, 'timezone': 'GMT'}, True)
    fixed = {
        'hko/forecast': 'https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=fnd&lang=en',
        'hko/warnings': 'https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=warningInfo&lang=en',
        'hko/warning_summary': 'https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=warnsum&lang=en',
        'hko/local_forecast': 'https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=flw&lang=en',
        'jma/tokyo_forecast': 'https://www.jma.go.jp/bosai/forecast/data/forecast/130000.json',
        'jma/kagawa_forecast': 'https://www.jma.go.jp/bosai/forecast/data/forecast/370000.json',
        'jma/tokyo_warnings': 'https://www.jma.go.jp/bosai/warning/data/r8/130000.json',
        'jma/kagawa_warnings': 'https://www.jma.go.jp/bosai/warning/data/r8/370000.json',
        'jma/tokyo_risk': 'https://www.jma.go.jp/bosai/probability/data/probability/r8/130000.json',
        'jma/kagawa_risk': 'https://www.jma.go.jp/bosai/probability/data/probability/r8/370000.json',
        'jma/tokyo_timeline': 'https://www.jma.go.jp/bosai/warning_timeline/data/130000.json',
        'jma/kagawa_timeline': 'https://www.jma.go.jp/bosai/warning_timeline/data/370000.json',
        'jma/river_flood_warnings': 'https://www.jma.go.jp/bosai/flood/data/r8/flood_xml.json',
        'wra/inundation_public_index': 'https://opendata.wra.gov.tw/cloud/5982FloodWarningOfDisasterPreventionInformation/286-%E9%98%B2%E7%81%BD%E8%B3%87%E8%A8%8A_%E6%B7%B9%E6%B0%B4%E8%AD%A6%E6%88%92.kml',
        'wra/inundation_warning': 'https://fhy.wra.gov.tw/pub_web_2011/kml/KmlShare/NewstFloodWarm.kml',
        'hko/flood_information': 'https://www.hko.gov.hk/en/wservice/warning/flood.htm',
        'cwa/warnings': 'https://www.cwa.gov.tw/Data/js/warn/Warning_Content.js',
        'cwa/tide_index': 'https://www.cwa.gov.tw/Data/js/fcst/Tide_Time_Data.js',
        'cwa/tide_page': 'https://www.cwa.gov.tw/V8/C/M/tide.html',
        'cwa/tamsui_tides': 'https://www.cwa.gov.tw/V8/C/M/Fishery/tide_30day_MOD/T500010.html',
        'jma/dust': 'https://www.jma.go.jp/bosai/map.html#contents=kosa',
        'jma/warning_feed': 'https://www.data.jma.go.jp/developer/xml/feed/extra.xml',
        'nict/report_latest': 'https://swc.nict.go.jp/data/api/report-ja-daily-latest.json',
        'swpc/alerts': 'https://services.swpc.noaa.gov/products/alerts.json',
        'swpc/three_day': 'https://services.swpc.noaa.gov/text/3-day-forecast.txt',
        'swpc/geomag': 'https://services.swpc.noaa.gov/text/3-day-geomag-forecast.txt',
        'swpc/outlook_27_day': 'https://services.swpc.noaa.gov/text/27-day-outlook.txt',
        'jtwc/outlook': 'https://www.metoc.navy.mil/jtwc/products/abpwweb.txt',
        'gtsm/blueearth_status': 'https://blueearthdata.org/',
    }
    jobs.update({name: (url, None, False) for name, url in fixed.items()})
    jobs['cwa/tamsui_hourly_tide'] = ('https://ocean.cwa.gov.tw/V2/map_common/get_station_info', {
        'end_time': dt.datetime.combine(day, dt.time(5)).strftime('%Y-%m-%d %H:%M:%S'),
        'station_id': '1102', 'kind': 'tide'}, 'POST')
    start = day
    stop = day + dt.timedelta(days=1)
    for key, station in [('tokyo_minato', 'TK'), ('takamatsu', 'TA')]:
        for year in {start.year, stop.year}:
            jobs[f'{key}/hourly_tide_{year}'] = (f'https://www.data.jma.go.jp/kaiyou/data/db/tide/suisan/txt/{year}/{station}.txt', None, False)
        jobs[f'{key}/official_tide'] = ('https://www.data.jma.go.jp/kaiyou/db/tide/suisan/suisan.php', {
            'stn': station, 'ys': start.year, 'ms': start.month, 'ds': start.day,
            'ye': stop.year, 'me': stop.month, 'de': stop.day, 'S_HILO': 'on', 'S_TIDE': 'on', 'LV': 'DL'}, False)
    for offset in (0, 1):
        date = day+dt.timedelta(days=offset)
        jobs[f'hko/tide_{offset}'] = ('https://data.weather.gov.hk/weatherAPI/opendata/opendata.php', {
            'dataType': 'HHOT', 'station': 'QUB', 'year': date.year,
            'month': date.month, 'day': date.day, 'rformat': 'json'}, False)
        jobs[f'hko/tide_extremes_{offset}'] = ('https://data.weather.gov.hk/weatherAPI/opendata/opendata.php', {
            'dataType': 'HLT', 'station': 'QUB', 'year': date.year,
            'month': date.month, 'day': date.day, 'rformat': 'json'}, False)
    token = os.getenv('CWA_API_KEY')
    if token:
        # This URL is redacted before serialization, including failed attempts.
        jobs['cwa/tide_api'] = ('https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-A0021-001',
                                {'Authorization': token, 'format': 'JSON', 'LocationName': '新北市淡水區'}, False)
        # Taipei's local-observatory forecast (CWA county forecast, one week).
        jobs['cwa/forecast'] = ('https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001',
                                {'Authorization': token, 'format': 'JSON', 'locationName': '臺北市'}, False)
    jobs.update(build_specialist_jobs(CITIES))
    return jobs


def redact_secrets(value):
    if isinstance(value, dict):
        return {k: redact_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    if isinstance(value, str):
        for name in ('CWA_API_KEY',):
            token = os.getenv(name)
            if token:
                value = value.replace(token, '[REDACTED]').replace(urllib.parse.quote_plus(token), '[REDACTED]')
        value = re.sub(r'(Authorization=)[^&\s]+', r'\1[REDACTED]', value, flags=re.I)
    return value


def collect(day, out):
    from specialists import extract_snow, extract_pollen, extract_region, extract_temperature_ensemble, extract_ec46_daily, REGIONS
    from coastal import collect_coastal
    now = dt.datetime.now(UTC)
    start = dt.datetime.combine(day, dt.time(6), JST).astimezone(UTC)
    end = start+dt.timedelta(hours=24)
    long_end = start+dt.timedelta(hours=384)
    chart_end = start+dt.timedelta(days=28)
    root = {'schema': 'east_asia_weather_raw_v3', 'phase': 'collection',
            'target_date_jst': day.isoformat(), 'collection_utc': now.isoformat(),
            'window_start_utc': start.isoformat(), 'window_end_utc': end.isoformat(),
            'long_end_utc': long_end.isoformat(), 'chart_end_utc': chart_end.isoformat(), 'cities': {}, 'sources': {},
            'collector_commit': os.getenv('GITHUB_SHA'), 'workflow_run_id': os.getenv('GITHUB_RUN_ID'),
            'quality': {'ready': False, 'core_24h_ready': False, 'air_24h_ready': False,
                        'chart_28d_ready': False, 'reasons': []}}
    jobs = build_jobs(day)
    with tempfile.TemporaryDirectory(prefix='weather-coastal-') as coastal_cache, futures.ThreadPoolExecutor(max_workers=8) as pool:
        coastal_future = pool.submit(collect_coastal, start, end, now, coastal_cache)
        pending = {pool.submit((lambda u, p: request(u, p, method='POST')) if numeric_job == 'POST' else numerical if numeric_job else request, url, params): name
                   for name, (url, params, numeric_job) in jobs.items()}
        for job in futures.as_completed(pending):
            name = pending[job]
            try:
                source = job.result()
            except Exception as exc:
                source = {'ok': False, 'http_status': None, 'response': None,
                          'url': jobs[name][0], 'fetched_at_utc': iso_now(),
                          'error': {'code': 'ADAPTER_EXCEPTION', 'message': str(exc)[:300]}}
            root['sources'][name] = source_freshness(source, now, 24*35 if '27_day' in name else 36)
            print(json.dumps({'source': name, 'http': source.get('http_status'),
                              'ok': source['ok'], 'freshness': source.get('freshness')}), flush=True)
        try:
            coastal = coastal_future.result()
        except Exception as exc:
            coastal = {'ready': False, 'cities': {}, 'error': {'code': 'COASTAL_ADAPTER_ERROR', 'message': str(exc)}}
        root['sources']['noaa/coastal'] = {'ok': coastal.get('ready', False), 'http_status': None,
            'fetched_at_utc': iso_now(), 'url': 'https://noaa-gestofs-pds.s3.amazonaws.com/', 'response': coastal,
            'model_initialized_at_utc': coastal.get('model_initialized_at_utc'), 'error': coastal.get('error')}
    # A live official Atom feed is an independent fallback for stale bosai JSON.
    entries = warning_feed_entries(root['sources']['jma/warning_feed'].get('response'))
    for region, label in [('tokyo', '東京都'), ('kagawa', '香川県')]:
        match = next((item for item in entries if label in item['content'] and 'VPWW53' in item['url']), None)
        if match:
            root['sources'][f'jma/{region}_warning_xml'] = source_freshness(request(match['url']), now)
    # NICT's actual space-weather bulletin is its daily report, not the
    # centre's homepage (whose served HTML carries no parseable forecast
    # text — verified 2026-11-07). The site's JSON API names the latest
    # report and the URL of its text body; fetch that body as a second source.
    nict_latest = root['sources'].get('nict/report_latest', {}).get('response')
    nict_body_url = None
    if isinstance(nict_latest, dict):
        nict_items = nict_latest.get('items') or []
        if nict_items and isinstance(nict_items[0], dict):
            nict_body_url = nict_items[0].get('body')
    if nict_body_url:
        nict_body_url = nict_body_url.replace('http://', 'https://', 1)
        try:
            root['sources']['nict/report_body'] = source_freshness(request(nict_body_url), now)
        except Exception as exc:
            root['sources']['nict/report_body'] = {'ok': False, 'http_status': None,
                'fetched_at_utc': iso_now(), 'url': nict_body_url, 'response': None,
                'error': {'code': 'ADAPTER_EXCEPTION', 'message': str(exc)[:300]}}
    # JTWC numbered warnings for every active cyclone ABPW names (the advisory
    # alone carries no forecast track), plus a machine-readable probe of the
    # ECMWF OpenCharts cyclone product so the 06:00 ECMWF tropical-cyclone
    # check is an explicit recorded step, never a silent omission. ECMWF's API
    # answers with its own availability/error JSON; whatever it returns is
    # retained verbatim as the probe result.
    abpw_text = root['sources']['jtwc/outlook'].get('response')
    for system in jtwc_system_ids(abpw_text):
        number, basin = system[:2], system[2]
        if basin != 'W' or int(number) >= 90:
            continue
        url = f'https://www.metoc.navy.mil/jtwc/products/wp{number}{now.year % 100:02d}web.txt'
        try:
            root['sources'][f'jtwc/warning_{system.lower()}'] = source_freshness(request(url), now)
        except Exception as exc:
            root['sources'][f'jtwc/warning_{system.lower()}'] = {'ok': False, 'http_status': None,
                'fetched_at_utc': iso_now(), 'url': url, 'response': None,
                'error': {'code': 'ADAPTER_EXCEPTION', 'message': str(exc)[:300]}}
        # Independent-model cyclone checks along this system's warning track:
        # GFS (the standing second model) and ECMWF open data (the alternative
        # ECMWF route) — a failed product is always answered by another route.
        track = parse_warning_track(root['sources'][f'jtwc/warning_{system.lower()}'].get('response'), now)
        if track:
            for label, check_fn in (('gfs', lambda: gfs_track_check(track)),
                                    ('ecmwf_opendata', lambda: ecmwf_opendata_track_check(track, now))):
                key = f'{label}/cyclone_check_{system.lower()}'
                try:
                    root['sources'][key] = source_freshness(check_fn(), now)
                except Exception as exc:
                    root['sources'][key] = {'ok': False, 'http_status': None,
                        'fetched_at_utc': iso_now(), 'url': None, 'response': None,
                        'error': {'code': 'ADAPTER_EXCEPTION', 'message': str(exc)[:300]}}
    # ECMWF OpenCharts cyclone product: walk back through recent base times —
    # the newest cycle is not always published yet — and retain the first
    # success, or every attempt when none succeeds.
    attempts = []
    probe = None
    seen_bases = set()
    for hours_back in (0, 6, 12, 18, 24, 30):
        base = (now - dt.timedelta(hours=hours_back)).replace(minute=0, second=0, microsecond=0)
        base = base.replace(hour=(base.hour // 6) * 6)
        base_text = base.strftime('%Y-%m-%dT%H:00:00Z')
        if base_text in seen_bases:
            continue
        seen_bases.add(base_text)
        ecmwf_url = ('https://charts.ecmwf.int/opencharts-api/v1/products/cyclone/?base_time='
                     + urllib.parse.quote(base_text, safe=''))
        try:
            candidate = request(ecmwf_url)
        except Exception as exc:
            candidate = {'ok': False, 'http_status': None, 'response': None,
                         'error': {'code': 'ADAPTER_EXCEPTION', 'message': str(exc)[:300]}}
        attempts.append({'base_time': base_text, 'ok': candidate['ok'],
                         'http_status': candidate.get('http_status')})
        probe = {**candidate, 'response': {'base_time_used': base_text if candidate['ok'] else None,
                                           'attempts': attempts,
                                           'product': candidate.get('response')}}
        if candidate['ok']:
            break
    root['sources']['ecmwf/cyclone_product'] = source_freshness(probe, now)
    core_checks = []
    air_checks = []
    gfs_checks = []
    chart_checks = []
    for key, (name, lat, lon, zone) in CITIES.items():
        hr = forecast_result(root['sources'][f'{key}/ifs'], 'ecmwf_ifs_9km', lat, lon)
        air = forecast_result(root['sources'][f'{key}/air'], 'cams_global', lat, lon)
        gfs = forecast_result(root['sources'][f'{key}/gfs'], 'noaa_gfs_seamless', lat, lon)
        check = audit_hourly(hr, CORE_FIELDS, start)
        air_check = audit_hourly(air, AIR_FIELDS, start)
        gfs_check = audit_hourly(gfs, GFS_FIELDS, start)
        previous_pressure = audit_hourly(hr, ['pressure_msl'], start-dt.timedelta(hours=24))
        interval_check = audit_hourly(hr, ['precipitation', 'rain', 'snowfall', 'wind_gusts_10m', 'precipitation_probability'], start+dt.timedelta(hours=1))
        city = {'key': key, 'latitude': lat, 'longitude': lon, 'timezone': zone,
                'hourly': hr, 'raw_air_quality': air, 'gfs': gfs,
                'checks': {'core_24h': check, 'air_24h': air_check, 'gfs_24h': gfs_check, 'previous_day_pressure': previous_pressure, 'preceding_hour_intervals': interval_check}}
        city['coastal_forecast'] = {**{k: v for k, v in coastal.items() if k not in ('cities', 'downloads')}, **coastal.get('cities', {}).get(key, {'ready': False})}
        city['snow_probability_inputs'] = extract_snow(root['sources'][f'{key}/snow_ensemble'], start, audit_hourly)
        if key in ('hong_kong', 'tokyo_minato'):
            city['temperature_ensemble'] = extract_temperature_ensemble(
                root['sources'][f'{key}/temperature_ensemble'], start, long_end, zone)
        if key in ('tokyo_minato', 'takamatsu'):
            city['pollen'] = extract_pollen(root['sources'][f'{key}/pollen'], start, audit_hourly, root['sources']['pollen/publication_config'])
        core_checks.append(check['ready'] and previous_pressure['ready'] and interval_check['ready'])
        air_checks.append(air_check['ready'])
        gfs_checks.append(gfs_check['ready'])
        for field, status in check.get('variables', {}).items():
            if not status['ready']:
                root['quality']['reasons'].append(f'{name}: {field} has {status["valid_slots"]}/24 values')
        if key in ('hong_kong', 'tokyo_minato'):
            extended = forecast_result(root['sources'][f'{key}/ec46'], 'ecmwf_ec46_ensemble_mean_36km', lat, lon)
            city['extended_hourly'] = extended
            long_ifs = forecast_result(root['sources'][f'{key}/ifs_long'], 'ecmwf_ifs_9km', lat, lon)
            ens_mean = city.get('temperature_ensemble', {}).get('mean_hourly')
            city['chart_hourly'] = combined_temperature(long_ifs, extended, ens_mean)
            city['ec46_daily'] = extract_ec46_daily(root['sources'][f'{key}/ec46_members'], zone)
            # Member-extrema overrides for the chart: on ensemble-sourced days
            # the plotted high / night low is the mean of the members' own
            # daily extrema, never an extremum of the ensemble-mean series
            # (biased at every lead: ~0.2 °C on IFS ENS, worse further out).
            mext = {}
            for ds, v in ((city.get('temperature_ensemble') or {}).get('daily') or {}).items():
                if v.get('temp_high_member_mean') is not None or v.get('temp_nightlow_member_mean') is not None:
                    mext[ds] = {'high': v.get('temp_high_member_mean'),
                                'night_low': v.get('temp_nightlow_member_mean'),
                                'source': 'ifs_ens', 'members': v.get('members')}
            for ds, v in (city.get('ec46_daily') or {}).items():
                if v.get('temp_high_member_mean') is not None or v.get('temp_nightlow_member_mean') is not None:
                    mext[ds] = {'high': v.get('temp_high_member_mean'),
                                'night_low': v.get('temp_nightlow_member_mean'),
                                'source': 'ec46', 'members': v.get('temp_members')}
            city['member_extrema'] = mext
            city['checks']['chart_28d'] = audit_long(city['chart_hourly'], start, chart_end)
            # AIFS variant of the chart (user, 2026-11-08): the AIFS ensemble
            # alone — days beyond the AIFS horizon stay blank on that chart;
            # no other model fills them (user correction, 2026-11-08: the
            # EC46 tail is NOT part of the AIFS chart). AIFS Single is fetched
            # alongside and kept as a daily-extrema record for the
            # cross-check; at 0.25° and six-hourly sampling it offers no
            # advantage over its own ENS for the plotted extrema.
            city['aifs_ensemble'] = extract_temperature_ensemble(
                root['sources'][f'{key}/aifs_ensemble'], start, long_end, zone,
                model='ecmwf_aifs025_ensemble')
            aifs_mean = city['aifs_ensemble'].get('mean_hourly')
            city['aifs_chart_hourly'] = combined_temperature(
                aifs_mean, {}, None,
                first_marker='ecmwf_aifs025_ensemble_mean',
                tail_note='AIFS chart series: ECMWF AIFS ensemble mean '
                          '(0.25 degrees) only; days beyond the AIFS horizon '
                          'are left blank by design.')
            aifs_single = forecast_result(root['sources'][f'{key}/aifs_single'],
                                          'ecmwf_aifs025', lat, lon)
            city['aifs_single_daily'] = _daily_extrema(aifs_single, zone)
            aext = {}
            for ds, v in ((city.get('aifs_ensemble') or {}).get('daily') or {}).items():
                if v.get('temp_high_member_mean') is not None or v.get('temp_nightlow_member_mean') is not None:
                    aext[ds] = {'high': v.get('temp_high_member_mean'),
                                'night_low': v.get('temp_nightlow_member_mean'),
                                'source': 'aifs_ens', 'members': v.get('members')}
            city['aifs_member_extrema'] = aext
            ens_times = ((city['aifs_ensemble'].get('hourly') or {}).get('time') or [])
            aifs_end = parse_time(ens_times[-1]) if ens_times else start
            city['checks']['aifs_chart_span'] = audit_long(
                city['aifs_chart_hourly'], start, aifs_end)
            chart_checks.append(city['checks']['chart_28d']['ready'])
        if key in ('tokyo_minato', 'takamatsu'):
            parts = [jma_hourly_tides(root['sources'][f'{key}/hourly_tide_{year}'], year, start, end,
                     'Tokyo (Harumi)' if key == 'tokyo_minato' else 'Takamatsu') for year in {day.year, (day+dt.timedelta(days=1)).year}]
            city['astronomical_tide'] = {**parts[0], 'time': sum([v['time'] for v in parts], []),
                                        'height': sum([v['height'] for v in parts], []),
                                        'events': sum([v['events'] for v in parts], [])}
            city['astronomical_tide']['ready'] = len(city['astronomical_tide']['time']) == 24 and bool(city['astronomical_tide']['events'])
        elif key == 'hong_kong':
            parts = [hko_tides(root['sources'][f'hko/tide_{offset}'], (day+dt.timedelta(days=offset)).year, start, end) for offset in (0, 1)]
            city['astronomical_tide'] = {**parts[0], 'time': sum([v['time'] for v in parts], []),
                                        'height': sum([v['height'] for v in parts], [])}
            city['astronomical_tide']['ready'] = len(city['astronomical_tide']['time']) == 24
            city['astronomical_tide']['official_extrema_rows'] = [root['sources'][f'hko/tide_extremes_{offset}'].get('response') for offset in (0, 1)]
        else:
            city['astronomical_tide'] = cwa_tides(root['sources']['cwa/tamsui_tides'], start, end)
            station = root['sources']['cwa/tamsui_hourly_tide'].get('response')
            rows = station.get('chart_data', []) if isinstance(station, dict) else []
            hourly = []
            for row in rows:
                try:
                    text = str(row.get('datetime'))
                    stamp = parse_time(text if re.search(r'(Z|[+-]\d\d:\d\d)$', text) else text+'+08:00')
                except ValueError:
                    continue
                if start <= stamp < end and numeric(row.get('astron_tide')):
                    hourly.append((stamp.isoformat(), row['astron_tide']))
            city['astronomical_tide']['hourly_reference'] = {
                'station': 'CWA Danshuei tide station 1102', 'latitude': 25.17569, 'longitude': 121.42483,
                'units': 'm', 'datum': 'CWA station reference; retain separately from town-level chart/TWVD/MSL event heights',
                'time': [v[0] for v in hourly], 'height': [v[1] for v in hourly], 'ready': len(hourly) == 24}
            city['local_coastal_forecast'] = {'provider': 'CWA', 'station_id': '1102', 'chart_data': rows,
                'note': 'astron_tide is astronomical tide; surge is the forecast coastal water level, not itself a residual. Preserve the common station reference.'}
        root['cities'][name] = city
    q = root['quality']
    q['core_24h_ready'] = all(core_checks)
    q['air_24h_ready'] = all(air_checks)
    q['gfs_24h_ready'] = all(gfs_checks)
    q['chart_28d_ready'] = len(chart_checks) == 2 and all(chart_checks)
    q['official_tides_ready'] = all(c['astronomical_tide']['ready'] for c in root['cities'].values())
    q['all_city_hourly_tides_ready'] = all(len(c['astronomical_tide'].get('time', [])) == 24 or c['astronomical_tide'].get('hourly_reference', {}).get('ready') is True for c in root['cities'].values())
    q['solar_3day_ready'] = root['sources']['swpc/three_day']['ok'] and root['sources']['swpc/three_day']['freshness']['status'] == 'current'
    q['coastal_surge_24h_ready'] = coastal.get('ready', False)
    q['snow_probability_inputs_ready'] = all(c['snow_probability_inputs']['check']['ready'] for c in root['cities'].values())
    q['pollen_numeric_coverage_ready'] = all(root['cities'][CITIES[k][0]]['pollen']['numeric_coverage']['ready'] for k in ('tokyo_minato', 'takamatsu'))
    root['regional_forecasts'] = {key: extract_region(key, root['sources'][f'regional/{key}'], start, long_end, parse_time, numeric) for key in REGIONS}
    q['regional_384h_sample_inputs_ready'] = all(v['ready'] for v in root['regional_forecasts'].values())
    q['chart_confidence_inputs_ready'] = all(
        root['cities'][CITIES[k][0]]['temperature_ensemble']['check']['members_ready']
        for k in ('hong_kong', 'tokyo_minato'))
    warning_sources = [s for n, s in root['sources'].items() if n.startswith('jtwc/warning_')]
    q['jtwc_warnings_ready'] = all(s['ok'] for s in warning_sources)
    q['reasons'] += [
        'GloFAS grids have not been matched to applicable river reaches and flood thresholds.',
        'Urban inundation warning/risk feeds still need independent city-specific validation; rainfall and GloFAS point discharge alone do not establish inundation depth.',
        'Weathernews pollen numeric arrays were retrieved, but the public pages are closed for spring-season preparation; operational applicability remains unverified.',
        'Regional 3x3 sample grids provide actual 384h mean/spread guidance; official cyclone tracks and warnings must be combined with the grid at 06:00. The grid alone is not complete spatial cyclone coverage.',
    ]
    for name, source in root['sources'].items():
        if not source['ok']:
            q['reasons'].append(f'{name}: request failed ({source.get("http_status")})')
        elif source.get('freshness', {}).get('status') == 'stale':
            q['reasons'].append(f'{name}: stale official issue timestamp; not current warning evidence')
    for gate in ('core_24h_ready', 'air_24h_ready', 'chart_28d_ready', 'official_tides_ready', 'all_city_hourly_tides_ready', 'solar_3day_ready', 'coastal_surge_24h_ready', 'snow_probability_inputs_ready', 'regional_384h_sample_inputs_ready'):
        if not q[gate]:
            q['reasons'].append(f'{gate}: failed')
    q['ready'] = not q['reasons']
    q['state'] = 'complete' if q['ready'] else 'partial'
    root = redact_secrets(root)
    out.mkdir(parents=True, exist_ok=True)
    raw_path = out / f'{day.isoformat()}_raw.json'
    raw_path.write_text(json.dumps(root, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')
    # This is a serialization/read-back check, not a claim of meteorological completeness.
    assert json.loads(raw_path.read_text())['target_date_jst'] == day.isoformat()
    latest = out / 'latest'
    latest.mkdir(exist_ok=True)
    files = {}
    for name, city in root['cities'].items():
        key = city['key']
        file = latest / f'{key}.json'
        file.write_text(json.dumps(city, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')
        files[key] = {'path': f'latest/{key}.json', 'bytes': file.stat().st_size,
                      'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
    official = compact_official(root['sources'])
    # Raw JMA/CWA pages and SWPC bulletins remain source material, never instructions.
    file = latest / 'official.json'
    file.write_text(json.dumps(official, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')
    files['official'] = {'path': 'latest/official.json', 'bytes': file.stat().st_size,
                         'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
    file = latest / 'regional.json'
    file.write_text(json.dumps(root['regional_forecasts'], ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')
    files['regional'] = {'path': 'latest/regional.json', 'bytes': file.stat().st_size,
                         'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
    manifest = {k: root[k] for k in ('schema', 'target_date_jst', 'collection_utc', 'window_start_utc',
               'window_end_utc', 'long_end_utc', 'chart_end_utc', 'collector_commit', 'workflow_run_id', 'quality')}
    manifest['files'] = files
    manifest['raw_artifact'] = {'name': f'weather-raw-{os.getenv("GITHUB_RUN_ID", day.isoformat())}', 'filename': raw_path.name,
                                'bytes': raw_path.stat().st_size,
                                'sha256': hashlib.sha256(raw_path.read_bytes()).hexdigest()}
    manifest['source_status'] = {name: {k: source.get(k) for k in ('ok', 'http_status', 'fetched_at_utc', 'issued_at_utc', 'freshness', 'error')}
                                 for name, source in root['sources'].items()}
    (latest / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'target_date_jst': day.isoformat(), 'quality': q, 'raw_file': str(raw_path)}, ensure_ascii=False), flush=True)
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-date-jst')
    parser.add_argument('--out-dir', type=Path, default=Path('output'))
    args = parser.parse_args()
    day = dt.date.fromisoformat(args.target_date_jst) if args.target_date_jst else report_date(dt.datetime.now(UTC))
    root = collect(day, args.out_dir)
    # Missing core numerical inputs are operational failure, but files are still
    # published by the workflow so the 06:00 report can explain the exact gaps.
    gates = ('core_24h_ready', 'air_24h_ready', 'chart_28d_ready', 'all_city_hourly_tides_ready', 'coastal_surge_24h_ready', 'snow_probability_inputs_ready', 'regional_384h_sample_inputs_ready')
    return 0 if all(root['quality'].get(k) for k in gates) else 2


if __name__ == '__main__':
    sys.exit(main())
