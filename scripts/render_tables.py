#!/usr/bin/env python3
"""Render the report's pipe tables (Tables 1 / 2 and the Message-3 outlook
tables 3 / 4) from the bundle.

The tables are deterministic formatting of calculated.json + the city
digests — the user pointed out (2026-11-07) that they belong in the
workflow alongside the chart, leaving the 06:00 run the parts that need
analysis: the official-forecast contradiction check on 天気， the
category-by-category 特記 audit (the generated 特記 is the mechanical
floor — digest flags, visibility, snowfall — to which the run adds the
official-product, pollen, dust and flood/surge limbs), Message 3's
judgement cells (the cyclone 脅威 cells, the monsoon and
extratropical-cyclone interpretation, and the appended 判定 row), and
verification. Cell rules are the cron's settled ones, encoded once here.
"""
import math
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
    if wbgt_max is not None and wbgt_max >= 21 and band:
        return f'{fmt(wbgt_max)} {band}'
    if chill_ok:
        return f"風冷 {fmt(chill['minimum'])}"
    if japan and wbgt_max is not None and band:
        return f'{fmt(wbgt_max)} {band}'
    return '―'


def _precip(c):
    prob = (c.get('precipitation_probability') or {}).get('maximum')
    total = (c.get('precipitation') or {}).get('total')
    snow = (c.get('snowfall') or {}).get('total')
    snowp = (c.get('snow_probability') or {}).get('percent')
    head = f'{fmt(prob, 0)}%' if prob is not None else '―'
    snow_expected = (snow is not None and snow >= 0.1) or (snowp is not None and snowp >= 50)
    parts = [head]
    if total is not None and total > 0:
        parts.append(f'{fmt(total)}mm')
    if snow_expected and snow is not None:
        parts.append(f'雪 {fmt(snow)}cm')
    return ' '.join(parts)


def _wind(c, digest):
    wmax = (c.get('wind_speed_10m') or {}).get('maximum')
    compass = (digest or {}).get('wind_direction_at_max_compass')
    if wmax is None:
        return '―'
    kmh = round(wmax * 3.6)
    return f'{kmh}km/h {compass}' if compass else f'{kmh}km/h'


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
    # Second leader: another pollutant within 10 sub-index points of the
    # leader at the worst hour, read from the city's raw air-quality series.
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
    vis = (c.get('visibility') or {}).get('minimum')
    if vis is not None and vis < 1000:
        tokens.append('霧')
    snow = (c.get('snowfall') or {}).get('total')
    snowp = (c.get('snow_probability') or {}).get('percent')
    if (snow is not None and snow >= 0.1) or (snowp is not None and snowp >= 50):
        tokens.append('雪')
    return '・'.join(tokens) if tokens else '無'


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
# representative point for the sample-based closest-approach floor.
WATCH_AREAS = [('香港', 22.3, 114.2), ('台湾', 23.8, 121.0),
               ('ルソン海峡', 21.3, 120.9), ('小笠原', 27.1, 142.2),
               ('火山列島', 24.8, 141.3), ('本州', 35.7, 139.8)]


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


def _threat_floor(cw, system):
    """Closest sampled approach to a named warning area, from the GFS /
    ECMWF open-data samples along the warning track. A floor only: the
    06:00 run replaces it with the judged threat from the warning text."""
    sid = ((system or {}).get('id') or '').lower()
    best = None
    for name, check in ((cw or {}).get('track_checks') or {}).items():
        if sid and sid not in name:
            continue
        for s in (check or {}).get('samples') or []:
            lat, lon = s.get('latitude'), s.get('longitude')
            if lat is None or lon is None:
                continue
            for label, alat, alon in WATCH_AREAS:
                dist = _haversine_km(lat, lon, alat, alon)
                if best is None or dist < best[0]:
                    best = (dist, label)
    if best is None:
        return '―'
    if best[0] > 1000:
        return '対象地域から1,000km超（サンプル）'
    return f"{best[1]} 約{int(round(best[0] / 10.0)) * 10}km（サンプル）"


def _system_row(cw, system):
    token = CATEGORY_TOKENS.get(system.get('category'), system.get('category') or '')
    name = system.get('name')
    head = f"{name}（{system.get('id')}・{token}）" if name else f"{system.get('id')}（{token}）"
    move = MOVEMENT_TOKENS.get(system.get('movement_text'), system.get('movement_text'))
    motion = []
    if move and system.get('movement_kt') is not None:
        motion.append(f"{move} {system['movement_kt']}kt")
    elif move:
        motion.append(str(move))
    if system.get('max_winds_kt') is not None:
        motion.append(f"最大{system['max_winds_kt']}kt")
    if system.get('gusts_kt') is not None:
        motion.append(f"突風{system['gusts_kt']}kt")
    return (f"| {head} | {_latlon(system.get('latitude'), system.get('longitude'))} | "
            f"{'・'.join(motion) if motion else '―'} | {_threat_floor(cw, system)} |")


def _monsoon_cell(cw, digest):
    mentions = (cw or {}).get('monsoon_mentions') or []
    head = '特記なし' if not mentions else f'ABPWに言及あり（{len(mentions)}行）'
    ub = (digest or {}).get('ulaanbaatar') or {}
    if ub.get('max_pressure_hpa') is not None:
        head += f"｜UB域 格子最大 {fmt(ub['max_pressure_hpa'], 0)}hPa"
        if ub.get('tendency_hpa') is not None:
            head += f"・傾向 {ub['tendency_hpa']:+.1f}"
    return head


def _etc_cell(digest):
    parts = []
    for key, label in (('manchuria', '満洲'), ('sea_of_japan', '日本海')):
        e = (digest or {}).get(key) or {}
        if e.get('min_pressure_hpa') is None:
            continue
        at = e.get('min_at_utc') or ''
        when = f"{at[5:7]}/{at[8:10]} {at[11:16]}UTC" if len(at) >= 16 else at
        parts.append(f"{label} 格子最小 {fmt(e['min_pressure_hpa'], 0)}hPa"
                     f"（{_latlon(e.get('latitude'), e.get('longitude'))}・{when}）")
    return '・'.join(parts) if parts else '―'


def _space_cell(sw):
    sw = sw or {}
    if not sw.get('extreme'):
        return '特記なし'
    ids = sw.get('warning_or_alert_ids') or []
    return (f"G{sw.get('max_g_scale_in_outlook')}級・Kp {fmt(sw.get('max_kp_in_outlook'))}"
            f"（警報・注意報 {'・'.join(ids) if ids else 'なし'}）")


def _ecmwf_cell(cw):
    prod = (cw or {}).get('ecmwf_cyclone_product') or {}
    if prod.get('ok'):
        head = f"サイクロンプロダクト 取得可（基準時刻 {prod.get('base_time_used')}）"
    else:
        head = (f"サイクロンプロダクト 取得不可（HTTP {prod.get('http_status')}"
                f"・{len(prod.get('attempts') or [])}基準時刻）")
    checks = (cw or {}).get('track_checks') or {}
    if checks:
        gfs = any(k.startswith('gfs/') and (v or {}).get('ok') for k, v in checks.items())
        ecm = any(k.startswith('ecmwf_opendata/') and (v or {}).get('ok')
                  for k, v in checks.items())
        head += (f"｜進路サンプル GFS {'有' if gfs else 'なし'}"
                 f"・ECMWF開放データ {'有' if ecm else 'なし'}")
    return head


def render_outlook(raw, calculated):
    """Return (table3_markdown, table4_markdown) for Message 3.

    Table 3: one row per active tropical cyclone, or the single settled
    line when there are none. Table 4: the fixed-row special outlook;
    the 判定 row is the 06:00 run's to append, never rendered here.
    """
    calculated = calculated or {}
    cw = calculated.get('cyclone_watch') or {}
    systems = cw.get('systems') or []
    if systems:
        t3 = ['| 名称 | 位置 | 移動・強度 | 脅威 |', '|---|---|---|---|']
        t3 += [_system_row(cw, s) for s in systems]
        table3 = '\n'.join(t3) + '\n'
    else:
        table3 = '熱帯低気圧：なし\n'
    digest = calculated.get('regional_digest') or {}
    t4 = ['| 項目 | 状況 | 対象・根拠 |', '|---|---|---|',
          f"| 季節風 | {_monsoon_cell(cw, digest)} | JTWC ABPW・EC46地域格子 |",
          f"| 温帯低気圧 | {_etc_cell(digest)} | EC46地域格子（格子値） |",
          f"| 宇宙天気 | {_space_cell(calculated.get('space_weather'))} | SWPC・NICT |",
          f"| ECMWF検証 | {_ecmwf_cell(cw)} | ECMWF OpenCharts・NOAA GFS・ECMWF Open Data |"]
    return table3, '\n'.join(t4) + '\n'
