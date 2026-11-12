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


def frost_crosscheck(ifs_surface_min, gfs_soil_min):
    """Structured frost cross-check between NON-EQUIVALENT variables.

    The IFS limb is the ground-surface (skin) temperature; the GFS limb is
    its 0–10 cm soil-layer temperature — a different physical quantity at
    a different level. The GFS value therefore cannot validate the IFS
    frost flag; it is contextual only (review fix, 2026-11-08).
    """
    return {'ifs_surface_min_c': ifs_surface_min,
            'gfs_variable': 'soil_temperature_0_to_10cm',
            'gfs_min_c': gfs_soil_min,
            'equivalent_variables': False,
            'relation': 'contextual_only',
            'gfs_also_below_zero': (gfs_soil_min <= 0) if gfs_soil_min is not None else None}


def _kp_values(text):
    """Kp values from the SWPC outlook texts, in their actual formats.

    The digest's original 'Kp= N' pattern matched none of the current
    products, so max_kp_in_outlook silently stayed null (found 2026-11-07).
    The 3-day forecast states the maximum directly ('The greatest expected
    3 hr Kp for <dates> is 5.00') and both outlooks tabulate 3-hourly Kp in
    rows anchored by their UT range label — every other number on such a
    row is a Kp value.
    """
    values = []
    m = re.search(r'greatest expected 3 hr Kp.*?is\s+([0-9]+(?:\.[0-9]+)?)', text, re.I)
    if m:
        values.append(float(m.group(1)))
    for line in text.splitlines():
        row = re.match(r'\s*\d{2}-\d{2}UT\s+(.*)$', line)
        if row:
            values += [float(x) for x in re.findall(r'[0-9]+(?:\.[0-9]+)?', row.group(1))]
    values += [float(m) for m in re.findall(r'Kp[= ]+([0-9]+(?:\.[0-9]+)?)', text)]
    return [v for v in values if 0 <= v <= 9]


def _nict_digest(sources):
    """NICT daily space-weather report digest (Japan's own bulletin).

    The report is discovered through the centre's JSON API
    (nict/report_latest names the current issue and its text body,
    fetched as nict/report_body). Extracted factually: the issue id and
    update time, the summary verbatim, Kakioka's past-24h maximum K index,
    the next-day geomagnetic/solar outlook phrases, and the largest flare
    class mentioned. Corroboration for the SWPC-based digest — the
    'extreme' bar stays SWPC-defined.
    """
    latest = sources.get('nict/report_latest', {}).get('response')
    if isinstance(latest, str):
        try:
            latest = json.loads(latest)
        except ValueError:
            latest = None
    item = {}
    if isinstance(latest, dict):
        items = latest.get('items') or []
        if items and isinstance(items[0], dict):
            item = items[0]
    body = sources.get('nict/report_body', {}).get('response')
    text = ''
    if isinstance(body, str):
        text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body))
    k = re.search(r'過去24時間の最大K指数は\s*(\d+)', text)
    geo = re.search(r'今後1日間、地磁気活動は(.+?)予想されます', text)
    sol = re.search(r'今後1日間、太陽活動は(.+?)予想されます', text)
    rank = {'C': 0, 'M': 1, 'X': 2}
    # Flare classes appear as 'C6.2フレア' in prose and as 'M3.5/2B' in the
    # report's 3-day flare table; both forms count.
    flares = [(cls, float(v)) for cls, v in
              re.findall(r'([CMX])(\d+(?:\.\d+)?)(?:フレア|/)', text)]
    largest = max(flares, key=lambda f: (rank[f[0]], f[1])) if flares else None
    return {'report_id': item.get('id'), 'updated': item.get('updated'),
            'summary': item.get('summary'),
            'kakioka_k_max_24h': int(k.group(1)) if k else None,
            'geomagnetic_outlook': geo.group(1).strip() if geo else None,
            'solar_outlook': sol.group(1).strip() if sol else None,
            'largest_flare_class': f'{largest[0]}{largest[1]:g}' if largest else None}


def parse_space_weather(sources):
    """Digest of the NOAA SWPC space-weather products in the official bundle.

    Factual extraction only: alert product ids issued, the highest G-scale
    mentioned in the 3-day/geomagnetic outlook text, and whether anything
    warning-level or G2-or-stronger is present (the report's 'extreme' bar).
    """
    alerts = sources.get('swpc/alerts', {}).get('response')
    product_ids = []
    if isinstance(alerts, list):
        product_ids = [a.get('product_id') for a in alerts if isinstance(a, dict)]
    text = ''
    for key in ('swpc/three_day', 'swpc/geomag'):
        value = sources.get(key, {}).get('response')
        if isinstance(value, str):
            text += value + '\n'
    g_scales = [int(m) for m in re.findall(r'\bG([0-5])\b', text)]

    def _alert_scales():
        """R/S/G scales from ISSUED alerts, freshness-windowed.

        services.swpc.noaa.gov/products/alerts.json carries about a
        month of messages: a raw scan would report last month's G3 as
        today's. Only alerts issued within 72 hours before the newest
        alert count, probability-table lines (with %) are skipped, and
        range labels (R1-R2) never match. The geomagnetic scale also
        takes the outlook texts' own maximum (max_g_scale_in_outlook).
        """
        if not isinstance(alerts, list) or not alerts:
            return {}
        stamps = []
        for a in alerts:
            if isinstance(a, dict) and a.get('issue_datetime'):
                try:
                    stamps.append(parse_time(str(a['issue_datetime']).replace(' ', 'T')))
                except Exception:
                    pass
        if not stamps:
            return {}
        newest = max(stamps)
        out = {}
        for a in alerts:
            if not isinstance(a, dict) or not a.get('issue_datetime'):
                continue
            try:
                when = parse_time(str(a['issue_datetime']).replace(' ', 'T'))
            except Exception:
                continue
            if newest - when > dt.timedelta(hours=72):
                continue
            for line in str(a.get('message') or '').splitlines():
                if '%' in line:
                    continue
                for letter in 'RSG':
                    for m in re.findall(rf'\b{letter}([0-5])\b(?!-)', line):
                        out[letter] = max(out.get(letter, 0), int(m))
        return out

    alert_scales = _alert_scales()
    cme_arrivals = []
    for line in text.splitlines():
        low = line.lower()
        if 'cme' in low and ('arrival' in low or 'arrive' in low or 'impact' in low):
            snippet = ' '.join(line.split())[:160]
            if snippet and snippet not in cme_arrivals:
                cme_arrivals.append(snippet)
    kp_values = _kp_values(text)
    warning_ids = [p for p in product_ids if p and (p.startswith('WAR') or p.startswith('ALT'))]
    max_g = max(g_scales) if g_scales else None
    return {'alerts_count': len(product_ids) if isinstance(alerts, list) else None,
            'alert_product_ids': product_ids,
            'warning_or_alert_ids': warning_ids,
            'scales': {'radio_blackout_R': alert_scales.get('R'),
                       'radiation_storm_S': alert_scales.get('S'),
                       'geomagnetic_G': max([v for v in (alert_scales.get('G'), max(g_scales) if g_scales else None) if v is not None], default=None),
                       'basis': 'issued alerts within 72 h before the newest alert (alerts.json spans about a month); geomagnetic also takes the outlook texts maximum'},
            'cme_arrivals': cme_arrivals[:4],
            'max_g_scale_in_outlook': max_g,
            'max_kp_in_outlook': max(kp_values) if kp_values else None,
            'extreme': bool(warning_ids) or (max_g is not None and max_g >= 2),
            'nict': _nict_digest(sources),
            'sources_ok': {k: sources.get(k, {}).get('ok') for k in
                           ('swpc/alerts', 'swpc/three_day', 'swpc/geomag',
                            'nict/report_latest', 'nict/report_body')}}


def grade_from_spread(spread):
    """Chart confidence from the ensemble spread of daily maximum temperature."""
    if spread is None:
        return '低'
    if spread <= 1.2:
        return '高'
    if spread <= 2.2:
        return '中'
    return '低'


def chart_confidence_for_city(city, day0, days=16, ensemble_key='temperature_ensemble'):
    """Per-date chart confidence grades from ensemble temperature members.

    Returns {} when the city carries no such ensemble (older bundles),
    so callers can fall back to an explicit 'not evaluated' marker instead of
    inventing a grade. Dates beyond the ensemble's native span are graded
    '―' (not assessed) with basis 'outside_ensemble_coverage' — an
    unmeasured day must not carry the same label as a day that was
    measured and found low-confidence (review fix, 2026-11-09).
    """
    block = city.get(ensemble_key) or {}
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
            result[day.isoformat()] = {'grade': '―', 'spread_c': None,
                                       'members': len(maxima),
                                       'basis': 'outside_ensemble_coverage'}
    return result


def _aifs_single_crosscheck(city):
    """AIFS Single daily highs vs IFS ENS member-mean highs — agreement record.

    The Single's highs come from its own (six-hourly-origin) samples, so this
    is a coarse agreement signal between the two ECMWF model families, kept
    in the bundle for review; it feeds no chart and no grade.
    """
    single = city.get('aifs_single_daily') or {}
    ens_daily = ((city.get('temperature_ensemble') or {}).get('daily') or {})
    diffs = []
    for day, values in single.items():
        ref = (ens_daily.get(day) or {}).get('temp_high_member_mean')
        if values.get('high') is not None and ref is not None:
            diffs.append(abs(values['high'] - ref))
    return {'days_compared': len(diffs),
            'mean_abs_high_difference_c': round(sum(diffs) / len(diffs), 2) if diffs else None,
            'note': 'AIFS Single daily highs vs IFS ENS member-mean daily highs.'}


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


def fog_assessment(city, start, end):
    """Visibility reduction classified by cause, not by visibility alone.

    Fog requires the low-visibility hours themselves to be near
    saturation (relative humidity >= 95%, a dew-point spread <= 1 °C, or
    dense low cloud at those hours). Visibility below 1,000 m without
    that corroboration is reduced visibility of undetermined cause and
    is never labelled fog (review fix, 2026-11-08).
    """
    h = city['hourly']
    vis = dict(select(h, 'visibility', start, end))
    if len(vis) != 24:
        return {'ready': False, 'valid_slots': len(vis)}
    rh = dict(select(h, 'relative_humidity_2m', start, end))
    dew = dict(select(h, 'dew_point_2m', start, end))
    temp = dict(select(h, 'temperature_2m', start, end))
    low_cloud = dict(select(h, 'cloud_cover_low', start, end))
    low_hours = [t for t, v in vis.items() if v < 1000]
    rh_low = [rh[t] for t in low_hours if t in rh]
    spreads = [temp[t] - dew[t] for t in low_hours if t in temp and t in dew]
    cloud_low = [low_cloud[t] for t in low_hours if t in low_cloud]
    corroborated = bool(low_hours) and (
        (rh_low and max(rh_low) >= 95) or (spreads and min(spreads) <= 1.0)
        or (cloud_low and max(cloud_low) >= 90))
    mist_hours = [t for t, v in vis.items() if 1000 <= v < 5000]
    rh_mist = [rh[t] for t in mist_hours if t in rh]
    if low_hours and corroborated:
        classification = 'fog'
    elif low_hours:
        classification = 'visibility_reduced_cause_undetermined'
    elif mist_hours and rh_mist and max(rh_mist) >= 90:
        classification = 'mist'
    else:
        classification = 'none'
    return {'ready': True, 'visibility_min_m': round(min(vis.values())),
            'hours_below_1000m': len(low_hours),
            'max_rh_at_low_hours_pct': round(max(rh_low), 1) if rh_low else None,
            'min_dewpoint_spread_at_low_hours_c': round(min(spreads), 2) if spreads else None,
            'max_low_cloud_at_low_hours_pct': round(max(cloud_low)) if cloud_low else None,
            'corroborated': corroborated, 'classification': classification,
            'basis': 'visibility with humidity, dew-point spread and low cloud at the same hours; official observations or advisories may corroborate further at report time'}


def urban_inundation_screen(city, start, end):
    """Rainfall-rate screening for urban (pluvial) inundation.

    A screening statistic from the forecast's own hourly rainfall — NOT
    an official warning and NOT a river-flood assessment. Kept distinct
    from river flooding; a negative screen, and equally an empty
    official warning feed, never establishes safety (review fix,
    2026-11-08).
    """
    rows = select(city['hourly'], 'precipitation', start, end, accumulated=True)
    if len(rows) != 24:
        return {'ready': False, 'valid_slots': len(rows)}
    vals = [v for _, v in rows]
    max1 = max(vals)
    max3 = max(sum(vals[i:i + 3]) for i in range(len(vals) - 2))
    return {'ready': True, 'max_1h_mm': round(max1, 1), 'max_3h_mm': round(max3, 1),
            'screen_positive': bool(max1 >= 30 or max3 >= 50),
            'thresholds': 'max 1h >= 30 mm or max 3h >= 50 mm',
            'basis': 'rainfall-rate screening of the IFS hourly precipitation; not an official warning; river flooding is assessed separately'}


def _series_stats(times, values):
    pairs = [(t, v) for t, v in zip(times or [], values or []) if v is not None]
    if not pairs:
        return None
    hi = max(pairs, key=lambda p: p[1])
    return {'maximum': round(hi[1], 3), 'time_of_maximum_utc': hi[0]}


def coastal_assessment(raw_city):
    """Datum-safe coastal assessment (review fix, 2026-11-08).

    The STOFS combined water level is compared ONLY with the STOFS
    model's own astronomical tide (one model, one datum). Official tide
    tables (JMA DL, HKO, CWA datums) are never subtracted from STOFS
    levels: their events give timing and heights on their own stated
    datum, and the STOFS surge residual is always reported separately.
    """
    cf = raw_city.get('coastal_forecast') or {}
    out = {'stofs_ready': bool(cf.get('ready')),
           'datum': cf.get('datum'),
           'surge_residual': _series_stats(cf.get('time'), cf.get('surge_residual')),
           'combined_water_level': _series_stats(cf.get('time'), cf.get('combined_water_level'))}
    tide = _series_stats(cf.get('time'), cf.get('astronomical_tide'))
    out['stofs_astronomical_tide'] = tide
    if tide and out['combined_water_level']:
        out['exceedance_over_model_tide_m'] = round(
            out['combined_water_level']['maximum'] - tide['maximum'], 3)
        out['exceedance_basis'] = "same STOFS model datum (combined level vs the model's own astronomical tide)"
    else:
        out['exceedance_over_model_tide_m'] = None
        out['exceedance_basis'] = ('unavailable: no STOFS astronomical-tide series for this city '
                                   '(Hong Kong comes from the STOFS points file, which carries no tide field); '
                                   'the surge residual stands alone')
    official = raw_city.get('astronomical_tide') or {}
    events = list(official.get('events') or [])
    if not events and official.get('time') and official.get('height'):
        pts = [(t, v) for t, v in zip(official['time'], official['height']) if v is not None]
        for i in range(1, len(pts) - 1):
            if pts[i][1] >= pts[i - 1][1] and pts[i][1] > pts[i + 1][1]:
                events.append({'time': pts[i][0], 'kind': 'high', 'height': pts[i][1],
                               'derived': 'turning point of the official hourly series'})
            elif pts[i][1] <= pts[i - 1][1] and pts[i][1] < pts[i + 1][1]:
                events.append({'time': pts[i][0], 'kind': 'low', 'height': pts[i][1],
                               'derived': 'turning point of the official hourly series'})
    out['official_tide'] = {
        'station': official.get('station'), 'datum': official.get('datum') or official.get('datums'),
        'units': official.get('units'), 'ready': bool(official.get('ready')),
        'events': events,
        'datum_compatible_with_stofs': False,
        'use': 'event timing and heights on the official datum only; never subtracted from STOFS levels'}
    return out


# Per-city monsoon / cold-surge indicators (user probe, 2026-11-08: the
# 季節風 row's evidence was the Ulaanbaatar grid and the ABPW text only —
# the four report cities themselves were never assessed). Computed over
# the seven days from the window start from the city's own hourly
# forecast: the strongest 24-hour temperature fall and the strongest
# 24-hour pressure rise (the classic surge signature pair), and the
# strongest wind inside the city's monsoon sector (NE monsoon for Hong
# Kong / Taipei, NW monsoon for the Japanese cities). surge_signature
# requires BOTH limbs past threshold; the numbers are always carried so
# the report run can judge weaker cases itself.
MONSOON_SECTORS = {'Hong Kong': (300, 100), 'Taipei': (300, 100),
                   'Tokyo Minato Ward': (260, 80), 'Takamatsu': (260, 80)}
SURGE_DROP_C = -5.0
SURGE_RISE_HPA = 5.0
CALM_WIND_MS = 0.5  # WMO calm threshold: below it a direction is not meaningful


def _in_sector(deg, sector):
    lo, hi = sector
    return lo <= deg <= hi if lo <= hi else (deg >= lo or deg <= hi)


def monsoon_digest(city, name, start):
    zone = ZoneInfo(city.get('timezone') or 'UTC')
    horizon = start + dt.timedelta(days=7)
    # Series selection: collection carries a dedicated long IFS series
    # for this digest alone (city['monsoon_hourly'] — forecast_days 10,
    # just the five fields below), because the core 24-hour 'hourly'
    # series reaches only ~51 h into the 7-day window and the coverage
    # gate could never pass on it. Both series are the same model at the
    # same grid, so stamps are merged per field — core first, the long
    # series overlaid — which also keeps bundles collected before the
    # long series existed working unchanged.
    series = {}
    series_sources = []
    for field in ('temperature_2m', 'pressure_msl', 'wind_speed_10m',
                  'wind_direction_10m', 'wind_gusts_10m'):
        series[field] = {}
    for key in ('hourly', 'monsoon_hourly'):
        h = city.get(key) or {}
        if not h.get('time'):
            continue
        series_sources.append(key)
        for field in series:
            for t, v in zip(h.get('time') or [], h.get(field) or []):
                if v is None:
                    continue
                stamp = parse_time(t)
                if start <= stamp <= horizon:
                    series[field][stamp] = v
    out = {'ready': False, 'series_sources': series_sources}
    temps, press = series['temperature_2m'], series['pressure_msl']
    if len(temps) >= 25 and len(press) >= 25:
        drop = rise = None
        for stamp, v in temps.items():
            later = stamp + dt.timedelta(hours=24)
            if later in temps:
                d = temps[later] - v
                if drop is None or d < drop[0]:
                    drop = (d, later)
        for stamp, v in press.items():
            later = stamp + dt.timedelta(hours=24)
            if later in press:
                r = press[later] - v
                if rise is None or r > rise[0]:
                    rise = (r, later)
        # Surge event (review fix, 2026-11-09): the temperature fall and
        # the pressure rise must occur over the SAME 24-hour window. The
        # independent maxima above can sit days apart and do not describe
        # one event. Among jointly qualifying windows take the strongest
        # fall (ties: the strongest rise); the event's timing is recorded.
        event = None
        for stamp, v in temps.items():
            later = stamp + dt.timedelta(hours=24)
            if later not in temps or stamp not in press or later not in press:
                continue
            d = temps[later] - v
            r = press[later] - press[stamp]
            if d <= SURGE_DROP_C and r >= SURGE_RISE_HPA:
                if event is None or d < event[0] or (d == event[0] and r > event[1]):
                    event = (d, r, stamp, later)
        sector = MONSOON_SECTORS.get(name, (300, 100))
        north = None
        for stamp, deg in series['wind_direction_10m'].items():
            speed = series['wind_speed_10m'].get(stamp)
            if speed is None or not _in_sector(deg, sector):
                continue
            if north is None or speed > north[0]:
                north = (speed, series['wind_gusts_10m'].get(stamp), stamp)

        def _local(stamp):
            return stamp.astimezone(zone).strftime('%m-%d %H:%M') if stamp else None

        # Coverage of the 7-day window (review improvement, 2026-11-09):
        # a negative surge verdict is meaningful only when the window
        # searched is actually complete, so the counts, the window
        # bounds and the completeness flag are recorded for the report.
        _expected = int((horizon - start).total_seconds() // 3600) + 1
        _cov_complete = bool(
            len(temps) >= 0.98 * _expected
            and len(press) >= 0.98 * _expected
            and max(temps) >= horizon - dt.timedelta(hours=1)
            and max(press) >= horizon - dt.timedelta(hours=1))
        out = {'ready': True,
               'series_sources': series_sources,
               'window_start_local': _local(start),
               'window_end_local': _local(horizon),
               'coverage': {'temperature_hours': len(temps),
                            'pressure_hours': len(press),
                            'wind_hours': len(series['wind_speed_10m']),
                            'expected_hours': _expected,
                            'complete': _cov_complete},
               'surge_coverage_complete': _cov_complete,
               'max_24h_temp_drop_c': round(drop[0], 1) if drop else None,
               'drop_end_local': _local(drop[1]) if drop else None,
               'max_24h_pressure_rise_hpa': round(rise[0], 1) if rise else None,
               'rise_end_local': _local(rise[1]) if rise else None,
               'northerly_max_ms': round(north[0], 1) if north else None,
               'northerly_gust_ms': (round(north[1], 1)
                                     if north and north[1] is not None else None),
               'northerly_at_local': _local(north[2]) if north else None,
               'surge_signature': event is not None,
               'surge_event': ({'start_local': _local(event[2]),
                                'end_local': _local(event[3]),
                                'temp_drop_c': round(event[0], 1),
                                'pressure_rise_hpa': round(event[1], 1)}
                               if event else None)}
    return out


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
    result['fog'] = fog_assessment(city, start, end)
    result['urban_inundation'] = urban_inundation_screen(city, start, end)
    sd = result.get('snow_depth') or {}
    result['snow_depth_cm'] = (
        {'ready': True, 'minimum': round(sd['minimum'] * 100, 1),
         'maximum': round(sd['maximum'] * 100, 1), 'units': 'cm',
         'source_units': 'm (Open-Meteo snow_depth)'} if sd.get('ready') else {'ready': False})
    air = city['raw_air_quality']
    result['aqi'] = value_range(select(air, 'us_aqi', start, end))
    aqi_points = select(air, 'us_aqi', start, end)
    worst = max(aqi_points, key=lambda p: p[1]) if aqi_points else None
    at_worst = []
    if worst is not None:
        for field in air:
            if field.startswith('us_aqi_'):
                values = dict(select(air, field, start, end))
                if worst[0] in values:
                    at_worst.append((values[worst[0]], field, worst[0].isoformat()))
        at_worst.sort(reverse=True)
    result['aqi']['worst_hour_utc'] = worst[0].isoformat() if worst else None
    result['aqi']['components_at_worst_hour'] = at_worst
    result['aqi']['leading_component_at_worst_hour'] = at_worst[0] if at_worst else None
    result['aqi']['second_component_at_worst_hour'] = (
        at_worst[1] if len(at_worst) > 1 and at_worst[0][0] - at_worst[1][0] <= 10 else None)
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
    gfs = city.get('gfs') or {}
    gfs_temp = value_range(select(gfs, 'temperature_2m', start, end))
    gfs_soil = value_range(select(gfs, 'soil_temperature_0_to_10cm', start, end))
    ifs_surface_min = result['surface_temperature'].get('minimum') if result['surface_temperature'].get('ready') else None
    gfs_soil_min = gfs_soil.get('minimum') if gfs_soil.get('ready') else None
    result['model_crosscheck'] = {
        'gfs_temperature_2m': gfs_temp,
        'gfs_soil_temperature_0_to_10cm': gfs_soil,
        'temperature_max_difference_c': round(abs(result['temperature_2m']['maximum'] - gfs_temp['maximum']), 2)
            if result['temperature_2m'].get('ready') and gfs_temp.get('ready') else None,
        'temperature_min_difference_c': round(abs(result['temperature_2m']['minimum'] - gfs_temp['minimum']), 2)
            if result['temperature_2m'].get('ready') and gfs_temp.get('ready') else None,
        'frost_crosscheck': frost_crosscheck(ifs_surface_min, gfs_soil_min),
        'note': 'NOAA GFS (gfs_seamless via Open-Meteo) as the independent second model; '
                'the report values remain the ECMWF IFS ones unless the official local '
                'observatory forecast contradicts them. The GFS frost limb is its '
                '0–10 cm soil-layer temperature, NOT equivalent to the IFS surface '
                '(skin) temperature: contextual only, never a validation of the frost flag.'}
    # Prevailing wind direction is the MODAL direction (review fix,
    # 2026-11-08): the most frequently occurring 16-point sector over
    # the window's hourly directions — NOT the vector-mean (resultant)
    # direction, which is a different statistic and is kept separately
    # below. Ties break toward the tied sector with the greatest summed
    # wind speed. When the modal sector holds under a quarter of the
    # valid hours the flow is VARIABLE （変動） and no prevailing sector
    # is claimed; with no non-calm valid hours the state is missing —
    # unless every hour was measured and every hour was calm, which is
    # its own state (calm, 静穏) rather than missing input.
    # Calm hours (speed < 0.5 m/s) never vote: a calm hour's recorded
    # direction is not physically meaningful (review fix, 2026-11-09).
    _sector_count = {}
    _sector_speed = {}
    _calm_hours = 0
    for _t, _deg in directions.items():
        _spd = wind.get(_t)
        if _spd is not None and _spd < CALM_WIND_MS:
            _calm_hours += 1
            continue
        if _deg is None:
            continue
        _sec = compass16(_deg)
        _sector_count[_sec] = _sector_count.get(_sec, 0) + 1
        _sector_speed[_sec] = _sector_speed.get(_sec, 0.0) + (wind.get(_t) or 0.0)
    _valid_dirs = sum(_sector_count.values())
    _calm_share = (round(100.0 * _calm_hours / len(wind), 1) if wind else None)
    prevailing_compass = None
    prevailing_share = None
    prevailing_state = 'missing'
    if _valid_dirs:
        _top = max(_sector_count.values())
        _tied = [s for s, n in _sector_count.items() if n == _top]
        _modal = max(_tied, key=lambda s: _sector_speed.get(s, 0.0))
        prevailing_share = round(100.0 * _top / _valid_dirs, 1)
        if _top / _valid_dirs >= 0.25:
            prevailing_compass = _modal
            prevailing_state = 'ok'
        else:
            prevailing_state = 'variable'
    elif len(wind) == 24 and all(v < CALM_WIND_MS for v in wind.values()):
        # Fully measured all-calm window (review improvement,
        # 2026-11-09): 静穏, distinct from missing input.
        prevailing_state = 'calm'
    # Resultant (vector-mean) direction — a separate statistic, never
    # labelled prevailing.
    _sx = _sy = _sw = 0.0
    for _t, _deg in directions.items():
        _s = wind.get(_t)
        if _s is None or _deg is None:
            continue
        _r = math.radians(_deg)
        _sx += _s * math.sin(_r)
        _sy += _s * math.cos(_r)
        _sw += _s
    resultant_deg = None
    if _sw > 0 and math.hypot(_sx, _sy) / _sw >= 0.05:
        resultant_deg = round((math.degrees(math.atan2(_sx, _sy)) + 360) % 360, 1)
    _zone = ZoneInfo(city.get('timezone') or 'UTC')
    result['report_digest'] = {
        'dominant_weather_code': Counter(codes).most_common(1)[0][0] if codes else None,
        'humidity_mean': round(sum(humidity) / len(humidity), 1) if len(humidity) == 24 else None,
        'pressure_mean_hpa': round(sum(current.values()) / len(current), 1) if len(current) == 24 else None,
        'pressure_mean_change_24h': round(sum(v for _, v in changes) / len(changes), 1) if len(changes) == 24 else None,
        'wind_max_time_utc': max_wind_time.isoformat() if max_wind_time else None,
        'wind_max_time_local': max_wind_time.astimezone(_zone).strftime('%m-%d %H:%M') if max_wind_time else None,
        'wind_direction_at_max_deg': directions.get(max_wind_time) if max_wind_time else None,
        'wind_direction_at_max_compass': compass16(directions.get(max_wind_time)) if max_wind_time else None,
        'wbgt_band': wbgt_band,
        'wind_speed_mean_ms': round(sum(wind.values()) / len(wind), 2) if len(wind) == 24 else None,
        'wind_speed_max_ms': round(max(wind.values()), 2) if len(wind) == 24 else None,
        'wind_prevailing_compass': prevailing_compass,
        'wind_prevailing_share_pct': prevailing_share,
        'wind_prevailing_state': prevailing_state,
        'wind_calm_hours': _calm_hours,
        'wind_calm_share_pct': _calm_share,
        'wind_resultant_compass': compass16(resultant_deg) if resultant_deg is not None else None,
        'wind_gust_max_ms': result['wind_gusts_10m'].get('maximum') if result['wind_gusts_10m'].get('ready') else None,
        'wind_gust_max_time_utc': result['wind_gusts_10m'].get('time_of_maximum_utc') if result['wind_gusts_10m'].get('ready') else None,
        'wind_gust_max_time_local': (parse_time(result['wind_gusts_10m']['time_of_maximum_utc']).astimezone(_zone).strftime('%m-%d %H:%M') if result['wind_gusts_10m'].get('ready') and result['wind_gusts_10m'].get('time_of_maximum_utc') else None),
        'hazard_flags': hazard_flags(
            result['pm2_5'].get('maximum') if result['pm2_5'].get('ready') else None,
            result['pm10'].get('maximum') if result['pm10'].get('ready') else None,
            result['surface_temperature'].get('minimum') if result['surface_temperature'].get('ready') else None),
    }
    result['window_conventions'] = {
        'instantaneous_fields': '[start,end) — the value stamped at the window start is included; the value at the end is not',
        'preceding_hour_fields': '(start,end] — precipitation, rain, snowfall, wind_gusts_10m, precipitation_probability and the snowfall-probability member totals cover the hour ending at their timestamp',
        'snow_probability_event': (result.get('snow_probability') or {}).get('event')}
    return result


def regional_digest(raw):
    """Mechanical regional-grid statistics behind the outlook table.

    Ulaanbaatar: the grid's maximum MSLP and the pooled tendency over the
    first 72 hours of the regional series (the Siberian High segment of
    the monsoon row). Manchuria / Sea of Japan: the grid's minimum MSLP in
    the same span, with the grid point and valid time — the
    extratropical-cyclone row's floor, a grid statistic only, never a
    cyclone identification.
    """
    reg = raw.get('regional_forecasts') or {}

    def samples(area):
        rows = []
        for point in (reg.get(area) or {}).get('grid_forecasts') or []:
            hourly = point.get('hourly') or {}
            for t, v in zip(hourly.get('time') or [], hourly.get('pressure_msl') or []):
                if numeric(v):
                    rows.append((parse_time(t), float(v),
                                 point.get('latitude'), point.get('longitude')))
        return sorted(rows)

    digest = {}
    for area in ('ulaanbaatar', 'manchuria', 'sea_of_japan'):
        rows = samples(area)
        if not rows:
            continue
        start = rows[0][0]
        rows = [r for r in rows if r[0] < start + dt.timedelta(hours=72)]
        if not rows:
            continue
        entry = {'span_hours': 72, 'samples': len(rows)}
        if area == 'ulaanbaatar':
            peak = max(rows, key=lambda r: r[1])
            entry['max_pressure_hpa'] = round(peak[1], 1)
            entry['max_at_utc'] = peak[0].isoformat()
            first = [r[1] for r in rows if r[0] < start + dt.timedelta(hours=24)]
            last_start = max(r[0] for r in rows) - dt.timedelta(hours=24)
            last = [r[1] for r in rows if r[0] > last_start]
            if first and last:
                entry['tendency_hpa'] = round(
                    sum(last) / len(last) - sum(first) / len(first), 1)
        else:
            low = min(rows, key=lambda r: r[1])
            entry['min_pressure_hpa'] = round(low[1], 1)
            entry['min_at_utc'] = low[0].isoformat()
            entry['latitude'] = low[2]
            entry['longitude'] = low[3]
        digest[area] = entry
    return digest


def _track_assessment(samples, pressure_key):
    """Structured verification level for one model's track check.

    The samples are single-point MSLP/wind values AT the JTWC forecast
    positions: they can show the model's field is coherent with the
    warning (a deep low at the warned positions), but they cannot locate
    a model centre, so no position difference is ever computed and
    'consistent_with' never means 'confirmed' (review fix, 2026-11-08).
    """
    if not isinstance(samples, list) or not samples:
        return {'retrieved': False, 'agreement': 'not_assessed'}
    pressures = [s.get(pressure_key) for s in samples
                 if isinstance(s, dict) and s.get(pressure_key) is not None]
    if not pressures:
        return {'retrieved': True, 'samples': len(samples), 'samples_with_pressure': 0,
                'agreement': 'inconclusive'}
    frac = sum(1 for v in pressures if v <= 1005) / len(pressures)
    return {'retrieved': True, 'samples': len(samples),
            'samples_with_pressure': len(pressures),
            'point_pressure_min_hpa': round(min(pressures), 1),
            'point_pressure_median_hpa': round(statistics.median(pressures), 1),
            'fraction_points_at_or_below_1005hpa': round(frac, 2),
            'centre_identified': False,
            'centre_identification_basis': 'single-point samples at the JTWC positions cannot locate a model centre',
            'position_difference_km': None,
            'agreement': 'consistent_with' if (min(pressures) <= 1000 and frac >= 0.5) else 'inconclusive',
            'grid_resolution': '0.25 degrees (about 28 km): adequate for a coherence check against a named warning area, not for centre fixing'}


def compute_bundle(raw):
    start, end = parse_time(raw['window_start_utc']), parse_time(raw['window_end_utc'])
    cities = {}
    for name in (v[0] for v in CITIES.values()):
        try:
            cities[name] = compute_city(raw['cities'][name], start, end)
        except Exception as exc:
            cities[name] = {'error': f'{type(exc).__name__}: {exc}'}
    for name in list(cities):
        if not cities[name].get('error'):
            try:
                cities[name]['coastal_assessment'] = coastal_assessment(raw['cities'][name])
            except Exception as exc:
                cities[name]['coastal_assessment'] = {'error': f'{type(exc).__name__}: {exc}'}
    for name in list(cities):
        if not cities[name].get('error'):
            try:
                cities[name]['monsoon'] = monsoon_digest(raw['cities'][name], name, start)
            except Exception as exc:
                cities[name]['monsoon'] = {'error': f'{type(exc).__name__}: {exc}'}
    checks = {}
    for name, city in cities.items():
        checks[name] = not city.get('error') and all(
            v.get('ready') is True for k, v in city.items()
            if isinstance(v, dict) and 'ready' in v and k not in ('fog', 'urban_inundation'))
    sources = raw.get('sources', {})
    ecmwf_probe = sources.get('ecmwf/cyclone_product', {})
    ecmwf_response = ecmwf_probe.get('response')
    if isinstance(ecmwf_response, str):
        ecmwf_response = ecmwf_response[:2000]
    warning_names = sorted(n for n in sources if n.startswith('jtwc/warning_'))
    track_checks = {}
    for prefix in ('gfs/cyclone_check_', 'ecmwf_opendata/cyclone_check_'):
        for name, source in sources.items():
            if name.startswith(prefix):
                body = source.get('response') or {}
                samples = body.get('samples')
                key = 'gfs_pressure_msl_hpa' if prefix.startswith('gfs') else 'ecmwf_pressure_msl_hpa'
                track_checks[name] = {'ok': source.get('ok'), 'samples': samples,
                                      'assessment': _track_assessment(samples, key)}
    cyclone_watch = {
        **parse_jtwc_abpw(sources.get('jtwc/outlook', {}).get('response')),
        'warning_sources': warning_names,
        'warnings_all_ok': all(sources[n].get('ok') for n in warning_names) if warning_names else None,
        'ecmwf_cyclone_product': {'ok': ecmwf_probe.get('ok'),
                                  'http_status': ecmwf_probe.get('http_status'),
                                  'fetched_at_utc': ecmwf_probe.get('fetched_at_utc'),
                                  'base_time_used': ecmwf_response.get('base_time_used')
                                      if isinstance(ecmwf_response, dict) else None,
                                  'attempts': ecmwf_response.get('attempts')
                                      if isinstance(ecmwf_response, dict) else None,
                                  'response': ecmwf_response.get('product')
                                      if isinstance(ecmwf_response, dict) else ecmwf_response},
        'track_checks': track_checks,
        'note': 'ABPW digest covers the advisory valid time; forecast tracks are in the '
                'jtwc/warning_* source texts. The ECMWF entry is the OpenCharts cyclone '
                'product probe (base-time walk-back; data or the API\'s own availability '
                'error, retained in the official bundle); GFS and ECMWF open-data track '
                'samples are the alternative routes when the product cannot be obtained. '
                'None of them silently replaces JTWC warnings.'}
    day0 = dt.date.fromisoformat(raw['target_date_jst'])
    chart_confidence = {}
    chart_confidence_aifs = {}
    aifs_crosscheck = {}
    for key in ('hong_kong', 'tokyo_minato'):
        name = CITIES[key][0]
        if name in raw.get('cities', {}):
            chart_confidence[name] = chart_confidence_for_city(raw['cities'][name], day0, days=28)
            chart_confidence_aifs[name] = chart_confidence_for_city(
                raw['cities'][name], day0, days=28, ensemble_key='aifs_ensemble')
            aifs_crosscheck[name] = _aifs_single_crosscheck(raw['cities'][name])
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
            'space_weather': parse_space_weather(sources),
            'regional_digest': regional_digest(raw),
            'chart_confidence': chart_confidence,
            'chart_confidence_aifs': chart_confidence_aifs,
            'aifs_crosscheck': aifs_crosscheck}


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
