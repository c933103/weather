"""JTWC-guided IFS pressure-low screening; NOT basin-wide or EC46 tracking.

The ECMWF 0.25-degree deterministic IFS field is searched around a
JTWC forecast position. A pressure minimum alone does not prove a
tropical cyclone centre. A minimum with surrounding higher pressure AND
northern-hemisphere cyclonic 10-m winds is a screened centre candidate.

The search is JTWC-guided, so it must never be represented as blind,
independent basin-wide centre detection or ensemble-track verification.
"""
from __future__ import annotations

import io
import math
import tempfile
import numpy as np


def distance_km(a_lat, a_lon, b_lat, b_lon):
    """Great-circle distance, including the international date line."""
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians((b_lon - a_lon + 180) % 360 - 180)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(max(0.0, a))))


def pressure_minimum_candidate(latitudes, longitudes, pressures_pa,
                               reference_lat, reference_lon, radius_deg=5.0):
    """Screen a local, interior closed MSLP minimum in an IFS GRIB field.

    Geographic radius is an approximate tangent-plane distance. Reject a
    minimum found on the search boundary, a flat pressure field or an
    open trough without a surrounding ring of higher pressure. The result
    is evidence of a LOW, not by itself evidence of a tropical cyclone.
    """
    lat = np.asarray(latitudes, dtype=float).ravel()
    lon = np.asarray(longitudes, dtype=float).ravel()
    pressure = np.asarray(pressures_pa, dtype=float).ravel()
    if lat.size != lon.size or lat.size != pressure.size or lat.size < 100:
        return {'status': 'insufficient_field'}
    cos_ref = max(0.2, abs(math.cos(math.radians(reference_lat))))
    dy = lat - reference_lat
    dx = ((lon - reference_lon + 180) % 360 - 180) * cos_ref
    d_ref = np.hypot(dy, dx)
    mask = (np.isfinite(lat) & np.isfinite(lon) & np.isfinite(pressure)
            & (d_ref <= radius_deg))
    if np.count_nonzero(mask) < 40:
        return {'status': 'insufficient_neighbourhood'}
    indices = np.flatnonzero(mask)
    lowest = int(indices[np.argmin(pressure[mask])])
    clat, clon = float(lat[lowest]), float(lon[lowest])
    pmin = float(pressure[lowest])
    # Search-edge minima are not valid centres (a remote low can lie
    # just outside the window and slope into it).
    if d_ref[lowest] > radius_deg - 0.75:
        return {'status': 'minimum_at_search_boundary',
                'candidate_latitude': clat, 'candidate_longitude': clon}
    cos_c = max(0.2, abs(math.cos(math.radians(clat))))
    cdy = lat - clat
    cdx = ((lon - clon + 180) % 360 - 180) * cos_c
    radial = np.hypot(cdy, cdx)
    outer = (np.isfinite(pressure) & (radial >= 1.5) & (radial <= 2.5))
    if np.count_nonzero(outer) < 32:
        return {'status': 'insufficient_outer_ring'}
    # Require broad radial support in all eight sectors. A shallow
    # trough may have higher pressure along only two opposite sides.
    bearings = (np.degrees(np.arctan2(cdx, cdy)) + 360.0) % 360.0
    sectors = (bearings / 45.0).astype(int) % 8
    supported = 0
    sector_min_deltas = []
    for sector in range(8):
        subset = outer & (sectors == sector)
        if not np.any(subset):
            continue
        delta_hpa = (float(np.min(pressure[subset])) - pmin) / 100.0
        sector_min_deltas.append(round(delta_hpa, 2))
        if delta_hpa >= 0.7:
            supported += 1
    mean_ring_delta = (float(np.mean(pressure[outer])) - pmin) / 100.0
    if supported < 7 or mean_ring_delta < 1.5:
        return {'status': 'no_supported_closed_low',
                'candidate_latitude': clat,
                'candidate_longitude': clon,
                'ring_supported_sectors': supported,
                'ring_mean_difference_hpa': round(mean_ring_delta, 2)}
    return {'status': 'closed_pressure_low',
            'latitude': clat, 'longitude': clon,
            'minimum_pressure_hpa': round(pmin / 100.0, 1),
            'ring_supported_sectors': supported,
            'ring_mean_difference_hpa': round(mean_ring_delta, 2),
            'ring_min_differences_hpa': sector_min_deltas,
            'distance_to_jtwc_km': round(distance_km(
                reference_lat, reference_lon, clat, clon), 1),
            'search_radius_deg': radius_deg}


def cyclonic_wind_support(north_u, east_v, south_u, west_v,
                          hemisphere='north'):
    """Check wind rotation around a pressure-low candidate.

    10-m winds include steering/asymmetry; 3/4 tangential components
    must have the cyclonic sign and at least 1 m/s magnitude.
    """
    components = [north_u, east_v, south_u, west_v]
    if any(v is None or not math.isfinite(v) for v in components):
        return {'supported': False, 'valid_components': 0,
                'reason': 'missing_wind_components'}
    direction = 1 if hemisphere == 'north' else -1
    tangential = [-north_u * direction, east_v * direction,
                  south_u * direction, -west_v * direction]
    count = sum(value >= 1.0 for value in tangential)
    return {'supported': count >= 3, 'valid_components': 4,
            'cyclonic_components': count,
            'reason': 'rotation_supported' if count >= 3 else 'rotation_not_established'}


def _with_grib(data, callback):
    """ecCodes requires a seekable GRIB stream; release handles safely."""
    import eccodes as ec
    with tempfile.NamedTemporaryFile(suffix='.grib2') as tmp:
        tmp.write(data)
        tmp.flush()
        with open(tmp.name, 'rb') as stream:
            handle = ec.codes_grib_new_from_file(stream)
            if handle is None:
                raise ValueError('No GRIB message found')
            try:
                return callback(ec, handle)
            finally:
                ec.codes_release(handle)


def screen_grib_centre(msl_grib, u10_grib, v10_grib, reference_lat, reference_lon):
    """Decode ECMWF 0.25-degree MSLP and 10-m winds; return screened evidence.

    No full-grid storm identification is claimed: the initial search window
    is centred on the externally supplied JTWC forecast position.
    """
    def decode_pressure(ec, handle):
        # ecCodes 2.50.0 (the pinned version): codes_grib_get_data returns
        # a tuple of per-point dict records, NOT three arrays — unpacking
        # it raised ValueError on every real field (Codex P1, 2026-11-16,
        # verified against the pinned source). The array API returns the
        # three float64 ndarrays directly.
        latitudes = ec.codes_grib_get_array(handle, 'latitudes')
        longitudes = ec.codes_grib_get_array(handle, 'longitudes')
        values = ec.codes_grib_get_values(handle)
        return pressure_minimum_candidate(latitudes, longitudes, values,
                                          reference_lat, reference_lon)
    evidence = _with_grib(msl_grib, decode_pressure)
    if evidence.get('status') != 'closed_pressure_low':
        return {**evidence, 'centre_identified': False,
                'independence': 'JTWC-guided local search, not basin-wide detection'}
    latitude, longitude = evidence['latitude'], evidence['longitude']
    cos_c = max(0.2, abs(math.cos(math.radians(latitude))))
    try:
        # Only the tangential velocity at cardinal points is needed;
        # the model's 10-m u/v fields are not interpolated to a vortex
        # centre or converted into a false maximum-wind estimate.
        north_u, south_u = _with_grib(u10_grib, lambda ec, h: (
            ec.codes_grib_find_nearest(h, latitude + 1.0, longitude, npoints=1)[0]['value'],
            ec.codes_grib_find_nearest(h, latitude - 1.0, longitude, npoints=1)[0]['value']))
        east_v, west_v = _with_grib(v10_grib, lambda ec, h: (
            ec.codes_grib_find_nearest(h, latitude, longitude + 1.0 / cos_c, npoints=1)[0]['value'],
            ec.codes_grib_find_nearest(h, latitude, longitude - 1.0 / cos_c, npoints=1)[0]['value']))
        wind = cyclonic_wind_support(north_u, east_v, south_u, west_v,
                                      hemisphere='north' if latitude >= 0 else 'south')
    except Exception as exc:
        wind = {'supported': False, 'valid_components': 0,
                'reason': 'wind_field_unavailable', 'error': str(exc)[:160]}
    identified = bool(wind.get('supported'))
    return {**evidence, 'status': 'circulation_supported' if identified
            else 'pressure_minimum_only', 'centre_identified': identified,
            'wind_rotation': wind,
            'independence': 'JTWC-guided local search, not basin-wide detection',
            'ensemble_tracks_verified': False}
