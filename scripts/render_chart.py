#!/usr/bin/env python3
"""East Asia weather chart v3: semantic day highs + BETWEEN-DAYS night lows.

Inputs: actual numerical temperature time series (UTC valid timestamps) for
Hong Kong and Tokyo Minato Ward. No daily-summary-only or synthetic data fallback.

Each day D (shown at date tick x=D): high = maximum for the local day D.
Each NIGHT between D and D+1 (x=D+0.5): low = minimum over the local
noon(D) to noon(D+1) window, as requested. The x coordinates are visual
DATE SLOTS, not claims about the hour of the extrema. This differs from
placing low at 05:30 of calendar day D.

The chart draws TWO lines per city: a solid line through the day highs and a
dashed line through the night lows, with the high/low envelope as a continuous
translucent fill_between behind them. The envelope spans the full run from
the first extremum to the last (each boundary curve held flat across the
half-slot at the run edges), so the first and last 24 hours are shaded just
like the middle. No invented extrema and no interpolation across a missing
day or night: a missing extremum breaks its own line and the envelope run.
PCHIP smooths *only* between verified sampled extrema.

Example:
    python eastasia_weather_chart_renderer.py \
      --input /mnt/data/2026-11-08_raw.json \
      --target-date-jst 2026-11-08 \
      --output /mnt/data/2026-11-08_chart.png
"""
from __future__ import annotations
import argparse
from datetime import datetime, date, timedelta, time, timezone
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.interpolate import PchipInterpolator
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

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
    arr = []
    for idx,(t,v) in enumerate(zip(times,values)):
        if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v):
            vmin = tmins[idx] if idx < len(tmins) and isinstance(tmins[idx], (int, float)) else None
            vmax = tmaxs[idx] if idx < len(tmaxs) and isinstance(tmaxs[idx], (int, float)) else None
            arr.append((parse_utc(t), float(v), vmin, vmax))
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


def extrema_by_date(points, day0, site_tz, max_days=28):
    # All extrema windows are *local* calendar day/noon windows. Their graphical
    # positions are slot indices regardless of their actual forecast hour.
    # Points are (stamp, value, interval_min, interval_max): where the source
    # provides per-interval extremes (EC46's temperature_2m_min/max fields),
    # the day high / night low use them instead of the sampled values, which
    # at 6-hourly sampling miss the sub-interval peaks of the mean series.
    points=[(p[0],p[1],p[2] if len(p)>2 else None,p[3] if len(p)>3 else None) for p in points]
    stripped=[(t,v) for t,v,_,_ in points]
    highs={}; night_lows={}
    for i in range(max_days):
        d=day0+timedelta(days=i)
        start=datetime.combine(d,time.min,tzinfo=site_tz).astimezone(timezone.utc)
        stop=start+timedelta(days=1)
        ok,_=enough_coverage(stripped,start,stop)
        if ok:
            window=[(v,vx) for t,v,_,vx in points if start<=t<stop]
            highs[i]=max(vx if vx is not None else v for v,vx in window)
        # Night separating the labelled day and following day:
        # NOON THIS DAY -> NOON NEXT DAY (never calendar-day minima).
        ns=datetime.combine(d,time(12,0),tzinfo=site_tz).astimezone(timezone.utc)
        ne=ns+timedelta(days=1)
        ok,_=enough_coverage(stripped,ns,ne)
        if ok:
            window=[(v,vn) for t,v,vn,_ in points if ns<=t<ne]
            night_lows[i]=min(vn if vn is not None else v for v,vn in window)
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


def draw_city(ax,highs,lows,color,name):
    # Two-line design (the user's settled chart style): a SOLID line through
    # the day highs at the date positions and a DASHED line through the
    # night lows half a day later (each low is the local noon-to-noon
    # minimum), with the semi-transparent high/low envelope behind them. The
    # envelope spans each run edge to edge: each boundary curve is held flat
    # across the half-slot beyond its own end extrema, so the shading covers
    # the first slot (from t+0) and the last night (to t+384) as well.
    drawn=0
    labelled={'high':False,'low':False}
    for run in extrema_runs(highs,lows):
        hpts=[(x,y) for x,y,k in run if k=='high']
        lpts=[(x,y) for x,y,k in run if k=='low']
        x0,x1=run[0][0],run[-1][0]
        if hpts and lpts and x1>x0:
            grid=np.linspace(x0,x1,max(60,int((x1-x0)*60)))
            def curve(pts):
                if len(pts)==1:
                    return np.full(grid.shape,float(pts[0][1]))
                px=np.array([p[0] for p in pts]);py=np.array([p[1] for p in pts])
                return PchipInterpolator(px,py,extrapolate=False)(np.clip(grid,px[0],px[-1]))
            upper,lower=curve(hpts),curve(lpts)
            allowed=np.isfinite(upper)&np.isfinite(lower)&(upper>=lower)
            ax.fill_between(grid,lower,upper,where=allowed,alpha=.13,color=color,zorder=1,linewidth=0)
        def line(pts,ms,kind,**kw):
            if len(pts)>=2:
                px=np.array([p[0] for p in pts]);py=np.array([p[1] for p in pts])
                sample=np.linspace(px[0],px[-1],max(60,int((px[-1]-px[0])*60)))
                suffix=' 最高' if kind=='high' else ' 最低'
                ax.plot(sample,PchipInterpolator(px,py,extrapolate=False)(sample),
                        label=f'{name}{suffix}' if not labelled[kind] else '_nolegend_',**kw)
                labelled[kind]=True
            if pts:
                ax.plot([p[0] for p in pts],[p[1] for p in pts],'o',ms=ms,color=color,zorder=5,alpha=.95)
        line(hpts,3.8,'high',lw=2.6,color=color,solid_capstyle='round',zorder=4)
        line(lpts,3.2,'low',lw=2.1,color=color,ls=(0,(4,2)),dash_capstyle='round',zorder=4)
        drawn+=1
    return drawn


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
    if any('EC46' in label for label in labels):
        base+='｜EC46区間はアンサンブル平均（日々の変動は抑制）・天気はメンバー確率による'
    return base


def ec46_probability_icon(probs):
    """Day icon from EC46 member probabilities (see extract_ec46_daily).

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
            return ec46_probability_icon((row.get('ec46_daily') or {}).get(day.isoformat()))
    samples=[]
    for t,c in zip(ts,wc):
        d=parse_utc(t).astimezone(tz)
        if d.date()==day and 9<=d.hour<=18 and isinstance(c,(int,float)):
            samples.append(int(c))
    if not samples:return '―'
    # Daily weather label based on daytime modal code: indicative, not official.
    from collections import Counter
    return wmo_emoji(Counter(samples).most_common(1)[0][0])


def find_emoji_font():
    """Noto Color Emoji path, or None (matplotlib cannot render CBDT colour
    emoji as text — the icon rows fell back to faint monochrome glyphs, the
    regression the user flagged; the PNG therefore composites real colour
    emoji with PIL, as the original local charts did)."""
    import glob
    for cand in ['/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf',
                 *glob.glob('/usr/share/fonts/**/NotoColorEmoji.ttf', recursive=True)]:
        if Path(cand).exists():
            return cand
    return None


def paste_emojis(png_path, slots, font_path):
    """Composite colour weather emoji onto the saved PNG at figure-fraction
    slot positions [(fx, fy, char)]. Returns the number pasted."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.open(png_path).convert('RGBA')
    font = ImageFont.truetype(font_path, 109)
    pasted = 0
    for fx, fy, ch in slots:
        if not ch or ch == '―':
            continue
        tile = Image.new('RGBA', (160, 160), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((8, 8), ch, font=font, embedded_color=True)
        bbox = tile.getbbox()
        if not bbox:
            continue
        glyph = tile.crop(bbox)
        scale = 46 / glyph.height
        glyph = glyph.resize((max(1, round(glyph.width * scale)), 46), Image.LANCZOS)
        cx = fx * img.width - 22
        cy = (1 - fy) * img.height
        img.alpha_composite(glyph, (int(cx - glyph.width / 2), int(cy - glyph.height / 2)))
        pasted += 1
    img.convert('RGB').save(png_path)
    return pasted


def render(raw,target_day,out_path):
    d0=date.fromisoformat(target_day)
    plt.rcParams.update({'font.family':['Noto Sans CJK JP','Noto Sans Symbols2','DejaVu Sans'],'axes.unicode_minus':False})
    fig=plt.figure(figsize=(20,7.2),dpi=170)
    left=.125; width=.845
    ax=fig.add_axes([left,.27,width,.60])
    colors=plt.rcParams['axes.prop_cycle'].by_key()['color']
    reports={};raws={};bounds=[];total=0
    for i,city in enumerate(('Tokyo Minato Ward','Hong Kong')):
        arr,model=get_hourly(raw,city)
        # Explicitly refuse any file that is declared synthetic/test fixture.
        if raw.get('synthetic') is True or raw.get('test_fixture') is True:
            raise ValueError('Synthetic data cannot be issued as a weather chart.')
        highs,lows=extrema_by_date(arr,d0,SITES[city][1])
        completed=draw_city(ax,highs,lows,colors[i],SITES[city][0])
        reports[city]={'high_days':len(highs),'noon_to_noon_nights':len(lows),'rendered_segments':completed,
                       'forecast_first_utc':arr[0][0].isoformat(),'forecast_last_utc':arr[-1][0].isoformat(),'model':model}
        if completed:
            total+=completed;bounds+=list(highs.values())+list(lows.values())
        raws[city]=(raw.get('cities') or {}).get(city,{})
    if not total:raise RuntimeError('No verified consecutive day-high / noon-to-noon night-low windows to plot.')
    bmin=math.floor(min(bounds)-2);bmax=math.ceil(max(bounds)+2)
    ax.set_xlim(-.08,28.0);ax.set_ylim(bmin,bmax)
    ax.set_ylabel('気温（℃）',fontsize=11.5)
    ax.set_xticks(range(28))
    ax.set_xticklabels([(d0+timedelta(days=i)).strftime('%m/%d') for i in range(28)],fontsize=11)
    ax.set_yticks(np.arange(math.ceil(bmin/5)*5,bmax+1,5))
    ax.set_yticks(np.arange(bmin,bmax+1,1),minor=True)
    ax.grid(axis='y',which='minor',alpha=.11,lw=.5)
    ax.grid(axis='y',which='major',alpha=.20,lw=.7)
    ax.set_axisbelow(True)
    ax.spines[['top','right']].set_visible(False)
    ax.tick_params(length=0,axis='x',pad=8)
    ax.tick_params(axis='y',labelsize=10.5)
    ax.legend(loc='upper right',frameon=False,ncol=2,fontsize=11)
    fig.text(left,.947,'香港・東京都港区｜28日間気温予報',fontsize=20,weight='bold')
    fig.text(left,.908,'実線：日中最高（当日）／破線：夜間最低（日付間・当日正午～翌日正午の最低）｜淡色＝高低の連続帯',fontsize=10.8)
    fig.text(left,.882,'日付位置は時刻を示さない。下段の自信度はIFSアンサンブルの日最高気温の拡散より。気温の接続順は下部のデータ源注記参照。',fontsize=9.6,color='#595959')
    emoji_font=find_emoji_font()
    icon_slots=[]
    for row,city in enumerate(('Tokyo Minato Ward','Hong Kong')):
        y=.16-row*.055
        fig.text(.024,y,SITES[city][0],fontsize=13,weight='bold',va='center',color=colors[row])
        src=raws[city]
        # Confidence grades come from the IFS ensemble spread of daily maximum
        # temperature (compute.chart_confidence_for_city). When the bundle
        # carries no temperature ensemble, the row honestly stays 未評価.
        grades={}
        try:
            from compute import chart_confidence_for_city
            grades=chart_confidence_for_city(src,d0,days=28)
        except Exception:
            grades={}
        for i in range(28):
            dd=d0+timedelta(days=i)
            cc=daily_icon(src,dd,SITES[city][1])
            grade=(grades.get(dd.isoformat()) or {}).get('grade')
            cx=left+width*(i/28)
            if emoji_font and cc!='―':
                # Colour emoji are composited onto the PNG afterwards; here
                # only the grade text is drawn, right of the emoji slot.
                icon_slots.append((cx,y,cc))
                fig.text(cx+.005,y,f'/{grade}' if grade else '/未評価',fontsize=10.5,ha='left',va='center')
            elif emoji_font:
                # No weather code for this day in the bundle: show the
                # explicit no-data marker as text instead of a blank slot.
                fig.text(cx,y,f'―/{grade}' if grade else '―/未評価',fontsize=10.5,ha='center',va='center')
            else:
                val=f'{cc}/{grade}' if grade else f'{cc}/未評価'
                fig.text(cx,y,val,fontsize=10,ha='center',va='center')
    note=cascade_note(raws,d0)
    if note:
        fig.text(left,.068,note,fontsize=9.8,color='#444444')
    fig.text(left,.04,'各日の最高：その日の予報最大値　／　夜間の最低：両日正午に挟まれた24時間の予報最小値',fontsize=10,color='#555555')
    out=Path(out_path);out.parent.mkdir(parents=True,exist_ok=True)
    # The PNG is saved at the exact canvas size (no tight crop) so the icon
    # slots' figure fractions map 1:1 onto pixels for emoji compositing.
    fig.savefig(out,facecolor='white')
    # SVG twin of the same figure (vector deliverable; its icons stay text
    # glyphs — the colour compositing applies to the delivered PNG only).
    fig.savefig(out.with_suffix('.svg'),format='svg',bbox_inches='tight',facecolor='white')
    plt.close(fig)
    if emoji_font and icon_slots:
        reports['emoji_composited'] = paste_emojis(out, icon_slots, emoji_font)
    from PIL import Image
    with Image.open(out) as im:
        im.verify()
    if out.stat().st_size<5000:raise RuntimeError('Suspiciously empty PNG')
    return reports

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',required=True)
    parser.add_argument('--target-date-jst',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    raw=json.loads(Path(args.input).read_text(encoding='utf-8'))
    print(json.dumps(render(raw,args.target_date_jst,args.output),ensure_ascii=False,indent=2))
