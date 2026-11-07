"""Retrieve NOAA STOFS tide/surge guidance at applicable coastal grid cells.

STOFS is an explicitly labelled alternate model, not GTSM. Whole forecast files
are temporary; only actual point values, timestamps and provenance are saved.
"""
import concurrent.futures as futures
import datetime as dt
import hashlib
import math
from pathlib import Path
import tempfile
import urllib.request
import urllib.error
import time

UTC = dt.timezone.utc
TARGETS = {'hong_kong': (22.291, 114.214), 'taipei': (25.180, 121.410),
           'tokyo_minato': (35.650, 139.770), 'takamatsu': (34.365, 134.050)}
FIELDS = {250: 'combined_water_level', 194: 'astronomical_tide', 193: 'surge_residual'}


def download(url, file):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={
                'User-Agent': 'EastAsiaWeatherDaily/4.0 (github.com/c933103/weather)'}), timeout=45) as response:
                with open(file, 'wb') as out:
                    while chunk := response.read(1024*1024):
                        out.write(chunk)
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == 2 or isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(2**attempt)
    return {'url': url, 'fetched_at_utc': dt.datetime.now(UTC).isoformat(),
            'bytes': Path(file).stat().st_size,
            'sha256': hashlib.sha256(Path(file).read_bytes()).hexdigest()}


def extract_grib(file, expected, points=None):
    import eccodes as ec
    result = {}
    chosen = dict(points or {})
    with open(file, 'rb') as stream:
        while handle := ec.codes_grib_new_from_file(stream):
            try:
                valid = dt.datetime.strptime(str(ec.codes_get(handle, 'validityDate')) +
                    f'{ec.codes_get(handle, "validityTime"):04d}', '%Y%m%d%H%M').replace(tzinfo=UTC)
                if valid != expected:
                    raise ValueError('NOAA_VALID_TIME_MISMATCH')
                parameter = ec.codes_get(handle, 'parameterNumber')
                field = FIELDS.get(parameter)
                if not field:
                    continue
                if not chosen and field == 'combined_water_level':
                    for city, (lat, lon) in TARGETS.items():
                        if city == 'hong_kong':
                            continue  # Applicable NOAA station used in the narrow harbour.
                        candidates = ec.codes_grib_find_nearest(handle, lat, lon, npoints=4)
                        wet = [p for p in candidates if math.isfinite(p['value']) and abs(p['value']) < 1000 and p['distance'] <= 20]
                        chosen[city] = min(wet, key=lambda p: p['distance']) if wet else None
                result[field] = {}
                for city, point in chosen.items():
                    value = ec.codes_get_elements(handle, 'values', [point['index']])[0] if point else None
                    result[field][city] = float(value) if value is not None and math.isfinite(value) and abs(value) < 1000 else None
            finally:
                ec.codes_release(handle)
    if not {'combined_water_level', 'surge_residual'}.issubset(result):
        raise ValueError('NOAA_REQUIRED_PARAMETERS_MISSING')
    return result, chosen


def extract_hong_kong(file, start, end, field):
    import netCDF4
    import numpy as np
    lat, lon = TARGETS['hong_kong']
    with netCDF4.Dataset(file) as dataset:
        distances = np.hypot(dataset['y'][:]-lat, (dataset['x'][:]-lon)*math.cos(math.radians(lat)))*111.2
        index = int(np.argmin(distances))
        if float(distances[index]) > 10:
            raise ValueError('NOAA_HONG_KONG_STATION_TOO_FAR')
        names = netCDF4.chartostring(dataset['station_name'][:])
        units = dataset['time'].units.split('!')[0].strip()
        times = netCDF4.num2date(dataset['time'][:], units, only_use_cftime_datetimes=False,
                                only_use_python_datetimes=True)
        indices = [(i, t.replace(tzinfo=UTC)) for i, t in enumerate(times)
                   if start <= t.replace(tzinfo=UTC) < end and t.minute == 0 and t.second == 0]
        values = []
        for i, stamp in indices:
            v = dataset['zeta'][i, index]
            values.append(float(v) if not np.ma.is_masked(v) and math.isfinite(float(v)) and abs(float(v)) < 1000 else None)
        return {'time': [t.isoformat() for _, t in indices], field: values,
                'point': {'station_name': str(names[index]).strip(), 'station_index': index,
                          'latitude': float(dataset['y'][index]), 'longitude': float(dataset['x'][index]),
                          'distance_km': round(float(distances[index]), 3)},
                'model_description': str(dataset.getncattr('description'))}


def collect_coastal(start, end, now, cache):
    try:
        import eccodes
        import netCDF4
    except ImportError as exc:
        return {'ready': False, 'cities': {}, 'error': {'code': 'MISSING_DECODER_DEPENDENCY', 'message': str(exc)}}
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    issued = now-dt.timedelta(hours=7)
    issued = issued.replace(hour=issued.hour//6*6, minute=0, second=0, microsecond=0)
    attempts = []
    sample = None
    # Wait for a fully distributed cycle; bounded fallback preserves diagnostics.
    for retry in range(3):
        cycle = issued-dt.timedelta(hours=6*retry)
        prefix = f'https://noaa-gestofs-pds.s3.amazonaws.com/stofs_2d_glo.{cycle:%Y%m%d}/stofs_2d_glo.t{cycle:%H}z'
        hour0 = int((start-cycle).total_seconds()/3600)
        path = cache/f'{cycle:%Y%m%d%H}_f{hour0:03d}.grib2'
        try:
            first = download(f'{prefix}.northpacific.f{hour0:03d}.grib2', path)
            sample, points = extract_grib(path, start)
            attempts.append({'cycle': cycle.isoformat(), 'ok': True})
            break
        except Exception as exc:
            attempts.append({'cycle': cycle.isoformat(), 'ok': False, 'error': str(exc)[:200]})
    if sample is None:
        return {'ready': False, 'attempts': attempts, 'cities': {}, 'error': 'NOAA_CYCLE_UNAVAILABLE'}
    downloads = [first]
    rows = {start: sample}

    def fetch_hour(offset):
        stamp = start+dt.timedelta(hours=offset)
        lead = hour0+offset
        file = cache/f'{cycle:%Y%m%d%H}_f{lead:03d}.grib2'
        record = download(f'{prefix}.northpacific.f{lead:03d}.grib2', file)
        data, _ = extract_grib(file, stamp, points)
        return stamp, data, record

    failures = []
    with futures.ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(fetch_hour, offset): offset for offset in range(1, 24)}
        for future in futures.as_completed(pending):
            try:
                stamp, data, record = future.result()
                rows[stamp] = data; downloads.append(record)
                print(f'NOAA coastal valid hour: {stamp.isoformat()}', flush=True)
            except Exception as exc:
                failures.append({'hour': pending[future], 'error': str(exc)[:250]})
    required = [start+dt.timedelta(hours=h) for h in range(24)]
    cities = {}
    for city in ('taipei', 'tokyo_minato', 'takamatsu'):
        city_data = {'time': [t.isoformat() for t in required], 'point': points.get(city)}
        for field in FIELDS.values():
            city_data[field] = [rows.get(t, {}).get(field, {}).get(city) for t in required]
        city_data['ready'] = all(v is not None for field in ('combined_water_level', 'surge_residual') for v in city_data[field])
        cities[city] = city_data
    hk = {}
    for suffix, field in [('swl', 'surge_residual'), ('cwl', 'combined_water_level')]:
        file = cache/f'{cycle:%Y%m%d%H}_{suffix}.nc'
        try:
            downloads.append(download(f'{prefix}.points.{suffix}.nc', file))
            part = extract_hong_kong(file, start, end, field)
            if not part['model_description'].startswith(f'{cycle:%Y%m%d%H}'):
                raise ValueError('NOAA_NETCDF_CYCLE_MISMATCH')
            hk.update(part)
        except Exception as exc:
            failures.append({'hong_kong_field': field, 'error': str(exc)[:250]})
    hk['ready'] = len(hk.get('time', [])) == 24 and all(v is not None for field in ('surge_residual', 'combined_water_level') for v in hk.get(field, [])) and all(len(hk.get(field, [])) == 24 for field in ('surge_residual', 'combined_water_level'))
    cities['hong_kong'] = hk
    return {'model': 'NOAA STOFS-2D-Global (ADCIRC), GFS forcing', 'is_gtsm': False,
            'model_initialized_at_utc': cycle.isoformat(), 'fetched_at_utc': dt.datetime.now(UTC).isoformat(),
            'units': 'm', 'datum': 'NOAA model sea-level datum; do not subtract HKO/CWA/JMA tide-table datums',
            'limitations': 'Regional model guidance, not an official local warning or wave-runup/total-inundation forecast. Grid is about 10 km; point distance is retained. Keep the model residual separate from official astronomical tide tables.',
            'ready': all(v['ready'] for v in cities.values()), 'cities': cities,
            'downloads': downloads, 'attempts': attempts, 'failures': failures}


