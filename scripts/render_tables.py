#!/usr/bin/env python3
"""Render the report's pipe tables (Table 1 / Table 2) from the bundle.

The tables are deterministic formatting of calculated.json + the city
digests — the user pointed out (2026-11-07) that they belong in the
workflow alongside the chart, leaving the 06:00 run the parts that need
analysis: the official-forecast contradiction check on 天気， the
category-by-category 特記 audit (the generated 特記 is the mechanical
floor — digest flags, visibility, snowfall — to which the run adds the
official-product, pollen, dust and flood/surge limbs), Message 3, and
verification. Cell rules are the cron's settled ones, encoded once here.
"""
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
