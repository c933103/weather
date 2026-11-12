#!/usr/bin/env python3
"""Render the report's pipe tables (Tables 1 / 2 and the Message-3 outlook
tables 3 / 4) from the bundle.

The tables are deterministic formatting of calculated.json + the city
digests — the user pointed out (2026-11-07) that they belong in the
workflow alongside the chart, leaving the 06:00 run the parts that need
analysis: the official-forecast contradiction check on 天気， the
category-by-category 特記 audit (the generated 特記 is the mechanical
floor — digest flags, visibility, snowfall — to which the run adds the
official-product, pollen, dust and flood/surge limbs), the audit of
Message 3's generated analysis against the warning remarks and the
official products (plus the appended 判定 row), and verification.
Cell rules are the cron's settled ones, encoded once here.
"""
import math
import re
from datetime import datetime, timedelta, timezone

CITY_ORDER = [('Hong Kong', '香港'), ('Taipei', '台北'),
              ('Tokyo Minato Ward', '東京港区'), ('Takamatsu', '高松')]

WEATHER_TOKENS = {0: '晴', 1: '晴', 2: '晴時々曇', 3: '曇', 45: '霧', 48: '霧',
                  51: '霧雨', 53: '霧雨', 55: '霧雨', 56: '霧雨', 57: '霧雨',
                  61: '小雨', 63: '雨', 65: '大雨', 66: '雨', 67: '雨',
                  71: '雪', 73: '雪', 75: '雪', 77: '雪', 80: '小雨', 81: '雨',
                  82: '大雨', 85: '雪', 86: '雪', 95: '雷雨', 96: '雷雨', 99: '雷雨'}

AQI_FIELDS = {'us_aqi_pm2_5': 'PM2.5', 'us_aqi_pm10': 'PM10', 'us_aqi_ozone': 'O3',
              'us_aqi_nitrogen_dioxide': 'NO2', 'us_aqi_sulphur_dioxide': 'SO2',
              'us_aqi_carbon_monoxide': 'CO'}


def fmt(v, digits=1):
    """Compact number: at most `digits` decimals, trailing .0 dropped."""
    if v is None:
        return '―'
    r = round(float(v), digits)
    if r == int(r):
        return str(int(r))
    return str(r)


def _range(block):
    if not block or block.get('minimum') is None or block.get('maximum') is None:
        return '―'
    return f"{fmt(block['minimum'])}~{fmt(block['maximum'])}"


def _weather(digest):
    code = (digest or {}).get('dominant_weather_code')
    return WEATHER_TOKENS.get(code, '―') if code is not None else '―'


def _heat_cold(c, digest, japan):
    wbgt = c.get('wbgt') or {}
    wbgt_max = wbgt.get('maximum')
    band = (digest or {}).get('wbgt_band')
    chill = c.get('wind_chill') or {}
    chill_ok = chill.get('status') == 'applicable' and chill.get('minimum') is not None
    wbgt_min = wbgt.get('minimum')
    wbgt_ok = wbgt_max is not None and band
    wbgt_text = (f'{fmt(wbgt_min)}~{fmt(wbgt_max)} {band}' if wbgt_min is not None
                 else f'{fmt(wbgt_max)} {band}')
    if wbgt_ok and chill_ok:
        # Both valid: report BOTH (review fix, 2026-11-08) — selecting
        # one hides the other.
        return f"{wbgt_text}・風冷 {fmt(chill['minimum'])}"
    if wbgt_ok:
        # All four cities always carry the WBGT range (user fix,
        # 2026-11-10): Hong Kong/Taipei previously showed WBGT only at
        # max >= 21 and otherwise fell through to a blank-looking '―'
        # even though the range was computed (e.g. HK 9.5~19.5 on
        # 2026-11-10). '―' now means only: neither quantity exists.
        return wbgt_text
    if chill_ok:
        return f"風冷 {fmt(chill['minimum'])}"
    return '―'


def _precip(c):
    prob = (c.get('precipitation_probability') or {}).get('maximum')
    total = (c.get('precipitation') or {}).get('total')
    snow = (c.get('snowfall') or {}).get('total')
    snowp = (c.get('snow_probability') or {}).get('percent')
    head = f'{fmt(prob, 0)}%' if prob is not None else '―'
    parts = [head]
    if total is not None and total > 0:
        parts.append(f'{fmt(total)}mm')
    # Snowfall probability is an independent ensemble quantity: shown on
    # its own threshold, never merged into the rain probability.
    if snowp is not None and snowp >= 30:
        parts.append(f'雪確率{fmt(snowp, 0)}%')
    if snow is not None and snow >= 0.1:
        parts.append(f'雪 {fmt(snow)}cm')
    depth = (c.get('snow_depth_cm') or {}).get('maximum')
    if depth is not None and depth >= 0.5:
        parts.append(f'積雪深 {fmt(depth)}cm')
    return ' '.join(parts)


def _ms(v):
    s = f'{v:.1f}'
    return s.rstrip('0').rstrip('.') if '.' in s else s


def _wind(c, digest):
    """All wind speeds in m/s (user direction, 2026-11-08): maximum
    sustained with its direction, gust maximum, window mean, and the
    prevailing (resultant) direction — four distinct statistics in one
    consistent unit, each labelled."""
    digest = digest or {}
    wmax = digest.get('wind_speed_max_ms')
    if wmax is None:
        wmax = (c.get('wind_speed_10m') or {}).get('maximum')
    if wmax is None:
        return '―'
    parts = [f'最大{_ms(wmax)}m/s']
    compass = digest.get('wind_direction_at_max_compass')
    if compass:
        parts.append(compass)
    gust = digest.get('wind_gust_max_ms')
    if gust is not None:
        parts.append(f'突風{_ms(gust)}')
    mean = digest.get('wind_speed_mean_ms')
    if mean is not None:
        parts.append(f'平均{_ms(mean)}')
    # Prevailing direction states are distinct: a named sector, 変動
    # (variable — data present but no dominant sector), 静穏 (calm —
    # every hour measured and calm), or ― (missing input).
    prev = digest.get('wind_prevailing_compass')
    state = digest.get('wind_prevailing_state')
    if prev:
        parts.append(f'卓越{prev}')
    elif state == 'calm':
        parts.append('卓越静穏')
    elif state == 'variable':
        parts.append('卓越変動')
    else:
        parts.append('卓越―')
    return ' '.join(parts)


def _aqi_category(v):
    if v is None:
        return '―'
    for limit, label in ((50, '良'), (100, '普通'), (150, '注意'),
                         (200, '不良'), (300, '甚不良')):
        if v <= limit:
            return label
    return '危険'


def _aqi(c, raw_city):
    aqi = c.get('aqi') or {}
    vmax = aqi.get('maximum')
    lead = aqi.get('leading_component_at_worst_hour')
    if vmax is None or not lead:
        return '―'
    leaders = [AQI_FIELDS.get(lead[1], lead[1])]
    second = aqi.get('second_component_at_worst_hour')
    if second:
        leaders.append(AQI_FIELDS.get(second[1], second[1]))
        return f"{fmt(vmax, 0)} {_aqi_category(vmax)} {'・'.join(leaders)}"
    # Fallback for older calculated bundles: another pollutant within 10
    # sub-index points of the leader at the worst hour, read from the
    # city's raw air-quality series at that same hour.
    raw = (raw_city or {}).get('raw_air_quality') or {}
    times = raw.get('time') or []
    if lead[2] in times:
        i = times.index(lead[2])
        close = []
        for field, label in AQI_FIELDS.items():
            series = raw.get(field) or []
            if field != lead[1] and i < len(series) and isinstance(series[i], (int, float)):
                if lead[0] - series[i] <= 10:
                    close.append((series[i], label))
        if close:
            leaders.append(max(close)[1])
    return f"{fmt(vmax, 0)} {_aqi_category(vmax)} {'・'.join(leaders)}"


def _uv(c):
    vmax = (c.get('uv_index') or {}).get('maximum')
    if vmax is None:
        return '―'
    for limit, label in ((3, '弱'), (6, '中'), (8, '強'), (11, '甚強')):
        if vmax < limit:
            return f'{fmt(vmax)} {label}'
    return f'{fmt(vmax)} 極端'


def _pressure(digest):
    digest = digest or {}
    mean, delta = digest.get('pressure_mean_hpa'), digest.get('pressure_mean_change_24h')
    if mean is None:
        return '―'
    if delta is None:
        return fmt(mean)
    return f'{fmt(mean)} ({delta:+.1f})'


def _tokki_floor(c, digest):
    """The mechanical 特記 floor: digest hazard flags + visibility + snow.

    The 06:00 run's category-by-category audit adds the remaining limbs
    (official products, pollen, dust, flood/surge, strong wind/heavy rain/
    heat) and may correct this floor; 無 here means only that no mechanical
    limb fired.
    """
    flags = (digest or {}).get('hazard_flags') or {}
    tokens = []
    if flags.get('high_pm'):
        tokens.append('高PM')
    if flags.get('frost'):
        tokens.append('霜')
    fog = (c.get('fog') or {}).get('classification')
    if fog == 'fog':
        tokens.append('霧')
    elif fog == 'mist':
        tokens.append('靄')  # mist token in kanji (user, 2026-11-08; JMA writes もや in kana)
    if (c.get('urban_inundation') or {}).get('screen_positive'):
        tokens.append('内水注意')
    snow = (c.get('snowfall') or {}).get('total')
    snowp = (c.get('snow_probability') or {}).get('percent')
    if (snow is not None and snow >= 0.1) or (snowp is not None and snowp >= 50):
        tokens.append('雪')
    return '・'.join(tokens) if tokens else '無'


def _tide(c, raw_city):
    coast = c.get('coastal_assessment') or {}
    official = coast.get('official_tide') or {}
    events = official.get('events') or []
    if not events:
        return '―'
    from zoneinfo import ZoneInfo
    tz = ZoneInfo((raw_city or {}).get('timezone') or 'UTC')

    def height(ev):
        if 'height' in ev:
            return ev['height']
        return ev.get('height_local_msl')
    highs = [e for e in events if e.get('kind') == 'high' and height(e) is not None]
    lows = [e for e in events if e.get('kind') == 'low' and height(e) is not None]
    unit = official.get('units') or ''
    digits = 1 if unit == 'm' else 0
    parts = []
    if highs:
        ev = max(highs, key=height)
        parts.append(f"満潮 {_parse_iso(ev['time']).astimezone(tz):%H:%M} {fmt(height(ev), digits)}{unit}")
    if lows:
        ev = min(lows, key=height)
        parts.append(f"干潮 {_parse_iso(ev['time']).astimezone(tz):%H:%M} {fmt(height(ev), digits)}{unit}")
    return ' '.join(parts) if parts else '―'


def render_tables(raw, calculated):
    """Return (table1_markdown, table2_markdown) for the four report cities."""
    calc_cities = (calculated or {}).get('cities') or {}
    raw_cities = (raw or {}).get('cities') or {}
    per_city = {}
    for name, _label in CITY_ORDER:
        c = calc_cities.get(name) or {}
        per_city[name] = (c, c.get('report_digest') or {}, raw_cities.get(name) or {})
    header = '| | ' + ' | '.join(label for _n, label in CITY_ORDER) + ' |'
    sep = '|---|---|---|---|---|'

    def row(label, fn):
        cells = [fn(*per_city[name][:2], per_city[name][2],
                    name in ('Tokyo Minato Ward', 'Takamatsu'))
                 for name, _l in CITY_ORDER]
        return f'| {label} | ' + ' | '.join(cells) + ' |'

    t1 = [header, sep,
          row('天気', lambda c, d, r, j: _weather(d)),
          row('気温', lambda c, d, r, j: _range(c.get('temperature_2m'))),
          row('体感', lambda c, d, r, j: _range(c.get('apparent_temperature'))),
          row('暑寒', lambda c, d, r, j: _heat_cold(c, d, j)),
          row('湿度', lambda c, d, r, j: (
              f"{fmt(d.get('humidity_mean'), 0)}% ({_range(c.get('relative_humidity_2m'))})"
              if d.get('humidity_mean') is not None else '―')),
          row('降水', lambda c, d, r, j: _precip(c)),
          row('風', lambda c, d, r, j: _wind(c, d))]
    notes = []
    for name, label in CITY_ORDER:
        diff = ((per_city[name][0].get('model_crosscheck') or {})
                .get('temperature_max_difference_c'))
        if diff is not None and diff >= 3:
            notes.append(f'{label}の最高気温でGFSと{fmt(diff)}°C差')
    if notes:
        t1.append('モデル差：' + '、'.join(notes) + '（値はECMWF IFS）')

    t2 = [header, sep,
          row('AQI', lambda c, d, r, j: _aqi(c, r)),
          row('UV', lambda c, d, r, j: _uv(c)),
          row('気圧', lambda c, d, r, j: _pressure(d)),
          row('潮汐', lambda c, d, r, j: _tide(c, r)),
          row('特記', lambda c, d, r, j: _tokki_floor(c, d))]
    return '\n'.join(t1) + '\n', '\n'.join(t2) + '\n'


# --- Message 3: the structured special-weather outlook (user, 2026-11-08) ---
# Table 3 lists every active tropical cyclone; Table 4 is the fixed-row
# outlook （季節風 / 温帯低気圧 / 宇宙天気 / ECMWF検証）. Both are mechanical
# floors like Table 2's 特記: every shipped cell is a true statement at its
# stated basis, and the 06:00 run replaces the 脅威 cells with its judged
# assessment from the warning tracks, corrects the 季節風 / 温帯低気圧
# cells against the ABPW text and JMA products when they say more, and
# appends the 判定 row to Table 4 before sending.

CATEGORY_TOKENS = {'SUPER TYPHOON': 'STY', 'TYPHOON': 'TY',
                   'TROPICAL STORM': 'TS', 'TROPICAL DEPRESSION': 'TD'}

MOVEMENT_TOKENS = {'NORTHWARD': 'N', 'NORTH-NORTHEASTWARD': 'NNE',
                   'NORTHEASTWARD': 'NE', 'EAST-NORTHEASTWARD': 'ENE',
                   'EASTWARD': 'E', 'EAST-SOUTHEASTWARD': 'ESE',
                   'SOUTHEASTWARD': 'SE', 'SOUTH-SOUTHEASTWARD': 'SSE',
                   'SOUTHWARD': 'S', 'SOUTH-SOUTHWESTWARD': 'SSW',
                   'SOUTHWESTWARD': 'SW', 'WEST-SOUTHWESTWARD': 'WSW',
                   'WESTWARD': 'W', 'WEST-NORTHWESTWARD': 'WNW',
                   'NORTHWESTWARD': 'NW', 'NORTH-NORTHWESTWARD': 'NNW',
                   'STATIONARY': '停滞', 'QUASI-STATIONARY': '停滞'}

# The report's named warning areas (user, 2026-11-07), each with a
# representative point for closest-approach analysis along the JTWC
# warning track.
WATCH_AREAS = [('香港', 22.3, 114.2), ('台湾', 23.8, 121.0),
               ('ルソン海峡', 21.3, 120.9), ('小笠原', 27.1, 142.2),
               ('火山列島', 24.8, 141.3), ('本州', 35.7, 139.8)]

_JST = timezone(timedelta(hours=9))
_COMPASS16 = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
              'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
_HDR_RE = re.compile(r'PGTW (\d{2})(\d{2})(\d{2})')
_POS_RE = re.compile(r'WARNING POSITION:\s*\n\s*(\d{2})(\d{2})(\d{2})Z --- NEAR '
                     r'([\d.]+)([NS]) ([\d.]+)([EW])')
_MOVE_RE = re.compile(r'MOVEMENT PAST SIX HOURS - (\d+) DEGREES AT (\d+) KTS')
_WIND_RE = re.compile(r'MAX SUSTAINED WINDS - (\d+) KT, GUSTS (\d+) KT')
_FCST_RE = re.compile(r'(\d+) HRS, VALID AT:\s*\n\s*(\d{2})(\d{2})(\d{2})Z --- '
                      r'([\d.]+)([NS]) ([\d.]+)([EW])')


def _parse_iso(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None


def _resolve(day, hh, mm, anchor):
    """Resolve a DDHHMM group against an anchor datetime, choosing the
    month candidate (previous / current / next) closest to the anchor."""
    best = None
    for shift in (-1, 0, 1):
        total = anchor.year * 12 + (anchor.month - 1) + shift
        try:
            cand = datetime(total // 12, total % 12 + 1, day, hh, mm,
                            tzinfo=timezone.utc)
        except ValueError:
            continue
        if best is None or abs(cand - anchor) < abs(best - anchor):
            best = cand
    return best


def _warning_text(raw, system_id):
    entry = ((raw or {}).get('sources') or {}).get(
        f'jtwc/warning_{(system_id or "").lower()}')
    if isinstance(entry, dict) and entry.get('ok') and entry.get('response'):
        return entry['response']
    return None


def _parse_warning(text, anchor):
    """Parse a JTWC warning text into its current position / movement /
    winds and its forecast track [(valid_utc, lat, lon, max_kt), ...].
    Returns None when the text carries no parseable warning position."""
    if not text:
        return None
    pos = _POS_RE.search(text)
    if not pos:
        return None
    hdr = _HDR_RE.search(text)
    ref = anchor
    if hdr:
        ref = _resolve(int(hdr.group(1)), int(hdr.group(2)),
                       int(hdr.group(3)), anchor) or anchor

    def coord(value, hemi, positive):
        return float(value) if hemi == positive else -float(value)

    valid = _resolve(int(pos.group(1)), int(pos.group(2)),
                     int(pos.group(3)), ref)
    lat = coord(pos.group(4), pos.group(5), 'N')
    lon = coord(pos.group(6), pos.group(7), 'E')
    wind = _WIND_RE.search(text)
    max_kt = int(wind.group(1)) if wind else None
    gust_kt = int(wind.group(2)) if wind else None
    move = _MOVE_RE.search(text)
    track = [(valid, lat, lon, max_kt)]
    for fm in _FCST_RE.finditer(text):
        fvalid = _resolve(int(fm.group(2)), int(fm.group(3)),
                          int(fm.group(4)), ref)
        w2 = _WIND_RE.search(text, fm.end(), fm.end() + 300)
        track.append((fvalid,
                      coord(fm.group(5), fm.group(6), 'N'),
                      coord(fm.group(7), fm.group(8), 'E'),
                      int(w2.group(1)) if w2 else None))
    track = [p for p in track if p[0] is not None]
    track.sort(key=lambda p: p[0])
    # Wind radii per track point, PER QUADRANT (nautical miles), and the
    # analysis maximum significant wave height: the impact fields the
    # centre distance alone cannot express. The 34 kt field is NOT
    # circular — collapsing the quadrants to their maximum overstates
    # the reach toward an area facing a weaker quadrant (review fix,
    # 2026-11-08) — so each block keeps its NE/SE/SW/NW values; a block
    # without quadrant labels is kept as one isotropic value ('ALL').
    marks = [pos.start()] + [fm.start() for fm in _FCST_RE.finditer(text)]
    radii = []
    for i, mstart in enumerate(marks):
        mend = marks[i + 1] if i + 1 < len(marks) else len(text)
        segment = text[mstart:mend]
        entry = {}
        for block in re.finditer(
                r'RADIUS OF (\d+) KT WINDS - (.*?)(?=RADIUS OF|FORECAST WINDS|'
                r'MAXIMUM SIGNIFICANT|REMARKS|\Z)', segment, re.S):
            body = block.group(2)
            pairs = re.findall(
                r'(\d+)\s*NM\s+(NORTHEAST|SOUTHEAST|SOUTHWEST|NORTHWEST)',
                body)
            if pairs:
                entry[int(block.group(1))] = {
                    {'NORTHEAST': 'NE', 'SOUTHEAST': 'SE',
                     'SOUTHWEST': 'SW', 'NORTHWEST': 'NW'}[q]: int(v)
                    for v, q in pairs}
            else:
                values = [int(v) for v in re.findall(r'(\d+) NM', body)]
                if values:
                    entry[int(block.group(1))] = {'ALL': max(values)}
        radii.append(entry)
    wave = re.search(r'MAXIMUM\s+SIGNIFICANT\s+WAVE\s+HEIGHT\s+AT\s+\d+Z\s+IS\s+(\d+)\s+FEET', text)
    return {'valid': valid, 'lat': lat, 'lon': lon, 'max_kt': max_kt,
            'gust_kt': gust_kt,
            'move_deg': int(move.group(1)) if move else None,
            'move_kt': int(move.group(2)) if move else None,
            'track': track, 'track_radii': radii,
            'max_wave_ft': int(wave.group(1)) if wave else None}


def _track_closest(track, alat, alon):
    """Closest approach of a forecast track to a point, segment-wise.
    Returns (distance_km, valid_utc_at_closest, interpolated_max_kt)."""
    if not track:
        return None
    if len(track) == 1:
        _, la, lo, w = track[0]
        return (_haversine_km(la, lo, alat, alon), track[0][0], w)
    best = None
    for (t0, la0, lo0, w0), (t1, la1, lo1, w1) in zip(track, track[1:]):
        scale = math.cos(math.radians((la0 + la1) / 2.0))
        ax, ay = lo0 * scale, la0
        bx, by = lo1 * scale, la1
        px, py = alon * scale, alat
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        t = 0.0 if denom == 0 else max(
            0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
        dist = _haversine_km(la0 + (la1 - la0) * t, lo0 + (lo1 - lo0) * t,
                             alat, alon)
        if best is None or dist < best[0]:
            when = t0 + (t1 - t0) * t if (t0 and t1) else (t0 or t1)
            wind = (w0 + (w1 - w0) * t) if (w0 is not None and w1 is not None) \
                else (w0 if w0 is not None else w1)
            best = (dist, when, wind)
    return best


def _round10(value):
    return int(round(value / 10.0)) * 10


def _bearing_deg(la, lo, alat, alon):
    """Initial bearing from a storm centre to an area point, 0–360°."""
    p1, p2 = math.radians(la), math.radians(alat)
    dl = math.radians(alon - lo)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _gale_reach(track, radii, alat, alon):
    """True when the forecast 34 kt wind footprint reaches the area:
    the wind impact outcome, assessed separately from the centre track.
    The radius tested is the one for the quadrant FACING the area
    (bearing from the centre), never the maximum over quadrants treated
    as a circle; an unlabelled radius ('ALL', or the legacy plain-number
    shape) is isotropic by construction. The footprint is tested at
    every forecast point AND along each segment between consecutive
    points — at the segment's closest-approach point, with the facing
    quadrant's radius linearly interpolated between the two points
    (review improvement, 2026-11-09). The segment test is approximate
    screening only: an inferred between-points passage is never an
    officially issued exact arrival time."""
    if not radii or not track:
        return False

    def _radius_at(la, lo, entry):
        r34 = (entry or {}).get(34)
        if not r34:
            return None
        if isinstance(r34, (int, float)):
            return r34
        quad = ('NE', 'SE', 'SW', 'NW')[
            int(_bearing_deg(la, lo, alat, alon) // 90) % 4]
        return r34.get(quad, r34.get('ALL'))

    pts = list(zip(track, radii))
    for (_t, la, lo, _w), entry in pts:
        r_nm = _radius_at(la, lo, entry)
        if r_nm and _haversine_km(la, lo, alat, alon) <= r_nm * 1.852:
            return True
    for ((_t0, la0, lo0, _w0), e0), ((_t1, la1, lo1, _w1), e1) in zip(pts, pts[1:]):
        scale = math.cos(math.radians((la0 + la1) / 2.0))
        ax, ay = lo0 * scale, la0
        bx, by = lo1 * scale, la1
        px, py = alon * scale, alat
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        t = 0.0 if denom == 0 else max(
            0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
        if t in (0.0, 1.0):
            continue  # endpoints already tested above
        r0 = _radius_at(la0, lo0, e0)
        r1 = _radius_at(la1, lo1, e1)
        if r0 is None and r1 is None:
            continue
        r_nm = (r0 + (r1 - r0) * t) if (r0 is not None and r1 is not None) \
            else (r0 if r0 is not None else r1)
        la = la0 + (la1 - la0) * t
        lo = lo0 + (lo1 - lo0) * t
        if r_nm and _haversine_km(la, lo, alat, alon) <= r_nm * 1.852:
            return True
    return False


def _warning_threat(track, radii=None, max_wave_ft=None):
    """Analyzed threat from the warning forecast track: the closest
    approach to each named warning area, segment-interpolated, with the
    JST time and the forecast intensity at closest approach — plus the
    separate wind outcome (34 kt radii reach) and maritime outcome
    (maximum significant wave height), which the centre distance alone
    understates."""
    per_area = []
    for label, alat, alon in WATCH_AREAS:
        got = _track_closest(track, alat, alon)
        if got:
            per_area.append((got[0], label, got[1], got[2],
                             _gale_reach(track, radii, alat, alon)))
    if not per_area:
        return '―'
    per_area.sort(key=lambda p: p[0])
    close = [p for p in per_area if p[0] <= 1000]
    if not close:
        text = (f"対象地域への接近なし"
                f"（最接近 {per_area[0][1]} 約{_round10(per_area[0][0])}km）")
        if any(p[4] for p in per_area):
            text += '（ただし34kt強風域の到達あり）'
        return text
    dist, label, when, wind, gale = close[0]
    head = f"{label} 約{_round10(dist)}km"
    if when is not None:
        head += f"・{when.astimezone(_JST):%m/%d %H:%M}JST"
    if wind is not None:
        head += f"・{int(round(wind))}kt級"
    if gale:
        head += '・34kt強風域到達'
    if len(close) > 1:
        head += f"・{close[1][1]} 約{_round10(close[1][0])}km"
        if close[1][4] and not gale:
            head += '（34kt強風域到達）'
    if max_wave_ft is not None and max_wave_ft >= 15:
        head += f'・高波{max_wave_ft}ft'
    return head


def _category_token(max_kt, fallback):
    if max_kt is None:
        return fallback
    return 'TY' if max_kt >= 64 else 'TS' if max_kt >= 34 else 'TD'


def _latlon(lat, lon):
    if lat is None or lon is None:
        return '―'
    return (f"{abs(lat):.1f}{'N' if lat >= 0 else 'S'} "
            f"{abs(lon):.1f}{'E' if lon >= 0 else 'W'}")


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _system_row(raw, anchor, system):
    """One Table 3 row. Position, intensity and movement come from the
    system's current JTWC warning text (the ABPW digest values lag the
    warnings and are only the fallback for systems with no warning);
    the threat cell is the warning-track closest-approach analysis."""
    sid = system.get('id') or ''
    fallback_token = CATEGORY_TOKENS.get(
        system.get('category'), system.get('category') or '')
    name = system.get('name')
    warn = _parse_warning(_warning_text(raw, sid), anchor)
    if warn:
        token = _category_token(warn['max_kt'], fallback_token)
        head = f"{name}（{sid}・{token}）" if name else f"{sid}（{token}）"
        pos = [_latlon(warn['lat'], warn['lon'])]
        if warn['max_kt'] is not None:
            pos.append(f"最大{warn['max_kt']}kt")
        if warn['gust_kt'] is not None:
            pos.append(f"突風{warn['gust_kt']}kt")
        if warn['move_deg'] is not None and warn['move_kt'] is not None:
            move = f"{_COMPASS16[int(round(warn['move_deg'] / 22.5)) % 16]} {warn['move_kt']}kt"
        else:
            move = '―'
        return (f"| {head} | {'・'.join(pos)} | {move} | "
                f"{_warning_threat(warn['track'], warn.get('track_radii'), warn.get('max_wave_ft'))} |")
    head = f"{name}（{sid}・{fallback_token}）" if name else f"{sid}（{fallback_token}）"
    pos = [_latlon(system.get('latitude'), system.get('longitude'))]
    if system.get('max_winds_kt') is not None:
        pos.append(f"最大{system['max_winds_kt']}kt")
    if system.get('gusts_kt') is not None:
        pos.append(f"突風{system['gusts_kt']}kt")
    move = MOVEMENT_TOKENS.get(system.get('movement_text'),
                               system.get('movement_text'))
    motion = (f"{move} {system['movement_kt']}kt"
              if move and system.get('movement_kt') is not None
              else (str(move) if move else '―'))
    return f"| {head} | {'・'.join(pos)} | {motion} | ―（警報未発出） |"


def _monsoon_analysis(cw, digest, cities=None):
    mentions = (cw or {}).get('monsoon_mentions') or []
    parts = ['モンスーンの特記なし' if not mentions
             else f"モンスーンの言及あり（ABPW {len(mentions)}行）"]
    ub = (digest or {}).get('ulaanbaatar') or {}
    tendency = ub.get('tendency_hpa')
    if tendency is not None:
        if tendency <= -5:
            parts.append('シベリア高気圧は弱化傾向（寒気の南下は弱まる）')
        elif tendency >= 5:
            parts.append('シベリア高気圧は強まる傾向（寒気の南下に注意）')
        else:
            parts.append('シベリア高気圧に大きな変化なし')
    # Per-city limb (2026-11-08): the four cities' own surge indicators,
    # not only the UB grid — a city is named when a single 24-hour window
    # carries both the temperature fall and the pressure rise past the
    # surge thresholds (joint event, review fix 2026-11-09).
    digests = [(label, ((cities or {}).get(name) or {}).get('monsoon') or {})
               for name, label in CITY_ORDER]
    surged = [(label, d) for label, d in digests if d.get('surge_signature')]
    if surged:
        # The 分析 sentence names WHEN each surge window runs (user,
        # 2026-11-12: the windows sat only in the 数値・根拠 column and the
        # analysis left the timing unstated).
        named = []
        for label, d in surged:
            ev = d.get('surge_event') or {}
            if ev.get('start_local') and ev.get('end_local'):
                win = (f"{ev['start_local'].replace('-', '/', 1)}"
                       f"→{ev['end_local'].replace('-', '/', 1)}")
                named.append(f"{label} {win}")
            else:
                named.append(label)
        parts.append(f"{'・'.join(named)}に寒気サージの指標あり"
                     '（同一24h窓で低下・昇圧がともに閾値超過）')
    elif any(d.get('ready') for _, d in digests):
        unassessed = [label for label, d in digests
                      if not (d.get('ready')
                              and d.get('surge_coverage_complete'))]
        if unassessed:
            parts.append(f"{'・'.join(unassessed)}は7日窓不完全につきサージ未評価"
                         '（評価済みの都市に指標なし）')
        else:
            parts.append('4市に寒気サージの指標なし')
    return '。'.join(parts)


def _monsoon_values(cw, digest, cities=None):
    ub = (digest or {}).get('ulaanbaatar') or {}
    parts = []
    if ub.get('max_pressure_hpa') is not None:
        seg = f"UB域 格子最大 {fmt(ub['max_pressure_hpa'], 0)}hPa"
        if ub.get('tendency_hpa') is not None:
            seg += f"・72h傾向 {ub['tendency_hpa']:+.1f}hPa"
        parts.append(seg)
    if (cw or {}).get('monsoon_mentions'):
        parts.append(f"ABPW言及 {len(cw['monsoon_mentions'])}行")
    segs = []
    for name, label in CITY_ORDER:
        m = ((cities or {}).get(name) or {}).get('monsoon') or {}
        if not m.get('ready') or m.get('max_24h_temp_drop_c') is None:
            continue
        seg = (f"{label} 低下 {m['max_24h_temp_drop_c']:+.1f}℃"
               f"（{m.get('drop_end_local')}）"
               f"・昇圧 {m['max_24h_pressure_rise_hpa']:+.1f}hPa"
               f"・北寄り最大 {fmt(m.get('northerly_max_ms'))}m/s")
        _ev = m.get('surge_event')
        if _ev:
            seg += f"（サージ指標 {_ev.get('start_local')}→{_ev.get('end_local')}）"
        elif m.get('surge_signature'):
            seg += '（サージ指標）'
        if not m.get('surge_coverage_complete'):
            seg += '（7日窓不完全）'
        segs.append(seg)
    if segs:
        parts.append('4市指標 ' + '｜'.join(segs))
    return '・'.join(parts) if parts else '―'


ETC_AREAS = (('manchuria', '満洲'), ('sea_of_japan', '日本海'),
             ('east_china_sea', '東シナ海'), ('tokaido', '本州南岸'),
             ('okhotsk', 'オホーツク'))


def _etc_analysis(digest):
    mins = []
    for key, label in ETC_AREAS:
        e = (digest or {}).get(key) or {}
        if e.get('min_pressure_hpa') is not None:
            mins.append((e['min_pressure_hpa'], label))
    if not mins:
        return '―'
    deepest, label = min(mins)
    if deepest < 1000:
        return f"{label}に発達した低気圧の兆候（格子値）"
    if deepest < 1008:
        return f"{label}に低圧部の兆候（格子値）"
    return '発達した低気圧の兆候なし（格子値）'


def _etc_values(digest):
    parts = []
    for key, label in ETC_AREAS:
        e = (digest or {}).get(key) or {}
        if e.get('min_pressure_hpa') is None:
            continue
        at = e.get('min_at_utc') or ''
        when = f"{at[5:7]}/{at[8:10]} {at[11:16]}UTC" if len(at) >= 16 else at
        parts.append(f"{label} 格子最小 {fmt(e['min_pressure_hpa'], 0)}hPa"
                     f"（{_latlon(e.get('latitude'), e.get('longitude'))}・{when}）")
    return '・'.join(parts) if parts else '―'


def _space_analysis(sw):
    sw = sw or {}
    scales = sw.get('scales') or {}
    parts = []
    if scales.get('geomagnetic_G') is not None:
        parts.append(f"地磁気嵐 G{scales['geomagnetic_G']}級")
    elif sw.get('max_g_scale_in_outlook') is not None:
        parts.append(f"地磁気嵐 G{sw['max_g_scale_in_outlook']}級")
    if scales.get('radiation_storm_S') is not None:
        parts.append(f"放射線嵐 S{scales['radiation_storm_S']}級")
    if scales.get('radio_blackout_R') is not None:
        parts.append(f"電波障害 R{scales['radio_blackout_R']}級")
    if sw.get('cme_arrivals'):
        parts.append('CME到達の言及あり')
    if not parts and not sw.get('extreme'):
        return '特記なし'
    head = '・'.join(parts) if parts else '警報級の事象'
    if sw.get('extreme'):
        head += '。4都市の日常生活への直接の影響は限定的'
    ids = sw.get('warning_or_alert_ids') or []
    if ids:
        head += f"（警報・注意報 {'・'.join(ids)}）"
    return head


def _space_values(sw):
    sw = sw or {}
    parts = []
    if sw.get('max_kp_in_outlook') is not None:
        parts.append(f"Kp最大 {fmt(sw['max_kp_in_outlook'])}")
    nict = sw.get('nict') or {}
    if nict.get('kakioka_k_max_24h') is not None:
        parts.append(f"柿岡K最大 {fmt(nict['kakioka_k_max_24h'], 0)}")
    if sw.get('cme_arrivals'):
        parts.append(f"CME到達言及 {len(sw['cme_arrivals'])}件")
        # The snippet is the SWPC product's own line — it carries the
        # expected arrival timing the analysis sentence omits (audit,
        # 2026-11-12: same defect class as the monsoon surge windows).
        parts.append(f"CME原文：{sw['cme_arrivals'][0]}")
    return '・'.join(parts) if parts else '―'


def _track_check_flags(cw):
    checks = (cw or {}).get('track_checks') or {}
    gfs = any(k.startswith('gfs/') and (v or {}).get('ok')
              for k, v in checks.items())
    ecm = any(k.startswith('ecmwf_opendata/') and (v or {}).get('ok')
              for k, v in checks.items())
    return bool(checks), gfs, ecm


def _ecmwf_analysis(cw):
    prod = (cw or {}).get('ecmwf_cyclone_product') or {}
    if prod.get('ok'):
        return 'ECMWFサイクロンプロダクトを取得し進路と照合'
    checks = (cw or {}).get('track_checks') or {}
    assessments = [(v or {}).get('assessment') or {} for v in checks.values()]
    if any(a.get('agreement') == 'consistent_with' for a in assessments):
        return ('ECMWF製品は取得不可。独立モデルサンプルは警報位置と整合'
                '（中心同定ではなく整合性確認）')
    if any(a.get('retrieved') for a in assessments):
        return 'ECMWF製品は取得不可。サンプルは取得済みだが整合は不確定'
    has, gfs, ecm = _track_check_flags(cw)
    if has and (gfs or ecm):
        return 'ECMWF製品は取得不可。進路サンプルで部分的に代替確認'
    return 'ECMWF製品・代替サンプルとも取得不可'


def _ecmwf_values(cw):
    prod = (cw or {}).get('ecmwf_cyclone_product') or {}
    if prod.get('ok'):
        head = f"プロダクト基準時刻 {prod.get('base_time_used')}"
    else:
        head = (f"プロダクト HTTP {prod.get('http_status')}"
                f"・{len(prod.get('attempts') or [])}基準時刻遡及")
    has, gfs, ecm = _track_check_flags(cw)
    if has:
        head += (f"｜進路サンプル GFS {'有' if gfs else 'なし'}"
                 f"・ECMWF開放データ {'有' if ecm else 'なし'}")
    checks = (cw or {}).get('track_checks') or {}
    mins = [(v.get('assessment') or {}).get('point_pressure_min_hpa')
            for v in checks.values()]
    mins = [m for m in mins if m is not None]
    if mins:
        head += f"｜サンプル点圧 最小{fmt(min(mins), 0)}hPa・中心同定なし"
    return head


_SHORT_LABELS = {'Hong Kong': '香港', 'Taipei': '台北',
                 'Tokyo Minato Ward': '東京', 'Takamatsu': '高松'}


def _regional_hazard(calculated):
    """Heavy rain, snow and gusts assessed across the four cities —
    regional hazards beyond the cyclone track (review fix, 2026-11-08).
    Sea state and transport consequences have no city-level model field
    in the bundle; the row says so rather than implying they were
    assessed."""
    cities = (calculated or {}).get('cities') or {}
    rain_day, rain_1h, snow_day, gust = [], [], [], []
    for name, label in _SHORT_LABELS.items():
        c = cities.get(name) or {}
        total = (c.get('precipitation') or {}).get('total')
        if total is not None:
            rain_day.append((total, label))
        screen = c.get('urban_inundation') or {}
        if screen.get('max_1h_mm') is not None:
            rain_1h.append((screen['max_1h_mm'], label))
        snow = (c.get('snowfall') or {}).get('total')
        if snow is not None:
            snow_day.append((snow, label))
        g = ((c.get('report_digest') or {}).get('wind_gust_max_ms'))
        if g is not None:
            gust.append((g, label,
                         (c.get('report_digest') or {}).get(
                             'wind_gust_max_time_local')))
    findings = []
    if rain_day and max(rain_day)[0] >= 50:
        findings.append(f"大雨（{max(rain_day)[1]} 日降水{fmt(max(rain_day)[0], 0)}mm）")
    if rain_1h and max(rain_1h)[0] >= 30:
        findings.append(f"短時間強雨（{max(rain_1h)[1]} 1h{fmt(max(rain_1h)[0], 0)}mm）")
    if snow_day and max(snow_day)[0] >= 5:
        findings.append(f"大雪（{max(snow_day)[1]} 降雪{fmt(max(snow_day)[0], 0)}cm）")
    if gust and max(gust)[0] >= 25:
        _g = max(gust)
        _when = f"・{_g[2].replace('-', '/', 1)}" if _g[2] else ''
        findings.append(f"強風（{_g[1]} 突風{fmt(_g[0], 1)}m/s{_when}）")
    analysis = '・'.join(findings) if findings else '都市計算値の範囲では特記なし'
    analysis += '。波浪・交通影響はモデル値なし（警報類で確認）'
    values = []
    if rain_day:
        values.append(f"日降水最大 {fmt(max(rain_day)[0], 0)}mm（{max(rain_day)[1]}）")
    if rain_1h:
        values.append(f"1h最大 {fmt(max(rain_1h)[0], 0)}mm（{max(rain_1h)[1]}）")
    if snow_day:
        values.append(f"降雪最大 {fmt(max(snow_day)[0], 0)}cm（{max(snow_day)[1]}）")
    if gust:
        _g = max(gust)
        _when = f"・{_g[2].replace('-', '/', 1)}" if _g[2] else ''
        values.append(f"突風最大 {fmt(_g[0], 1)}m/s（{_g[1]}{_when}）")
    surge = []
    for name, label in _SHORT_LABELS.items():
        exc = (((cities.get(name) or {}).get('coastal_assessment') or {})
               .get('exceedance_over_model_tide_m'))
        if exc is not None and exc >= 0.5:
            surge.append((exc, label))
    if surge:
        exc, label = max(surge)
        values.append(f"STOFS水位偏差 +{exc:.1f}m（{label}）・警報閾値未検証")
    return analysis, '・'.join(values) if values else '―'


def render_outlook(raw, calculated):
    """Return (table3_markdown, table4_markdown) for Message 3.

    Analysis first (user correction, 2026-11-08): Table 3 reads each
    system's current JTWC warning text for position / intensity /
    movement and analyzes the warning forecast track for the threat;
    Table 4 separates each topic's 分析 (the conclusion) from its
    数値・根拠 (the structured raw values behind it). The 判定 row is
    the 06:00 run's to append, never rendered here.
    """
    raw = raw or {}
    calculated = calculated or {}
    anchor = _parse_iso(raw.get('collection_utc')) or datetime.now(timezone.utc)
    cw = calculated.get('cyclone_watch') or {}
    systems = cw.get('systems') or []
    if systems:
        t3 = ['| 名称 | 現在位置・強度 | 移動 | 脅威・最接近 |', '|---|---|---|---|']
        t3 += [_system_row(raw, anchor, s) for s in systems]
        table3 = '\n'.join(t3) + '\n'
    else:
        table3 = '熱帯低気圧：なし\n'
    digest = calculated.get('regional_digest') or {}
    sw = calculated.get('space_weather')
    t4 = ['| 項目 | 分析 | 数値・根拠 |', '|---|---|---|',
          f"| 季節風 | {_monsoon_analysis(cw, digest, calculated.get('cities'))} | {_monsoon_values(cw, digest, calculated.get('cities'))} |",
          f"| 温帯低気圧 | {_etc_analysis(digest)} | {_etc_values(digest)} |",
          f"| 地域災害 | {_regional_hazard(calculated)[0]} | {_regional_hazard(calculated)[1]} |",
          f"| 宇宙天気 | {_space_analysis(sw)} | {_space_values(sw)} |",
          f"| ECMWF検証 | {_ecmwf_analysis(cw)} | {_ecmwf_values(cw)} |"]
    return table3, '\n'.join(t4) + '\n'
