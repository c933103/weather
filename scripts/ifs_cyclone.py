"""ECMWF Open Data medium-range IFS tropical-cyclone trajectories."""
from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import urllib.error
import urllib.request

UTC = dt.timezone.utc
STREAMS = ('oper', 'enfo')
SOURCE_PREFIX = 'ecmwf_opendata/cyclone_bufr_'
ORIGINS = ('https://data.ecmwf.int/forecasts',
           'https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com')
MAX_BYTES = 12_000_000
REGION = 'western_north_pacific'


def archive_hours(cycle, model="ifs"):
    return 360 if model == "aifs-ens" or cycle.hour in (0, 12) else 144


def _check_model_stream(model, stream):
    if not (model == 'ifs' and stream in STREAMS or model == 'aifs-ens' and stream == 'enfo'):
        raise ValueError('Unsupported cyclone model or stream')


def product_path(cycle, stream, model='ifs'):
    _check_model_stream(model, stream)
    return (f'{cycle:%Y%m%d/%Hz}/{model}/0p25/{stream}/'
            f'{cycle:%Y%m%d%H%M%S}-{archive_hours(cycle, model)}h-{stream}-tf.bufr')


def _frames(body):
    """Consume every byte, including the edition-4 section boundaries."""
    offset = 0
    frames = []
    while offset < len(body):
        if len(body) - offset < 8 or body[offset:offset + 4] != b'BUFR' or body[offset + 7] != 4:
            raise ValueError(f'Expected a BUFR edition-4 header at byte {offset}')
        length = int.from_bytes(body[offset + 4:offset + 7], 'big')
        end = offset + length
        if length < 45 or end > len(body) or body[end - 4:end] != b'7777':
            raise ValueError(f'Invalid BUFR length or end marker at byte {offset}')
        # Edition 4 Section 1, optional Section 2, Sections 3 and 4.
        sections = (22, 4, 7, 4) if body[offset + 17] & 0x80 else (22, 7, 4)
        cursor = offset + 8
        for minimum in sections:
            if cursor + 3 > end - 4:
                raise ValueError(f'Truncated BUFR section at byte {cursor}')
            size = int.from_bytes(body[cursor:cursor + 3], 'big')
            if size < minimum or cursor + size > end - 4:
                raise ValueError(f'Invalid BUFR section length at byte {cursor}')
            cursor += size
        if cursor != end - 4:
            raise ValueError(f'BUFR sections do not fill the message at byte {offset}')
        frames.append((offset, end))
        offset = end
    return frames


def _decode_message(ec, handle, cycle, stream, model):
    # AIFS ENS adds WMO 001030 (generating application); do not
    # relabel a bare IFS template or accept this wrapper on an IFS path.
    expected_descriptors = [1030, 316082] if model == 'aifs-ens' else [316082]
    if (ec.codes_get(handle, 'bufrHeaderCentre') != 98
            or ec.codes_get(handle, 'dataCategory') != 7
            or ec.codes_get(handle, 'masterTablesVersionNumber') < 35
            or ec.codes_get_array(handle, 'unexpandedDescriptors').tolist() != expected_descriptors):
        raise ValueError('Expected the requested ECMWF tropical-cyclone template 316082')
    header_time = dt.datetime(*(ec.codes_get(handle, 'typical' + field)
                                for field in ('Year', 'Month', 'Day', 'Hour', 'Minute', 'Second')),
                              tzinfo=UTC)
    if header_time != cycle:
        raise ValueError(f'BUFR cycle {header_time.isoformat()} differs from requested cycle')
    subsets = ec.codes_get(handle, 'numberOfSubsets')
    if subsets < 1 or (subsets > 1 and not ec.codes_get(handle, 'compressedData')):
        raise ValueError('Expected a single subset or compressed cyclone subsets')
    if stream == 'oper' and subsets != 1:
        raise ValueError('Expected one deterministic subset per oper message')
    ec.codes_set(handle, 'unpack', 1)
    if model == 'aifs-ens' and set(ec.codes_get_array(handle, 'generatingApplication')) != {1}:
        raise ValueError('Unexpected AIFS ENS generating application')

    def values(key):
        array = ec.codes_get_array(handle, key).tolist()
        # ecCodes returns one element for constants in compressed BUFR.
        if len(array) == 1:
            array *= subsets
        if len(array) != subsets:
            raise ValueError(f'{key} has {len(array)} values for {subsets} subsets')
        return [None if v in (ec.CODES_MISSING_LONG, ec.CODES_MISSING_DOUBLE) else v
                for v in array]

    for field in ('year', 'month', 'day', 'hour', 'minute'):
        if set(values(field)) != {getattr(cycle, field)}:
            raise ValueError(f'BUFR data {field} differs from requested cycle')

    storm_ids = {s.strip() for s in ec.codes_get_string_array(handle, 'stormIdentifier')}
    names = {s.strip() for s in ec.codes_get_string_array(handle, 'longStormName')}
    if len(storm_ids) != 1 or '' in storm_ids or len(names) != 1:
        raise ValueError('Expected one storm identifier and name per BUFR message')
    storm_id = storm_ids.pop()
    identities = list(zip(values('ensembleMemberNumber'), values('ensembleForecastType')))
    if (any(number is None or kind is None for number, kind in identities)
            or len(set(identities)) != subsets
            or (stream == 'oper' and identities[0][1] != 0)):
        raise ValueError(f'Invalid or duplicate member identity for {storm_id}')

    def significance(rank, allowed):
        key = f'#{rank}#meteorologicalAttributeSignificance'
        codes = values(key)
        if not set(codes) <= allowed:
            raise ValueError(f'Unexpected cyclone position significance in {key}: {set(codes)}')
        return codes

    def positions(rank):
        for lat, lon in zip(values(f'#{rank}#latitude'), values(f'#{rank}#longitude')):
            if (lat is not None and not -90 <= lat <= 90
                    or lon is not None and not -180 <= lon <= 180):
                raise ValueError(f'Invalid cyclone coordinates at position rank {rank}')
            yield lat, lon

    significance(1, {1})
    observed = set(positions(1))
    if len(observed) != 1:
        raise ValueError(f'Inconsistent reported centre for {storm_id}')
    observed_lat, observed_lon = observed.pop()
    members = [{'member_number': number, 'forecast_type': kind, 'points': []}
               for number, kind in identities]
    lead_hours = [0]

    def add_points(lead, rank, intensity_rank, centre_allowed):
        centres = significance(rank, centre_allowed)
        significance(rank + 1, {3})
        pressures = values(f'#{intensity_rank}#pressureReducedToMeanSeaLevel')
        winds = values(f'#{intensity_rank}#windSpeedAt10M')
        valid_at = (cycle + dt.timedelta(hours=lead)).isoformat()
        for member, centre, (lat, lon), pressure, (wind_lat, wind_lon), speed in zip(
                members, centres, positions(rank), pressures, positions(rank + 1), winds):
            member['points'].append({
                'lead_hours': lead, 'valid_at_utc': valid_at,
                'latitude': lat, 'longitude': lon, 'centre_significance': centre,
                'pressure_hpa': pressure / 100 if pressure is not None else None,
                'max_wind': {'latitude': wind_lat, 'longitude': wind_lon, 'speed_m_s': speed},
            })

    add_points(0, 2, 1, {4, 5})
    replication = set(values('delayedDescriptorReplicationFactor'))
    if len(replication) != 1 or None in replication:
        raise ValueError(f'Inconsistent forecast replication count for {storm_id}')
    for index in range(1, replication.pop() + 1):
        leads = set(values(f'#{index}#timePeriod'))
        if len(leads) != 1 or None in leads:
            raise ValueError(f'Missing or inconsistent forecast lead for {storm_id}')
        lead = leads.pop()
        if not lead_hours[-1] < lead <= archive_hours(cycle, model):
            raise ValueError(f'Invalid forecast lead {lead}h for {storm_id}')
        add_points(lead, 2 * index + 2, index + 1, {1})
        lead_hours.append(lead)

    return {'storm_id': storm_id, 'storm_name': names.pop() or None,
            'reported_centre': {'latitude': observed_lat, 'longitude': observed_lon},
            'lead_hours': lead_hours, 'members': members}


def decode_tracks(body, cycle, stream, model='ifs'):
    """Validate the whole archive and return its storms with encoded member IDs."""
    import eccodes as ec

    _check_model_stream(model, stream)
    storms = []
    identities = set()
    for start, end in _frames(body):
        handle = None
        try:
            handle = ec.codes_new_from_message(body[start:end])
            storm = _decode_message(ec, handle, cycle, stream, model)
            current = {(storm['storm_id'], m['member_number'], m['forecast_type'])
                       for m in storm['members']}
            if identities & current:
                raise ValueError('Duplicate storm/member identity in archive')
            identities.update(current)
            storms.append(storm)
        except (ec.CodesInternalError, ValueError, OverflowError) as exc:
            raise ValueError(f'BUFR byte {start}: {exc}') from exc
        finally:
            if handle is not None:
                ec.codes_release(handle)
    return storms


def _in_region(storm):
    # Include W-basin systems and other trajectories with a position inside
    # the western North Pacific screen. Keep full tracks after selection.
    if storm['storm_id'].endswith('W'):
        return True

    def in_bounds(point):
        lat, lon = point['latitude'], point['longitude']
        return lat is not None and lon is not None and 0 <= lat <= 60 and 100 <= lon % 360 <= 180

    return in_bounds(storm['reported_centre']) or any(
        in_bounds(point) for member in storm['members'] for point in member['points'])


def _fetch(url):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'EastAsiaWeatherDaily/3.0 (github.com/c933103/weather)',
        'Accept': 'application/octet-stream,*/*',
    })
    with urllib.request.urlopen(request, timeout=12) as response:
        body = response.read(MAX_BYTES + 1)
        size = response.headers.get('Content-Length')
        if (response.status == 200 and size is not None
                and len(body) <= MAX_BYTES and int(size) != len(body)):
            raise ValueError('BUFR response size differs from HTTP Content-Length')
        return response.status, body


def _read_product(url, cycle, stream, model='ifs'):
    result = {'url': url, 'http_status': None, 'status': 'unavailable',
              'requested_cycle_utc': cycle.isoformat(), 'requested_model': model,
              'fetched_at_utc': dt.datetime.now(UTC).isoformat()}
    try:
        result['http_status'], body = _fetch(url)
    except ValueError as exc:
        return {**result, 'http_status': 200, 'status': 'malformed', 'detail': str(exc)}, None
    except urllib.error.HTTPError as exc:
        result['http_status'] = exc.code
        exc.close()
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        result['detail'] = str(exc)[:300]
    if result['http_status'] == 404:
        return {**result, 'status': 'unpublished'}, None
    if result['http_status'] != 200:
        return result, None
    if not body:
        return {**result, 'status': 'empty'}, None
    try:
        if len(body) > MAX_BYTES:
            raise ValueError('BUFR response exceeds download limit')
        storms = decode_tracks(body, cycle, stream, model)
    except ValueError as exc:
        return {**result, 'status': 'malformed', 'detail': str(exc)[:300]}, None
    except (ImportError, RuntimeError, OSError) as exc:
        return {**result, 'status': 'decoder_unavailable', 'detail': str(exc)[:300]}, None
    regional = [storm for storm in storms if _in_region(storm)]
    result['status'] = 'available' if regional else 'no_regional_tracks'
    return result, {
        'message_count': len(storms), 'sha256': hashlib.sha256(body).hexdigest(),
        'tracks': regional,
    }


def collect_tracks(now, stream):
    """Use the newest usable cycle for this stream, with bounded mirror/walk-back."""
    now = now.astimezone(UTC)
    base = now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)
    attempts = []
    selected = None
    decoded = None
    for hours_back in (0, 6, 12, 18):
        cycle = base - dt.timedelta(hours=hours_back)
        empty = None
        for origin in ORIGINS:
            attempt, payload = _read_product(f'{origin}/{product_path(cycle, stream)}', cycle, stream)
            attempts.append(attempt)
            if payload is not None:
                selected, decoded = attempt, payload
                break
            if attempt['status'] == 'empty' and empty is None:
                empty = attempt
            if attempt['status'] == 'decoder_unavailable':
                selected = attempt
                break
        if selected is not None or empty is not None:
            selected = selected or empty
            break
    if selected is None:
        # A missing object cannot explain a different host/cycle failing to answer.
        priority = {'malformed': 2, 'unavailable': 1, 'unpublished': 0}
        selected = max(attempts, key=lambda item: priority[item['status']])
    status = selected['status']
    ok = status in ('available', 'no_regional_tracks', 'empty')
    cycle_utc = selected['requested_cycle_utc'] if decoded is not None else None
    return {**selected, 'ok': ok, 'attempts': attempts,
            'model_initialized_at_utc': cycle_utc, 'issued_at_utc': None,
            'error': None if ok else {
                'code': 'IFS_CYCLONE_' + status.upper(), 'message': selected.get('detail')},
            'response': {'model': 'ecmwf_ifs_medium_range', 'stream': stream,
                         'region': REGION, 'cycle_utc': cycle_utc,
                         **(decoded or {'message_count': 0 if status == 'empty' else None,
                                        'tracks': []})}}
