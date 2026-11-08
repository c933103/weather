#!/usr/bin/env python3
"""Chart data derivation for the East Asia weather chart (drawing is in R).

Everything semantic about the chart lives here and is unit-tested: the
verified ECMWF hourly extraction, the local-day / noon-to-noon extrema
windows with their coverage audit, the member-extrema precedence, the run
builder, the per-day icons (modal daytime WMO code; ensemble-sourced days
from member probabilities), the IFS-ensemble confidence grades and the
source-cascade note. build_chart_data() emits the JSON document that
scripts/render_chart.R draws with ggplot2 — the R script makes no data
decisions of its own.
"""
from __future__ import annotations
from datetime import datetime, date, timedelta, time, timezone
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
SITES = {
    'Tokyo Minato Ward': ('東京都港区', ZoneInfo('Asia/Tokyo')),
    'Hong Kong': ('香港', ZoneInfo('Asia/Hong_Kong')),
}

def parse_utc(text):
    if isinstance(text, (int, float)):
        return datetime.fromtimestamp(text, tz=timezone.utc)
    d = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    if d.tzinfo is None:
        # Collector always asks for timezone=GMT; naive timestamps from it are UTC.
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def get_hourly(root, city):
    rec = (root.get('cities') or {}).get(city, {})
    hourly = rec.get('chart_hourly') or rec.get('hourly') or rec.get('forecast_hourly') or {}
    times = hourly.get('time') or hourly.get('time_utc') or []
    values = hourly.get('temperature_2m') or []
    model = hourly.get('model') or rec.get('model') or ''
    if not model.startswith(('ecmwf','ifs','aifs')):
        raise ValueError(f'{city}: hourly source/model is not verified as ECMWF ({model!r}).')
    if len(times) != len(values) or not times:
        raise ValueError(f'{city}: missing/inconsistent ECMWF hourly time and temperature arrays.')
    tmins = hourly.get('temperature_2m_min') or []
    tmaxs = hourly.get('temperature_2m_max') or []
    markers = hourly.get('sample_model') or []
    arr = []
    for idx,(t,v) in enumerate(zip(times,values)):
        if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v):
            vmin = tmins[idx] if idx < len(tmins) and isinstance(tmins[idx], (int, float)) else None
            vmax = tmaxs[idx] if idx < len(tmaxs) and isinstance(tmaxs[idx], (int, float)) else None
            marker = markers[idx] if idx < len(markers) else None
            arr.append((parse_utc(t), float(v), vmin, vmax, marker))
    arr = sorted(set(arr),key=lambda z:z[0])
    if len(arr) < 8:
        raise ValueError(f'{city}: not enough true time-stamped ECMWF samples.')
    return arr,model


def enough_coverage(points, start, end):
    """Boundary/step audit; allows 1/3/6-hour native or interpolated series."""
    valid=[(t,v) for t,v in points if start <= t < end]
    if len(valid)<3: return False,valid
    # The samples must reach both ends of the relevant *day/noon-to-noon* window.
    # 6h native sampling is accepted; longer gaps invalidate a daily extremum.
    if (valid[0][0]-start).total_seconds() > 6*3600: return False,valid
    if (end-valid[-1][0]).total_seconds() > 7*3600: return False,valid
    gaps=[(valid[j][0]-valid[j-1][0]).total_seconds() for j in range(1,len(valid))]
    if gaps and max(gaps)>7*3600: return False,valid
    return True,valid


def extrema_by_date(points, day0, site_tz, max_days=28, member_extrema=None):
    # All extrema windows are *local* calendar day/noon windows. Their graphical
    # positions are slot indices regardless of their actual forecast hour.
    # Points are (stamp, value, interval_min, interval_max, sample_model).
    # Precedence per window: (1) on majority-ensemble-sourced windows, the
    # mean of the members' own extrema (member_extrema) — an extremum of the
    # ensemble-mean series is biased at every lead; (2) the source's
    # per-interval extremes (EC46's temperature_2m_min/max fields), which at
    # 6-hourly sampling recover the mean series' sub-interval peaks;
    # (3) the sampled values themselves (the deterministic tier).
    points=[(p[0],p[1],p[2] if len(p)>2 else None,p[3] if len(p)>3 else None,
             p[4] if len(p)>4 else None) for p in points]
    stripped=[(t,v) for t,v,_,_,_ in points]
    member_extrema = member_extrema or {}

    def ensemble_majority(window_points):
        marks=[m for *_, m in window_points if m]
        return bool(marks) and sum(1 for m in marks if 'ensemble' in m) * 2 > len(marks)

    highs={}; night_lows={}
    for i in range(max_days):
        d=day0+timedelta(days=i)
        override=member_extrema.get(d.isoformat()) or {}
        start=datetime.combine(d,time.min,tzinfo=site_tz).astimezone(timezone.utc)
        stop=start+timedelta(days=1)
        ok,_=enough_coverage(stripped,start,stop)
        if ok:
            window=[p for p in points if start<=p[0]<stop]
            if override.get('high') is not None and ensemble_majority(window):
                highs[i]=override['high']
            else:
                highs[i]=max(p[3] if p[3] is not None else p[1] for p in window)
        # Night separating the labelled day and following day:
        # NOON THIS DAY -> NOON NEXT DAY (never calendar-day minima).
        ns=datetime.combine(d,time(12,0),tzinfo=site_tz).astimezone(timezone.utc)
        ne=ns+timedelta(days=1)
        ok,_=enough_coverage(stripped,ns,ne)
        if ok:
            window=[p for p in points if ns<=p[0]<ne]
            if override.get('night_low') is not None and ensemble_majority(window):
                night_lows[i]=override['night_low']
            else:
                night_lows[i]=min(p[2] if p[2] is not None else p[1] for p in window)
    return highs,night_lows


def extrema_runs(highs,lows):
    """Maximal runs of alternating extrema with no missing slot between them.

    Points are (x, value, kind) with highs at integer x and night lows at
    x+0.5. A gap of more than half a slot (a missing day or night) breaks the
    run. Crucially the FINAL night low (i=15, x=15.5) belongs to the last run
    even though no day high follows it — the previous triplet-based builder
    dropped it, leaving the chart short of t+384.
    """
    pts=[]
    for i in sorted(set(highs)|set(lows)):
        if i in highs:pts.append((float(i),highs[i],'high'))
        if i in lows:pts.append((i+.5,lows[i],'low'))
    pts.sort()
    runs=[];current=[]
    for p in pts:
        if current and p[0]-current[-1][0]>.5+1e-9:
            runs.append(current);current=[]
        current.append(p)
    if current:runs.append(current)
    return runs

def wmo_emoji(c):
    if c is None:return '―'
    if c in (0,):return '☀'
    if c in (1,2):return '🌤'
    if c==3:return '☁'
    if c in (45,48):return '🌫'
    if c in (51,53,55,56,57,61,63,65,66,67,80,81,82):return '🌧'
    if c in (71,73,75,77,85,86):return '❄'
    if c in (95,96,99):return '⛈'
    return '―'


CASCADE_LABELS={'ecmwf_ifs_9km':'ECMWF IFS 9km',
                'ecmwf_ifs025_ensemble_mean':'IFSアンサンブル平均',
                'ecmwf_ec46_ensemble_mean_36km':'EC46アンサンブル平均'}


def cascade_segments(row,tz,day0=None):
    """Contiguous source-model segments of the chart series, in local dates."""
    hr=row.get('chart_hourly') or {}
    ts=hr.get('time') or []; models=hr.get('sample_model') or []
    segments=[]
    for t,m in zip(ts,models):
        if not m:continue
        d=parse_utc(t).astimezone(tz).date()
        if day0 is not None and not (day0<=d<=day0+timedelta(days=28)):
            continue
        if segments and segments[-1][0]==m:
            segments[-1]=(m,segments[-1][1],d)
        else:
            segments.append((m,d,d))
    return segments


def cascade_note(raws,day0=None):
    """The reliability cascade actually used, labelled beneath the chart."""
    per_city={}
    for city in ('Tokyo Minato Ward','Hong Kong'):
        segs=cascade_segments(raws.get(city) or {},SITES[city][1],day0)
        per_city[city]=[(CASCADE_LABELS.get(m,m),a,b) for m,a,b in segs]
    def fmt(segs):
        return ' → '.join(f'{a.strftime("%m/%d")}–{b.strftime("%m/%d")} {label}' for label,a,b in segs)
    tokyo,hk=per_city['Tokyo Minato Ward'],per_city['Hong Kong']
    if not tokyo and not hk:return ''
    if tokyo==hk:
        base=f'気温データ源（信頼度の高い順に接続）：{fmt(tokyo)}'
    else:
        parts=[]
        if tokyo:parts.append(f'東京 {fmt(tokyo)}')
        if hk:parts.append(f'香港 {fmt(hk)}')
        base='気温データ源（信頼度の高い順に接続）：'+'｜'.join(parts)
    labels=[label for segs in per_city.values() for label,_,_ in segs]
    if any('アンサンブル' in label for label in labels):
        base+='｜アンサンブル区間の最高・最低はメンバー極値の平均（平均場の極値は使わない）・日々の変動は抑制・天気はメンバー確率による'
    return base


def member_probability_icon(probs):
    """Day icon from ensemble member probabilities (EC46 or IFS ENS).

    Snow when at least half the members carry >= 0.5 cm; rain when at least
    half carry >= 10 mm or >= 1 mm; a rain-leaning icon at >= 30% for 1 mm;
    otherwise the members' mean daytime cloud cover grades the dry icon.
    """
    if not probs:return '―'
    if (probs.get('p_snow') or 0)>=0.5:return '❄'
    if (probs.get('p_rain_10mm') or 0)>=0.5 or (probs.get('p_rain_1mm') or 0)>=0.5:return '🌧'
    if (probs.get('p_rain_1mm') or 0)>=0.3:return '🌦'
    cloud=probs.get('daytime_cloud_mean')
    if cloud is None:return '☁'
    return '☀' if cloud<20 else '🌤' if cloud<50 else '⛅' if cloud<80 else '☁'


ec46_probability_icon = member_probability_icon


def daily_icon(row,day,tz):
    hr=row.get('chart_hourly') or row.get('hourly') or {}
    ts=hr.get('time') or [];wc=hr.get('weather_code') or []
    if len(ts)!=len(wc):return '―'
    # EC46-sourced days never use the ensemble mean's weather_code (it reads
    # as rain almost every day); their icons come from member probabilities.
    models=hr.get('sample_model') or []
    if len(models)==len(ts):
        day_models=[m for t,m in zip(ts,models) if parse_utc(t).astimezone(tz).date()==day]
        if day_models and sum(1 for m in day_models if m and 'ec46' in m)>len(day_models)/2:
            return member_probability_icon((row.get('ec46_daily') or {}).get(day.isoformat()))
        if day_models and sum(1 for m in day_models if m and 'ifs025_ensemble' in m)>len(day_models)/2:
            # IFS-ENS-sourced days likewise never use a mean-state code (the
            # ENS tier carries none anyway); member probabilities instead.
            return member_probability_icon(
                ((row.get('temperature_ensemble') or {}).get('daily') or {}).get(day.isoformat()))
    samples=[]
    for t,c in zip(ts,wc):
        d=parse_utc(t).astimezone(tz)
        if d.date()==day and 9<=d.hour<=18 and isinstance(c,(int,float)):
            samples.append(int(c))
    if not samples:return '―'
    # Daily weather label based on daytime modal code: indicative, not official.
    from collections import Counter
    return wmo_emoji(Counter(samples).most_common(1)[0][0])


# Colour-emoji assets (scripts/assets/emoji/<codepoint>.png), extracted once
# from Noto Color Emoji — the same glyphs the retired matplotlib renderer
# composited with PIL. The R renderer draws these PNGs, so no emoji font is
# needed at chart time in either the PNG or the SVG.
EMOJI_ASSETS = {'☀': '2600', '🌤': '1f324', '☁': '2601', '🌫': '1f32b',
                '🌧': '1f327', '❄': '2744', '⛈': '26c8', '🌦': '1f326',
                '⛅': '26c5'}

CITY_ORDER = ('Tokyo Minato Ward', 'Hong Kong')


def build_chart_data(raw, target_day):
    """Derive everything the R chart draws. Returns (chart_data, reports).

    Raises on the same conditions the retired in-process renderer raised:
    a synthetic/test fixture, a non-ECMWF hourly source, or no verified
    consecutive day-high / night-low windows at all.
    """
    if raw.get('synthetic') is True or raw.get('test_fixture') is True:
        raise ValueError('Synthetic data cannot be issued as a weather chart.')
    d0 = date.fromisoformat(target_day)
    try:
        from compute import chart_confidence_for_city
    except Exception:
        chart_confidence_for_city = None
    cities = []
    reports = {}
    bounds = []
    total_segments = 0
    emoji_expected = 0
    for city in CITY_ORDER:
        arr, model = get_hourly(raw, city)
        row = (raw.get('cities') or {}).get(city, {})
        highs, lows = extrema_by_date(arr, d0, SITES[city][1],
                                      member_extrema=row.get('member_extrema'))
        runs = extrema_runs(highs, lows)
        total_segments += len(runs)
        bounds += list(highs.values()) + list(lows.values())
        grades = {}
        if chart_confidence_for_city is not None:
            try:
                grades = chart_confidence_for_city(row, d0, days=28)
            except Exception:
                grades = {}
        icons = []
        grade_list = []
        for i in range(28):
            dd = d0 + timedelta(days=i)
            ch = daily_icon(row, dd, SITES[city][1])
            icons.append({'emoji': ch, 'asset': EMOJI_ASSETS.get(ch)})
            if ch != '―':
                emoji_expected += 1
            grade_list.append((grades.get(dd.isoformat()) or {}).get('grade'))
        reports[city] = {'high_days': len(highs), 'noon_to_noon_nights': len(lows),
                         'rendered_segments': len(runs),
                         'forecast_first_utc': arr[0][0].isoformat(),
                         'forecast_last_utc': arr[-1][0].isoformat(), 'model': model}
        cities.append({'key': city, 'label': SITES[city][0],
                       'highs': [[i, float(highs[i])] for i in sorted(highs)],
                       'lows': [[i, float(lows[i])] for i in sorted(lows)],
                       'runs': [[[x, y, k] for x, y, k in run] for run in runs],
                       'icons': icons, 'grades': grade_list})
    if not total_segments:
        raise RuntimeError('No verified consecutive day-high / noon-to-noon night-low windows to plot.')
    raws = {city: (raw.get('cities') or {}).get(city, {}) for city in CITY_ORDER}
    data = {'schema': 'east_asia_weather_chart_data_v1',
            'target_date': target_day,
            'dates': [(d0 + timedelta(days=i)).strftime('%m/%d') for i in range(28)],
            'y_min': math.floor(min(bounds) - 2), 'y_max': math.ceil(max(bounds) + 2),
            'cascade_note': cascade_note(raws, d0),
            'cities': cities, 'emoji_expected': emoji_expected}
    reports['emoji_composited'] = emoji_expected
    return data, reports


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--target-date-jst', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    raw = json.loads(Path(args.input).read_text(encoding='utf-8'))
    data, reports = build_chart_data(raw, args.target_date_jst)
    Path(args.output).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(reports, ensure_ascii=False, indent=2))
