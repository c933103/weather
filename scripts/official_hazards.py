"""Normalize official warnings at GitHub collection time, without inferring safety.

Each provider keeps its native area, level, issuance and valid times.  A
downloaded page is not an active warning until it is parsed and its exact
municipality applicability and validity have been checked.
"""
import ast
import datetime as dt
import re
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
TPE = ZoneInfo("Asia/Taipei")
JMA_AREAS = {"tokyo_minato": ("tokyo", "1310300"),
             "takamatsu": ("kagawa", "3720100")}
JMA_NAMES = {"10": "大雨注意報", "12": "大雪注意報", "14": "雷注意報",
             "15": "強風注意報", "16": "波浪注意報", "17": "融雪注意報",
             "18": "洪水注意報", "19": "高潮注意報", "29": "洪水警報",
             "30": "大雨警報"}
TAIWAN_AREAS = ("臺北市", "新北市", "基隆市", "桃園市", "新竹市", "新竹縣",
                "苗栗縣", "臺中市", "彰化縣", "南投縣", "雲林縣",
                "嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣",
                "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣", "金門縣", "連江縣")


def _time(value, zone=UTC):
    if not value:
        return None
    try:
        x = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            x = dt.datetime.strptime(str(value), "%Y/%m/%d %H:%M")
        except ValueError:
            try:
                x = dt.datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    return (x.replace(tzinfo=zone) if x.tzinfo is None else x).astimezone(UTC)


def _instant(source):
    return _time(source.get("issued_at_utc") or source.get("fetched_at_utc"))


def _source_ok(s):
    return isinstance(s, dict) and s.get("ok") is True and (
        (s.get("freshness") or {}).get("status") != "stale")


def _jma_item(kind):
    status = kind.findtext("./{*}Status") or ""
    code = kind.findtext("./{*}Code") or ""
    return {"code": code, "name": kind.findtext("./{*}Name") or JMA_NAMES.get(code, code),
            "status": status}


def parse_jma_xml(source, code, as_of):
    """Exact municipality, never the prefecture's 'FullStatus: 一部'."""
    result = {"status": "missing", "source": "jma_xml", "active": [],
              "cancelled": [], "issued_at_utc": None}
    if not _source_ok(source) or not isinstance(source.get("response"), str):
        return result
    try:
        root = ET.fromstring(source["response"])
    except ET.ParseError:
        return {**result, "status": "invalid", "reason": "invalid XML"}
    head = root.find(".//{*}Head")
    issued = _time(head.findtext("./{*}ReportDateTime") if head is not None else None)
    if issued is None:
        return {**result, "status": "invalid", "reason": "no issue time"}
    result["issued_at_utc"] = issued.isoformat()
    if issued > as_of + dt.timedelta(minutes=5) or as_of - issued > dt.timedelta(hours=12):
        return {**result, "status": "stale", "reason": "warning XML older than 12 h or future-dated"}
    if head is not None and head.findtext("./{*}InfoType") in ("取消", "取消し"):
        return {**result, "status": "caution", "reason": "report cancellation; need latest full state"}
    items = [item for item in root.findall(".//{*}Item")
             if item.findtext("./{*}Area/{*}Code") == code]
    if not items:
        return {**result, "status": "unassessed",
                "reason": "municipal area absent; parent-region alerts do not apply automatically"}
    for item in items:
        for kind in item.findall("./{*}Kind"):
            entry = _jma_item(kind)
            if entry["status"] in ("解除", "無効", "取消"):
                result["cancelled"].append(entry)
            elif entry["name"] and entry["status"] not in ("発表警報・注意報はなし",):
                result["active"].append(entry)
    result["status"] = "ready"
    result["area_code"] = code
    return result


def parse_jma_json(source, code, as_of):
    """R8 JSON holds the latest report of EACH warning phenomenon.

    Do not choose the newest report across the whole array: an earlier
    wave warning can still be active after a later wind report.  For the
    same warning code choose its most recent status, including releases.
    """
    result = {"status": "missing", "source": "jma_json",
              "active": [], "cancelled": [], "issued_at_utc": None}
    if not _source_ok(source) or not isinstance(source.get("response"), list):
        return result
    latest_by_code = {}
    report_times = []
    for report in source["response"]:
        if not isinstance(report, dict):
            continue
        stamp = _time(report.get("reportDatetime"))
        if stamp is None:
            continue
        for item in (report.get("warning") or {}).get("class20Items", []):
            if item.get("areaCode") != code:
                continue
            report_times.append(stamp)
            for kind in item.get("kinds", []):
                warning_code = str(kind.get("code") or "")
                if not warning_code or kind.get("status") == "発表警報・注意報はなし":
                    continue
                old = latest_by_code.get(warning_code)
                if old is None or stamp >= old[0]:
                    latest_by_code[warning_code] = (stamp, kind)
    if not report_times:
        return {**result, "status": "unassessed", "reason": "exact municipality absent"}
    issued = max(report_times)
    result["issued_at_utc"] = issued.isoformat()
    if issued > as_of + dt.timedelta(minutes=5):
        return {**result, "status": "invalid", "reason": "future-dated JMA issue"}
    # A product array may retain many old observations and cancellations:
    # don't interpret that as a newly issued all-clear statement.
    for when, kind in latest_by_code.values():
        entry = {"code": str(kind.get("code")), "name": JMA_NAMES.get(str(kind.get("code")),
                  str(kind.get("code"))), "status": kind.get("status")}
        if when > as_of + dt.timedelta(minutes=5) or as_of - when > dt.timedelta(hours=12):
            continue  # historical, cannot assert currently active or canceled
        if entry["status"] in ("解除", "無効", "取消"):
            result["cancelled"].append(entry)
        elif entry["status"] in ("発表", "継続"):
            result["active"].append(entry)
    result["area_code"] = code
    result["status"] = "caution"
    result["reason"] = (
        "R8 JSON is per-phenomenon latest history, not a current full municipal "
        "snapshot; only recent positive/cancellation codes retained")
    return result


def _js_value(text, var):
    """Parse a bounded JS object/array literal as data, never execute JS."""
    if not isinstance(text, str) or len(text) > 1_000_000:
        return None
    hit = re.search(r"\bvar\s+" + re.escape(var) + r"\s*=\s*", text)
    if not hit:
        return None
    start = hit.end()
    if start >= len(text) or text[start] not in "[{":
        return None
    opening, closing = text[start], ("]" if text[start] == "[" else "}")
    depth, quote, escaped = 0, None, False
    for j in range(start, len(text)):
        ch = text[j]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                try:
                    return ast.literal_eval(text[start:j + 1])
                except (SyntaxError, ValueError, MemoryError, RecursionError):
                    return None
    return None


def _canonical_area(text):
    return (text or "").replace("台", "臺")


def _cwa_regions(warn):
    body = _canonical_area(warn.get("content_web") or "")
    if "特報區域" not in body:
        return None
    area_text = body.split("特報區域", 1)[1].split("三、", 1)[0]
    return [area for area in TAIWAN_AREAS if area in area_text]


def parse_cwa_js(source, as_of, window_end):
    """Read only currently listed warning IDs and explicitly listed areas."""
    out = {"status": "missing", "source": "cwa/warnings", "warnings": []}
    if not _source_ok(source):
        return out
    text = source.get("response")
    ids, data = _js_value(text, "WarnAll"), _js_value(text, "WarnContent")
    if not isinstance(ids, list) or not isinstance(data, dict):
        return {**out, "status": "invalid", "reason": "CWA warning JS not parseable"}
    # FIFOWS is the website's display group, not a warning record.  Its
    # members appear separately as Wxx IDs in both WarnAll and WarnContent.
    ids = [key for key in ids if key != "FIFOWS"]
    partial = False
    for key in ids:
        rec = data.get(key) or {}
        zh = rec.get("C") or {}
        if not zh:
            partial = True
            continue
        issue = _time(zh.get("issued"), TPE)
        expiry = _time(zh.get("validto"), TPE)
        areas = _cwa_regions(zh)
        if issue is None or expiry is None or areas is None:
            partial = True
        if expiry is not None and expiry <= as_of:
            continue
        if issue is not None and issue > window_end:
            continue
        out["warnings"].append({
            "id": str(key), "name": zh.get("title"), "issued_at_utc": issue.isoformat() if issue else None,
            "valid_to_utc": expiry.isoformat() if expiry else None,
            "areas": areas, "area_verified": areas is not None,
            "applies_taipei": ("臺北市" in areas) if areas is not None else None})
    out["status"] = "caution" if partial else "ready"
    if partial:
        out["reason"] = "some active warnings have missing issue, expiry or exact area"
    return out


def parse_cwa_api(source, as_of, window_end):
    """Prefer the CWA municipality-specific W-C0033-001 source when present."""
    out = {"status": "missing", "source": "cwa/official_warning_api", "warnings": []}
    if not _source_ok(source):
        return out
    body = source.get("response")
    areas = []
    if isinstance(body, str) and body.lstrip().startswith("<"):
        try:
            tree = ET.fromstring(body)
            for loc in tree.findall(".//{*}location"):
                area = loc.findtext("./{*}locationName")
                if area:
                    areas.append({"area": area, "name": loc.findtext("./{*}phenomena"),
                                  "level": loc.findtext("./{*}significance"),
                                  "start": loc.findtext("./{*}startTime"),
                                  "end": loc.findtext("./{*}endTime")})
        except ET.ParseError:
            return {**out, "status": "invalid", "reason": "invalid CWA XML"}
    elif isinstance(body, dict):
        records = body.get("records") or {}
        locations = records.get("location") or records.get("locations") or []
        if isinstance(locations, list):
            for loc in locations:
                if isinstance(loc, dict) and loc.get("locationName"):
                    areas.append({"area": loc["locationName"], "name": loc.get("phenomena"),
                                  "level": loc.get("significance"),
                                  "start": loc.get("startTime"), "end": loc.get("endTime")})
    else:
        return {**out, "status": "invalid", "reason": "unknown CWA response"}
    if not areas:
        return {**out, "status": "unassessed", "reason": "no municipal warning records parsed"}
    for area in areas:
        start, end = _time(area["start"], TPE), _time(area["end"], TPE)
        if not start or not end:
            return {**out, "status": "caution", "reason": "CWA warning validity unparsed"}
        if end > as_of and start < window_end:
            out["warnings"].append({
                "name": area["name"], "level": area["level"],
                "issued_at_utc": start.isoformat(), "valid_to_utc": end.isoformat(),
                "areas": [_canonical_area(area["area"])], "area_verified": True,
                "applies_taipei": _canonical_area(area["area"]) == "臺北市"})
    out["status"] = "ready"
    return out


def parse_wra_kml(source, kind):
    """WRA active-event KML, never the index KML or unchecked GloFAS grids."""
    out = {"status": "missing", "kind": kind, "warnings": [], "taipei_applicability": None}
    if not _source_ok(source) or not isinstance(source.get("response"), str):
        return out
    try:
        tree = ET.fromstring(source["response"])
    except ET.ParseError:
        return {**out, "status": "invalid", "reason": "invalid KML"}
    folders = [t.strip() for x in tree.findall(".//{*}Folder")
               if (t := (x.findtext("./{*}name") or "").strip())]
    placemarks = tree.findall(".//{*}Placemark")
    no_mark = ("目前無淹水警示" if kind == "urban_inundation" else
               "目前無水位警示")
    if not placemarks and any(no_mark in name or
                              (kind == "river_level" and "目前無河川水位警戒" in name)
                              for name in folders):
        return {**out, "status": "ready", "taipei_applicability": False}
    if not placemarks:
        return {**out, "status": "unassessed", "reason": "no recognized no-alert marker or alerts"}
    for mark in placemarks:
        name = mark.findtext("./{*}name") or ""
        detail = " ".join([name, mark.findtext("./{*}description") or ""] +
                          [x.text or "" for x in mark.findall(".//{*}SimpleData")])
        level = re.search(r"(?:淹水|河川水位|水位)?[第]?([一二三123])級(?:警戒|警示)", detail)
        out["warnings"].append({
            "name": name, "level": level.group(1) if level else None,
            "detail": detail[:500], "applies_taipei": "臺北市" in _canonical_area(detail)})
    out["status"] = "caution" if any(x["level"] is None for x in out["warnings"]) else "ready"
    out["taipei_applicability"] = any(x["applies_taipei"] for x in out["warnings"])
    if out["status"] == "caution":
        out["reason"] = "some WRA warning levels not encoded in the KML"
    return out


def summarize(sources, as_of_utc, window_end_utc):
    now, end = _time(as_of_utc), _time(window_end_utc)
    if now is None or end is None:
        raise ValueError("official hazard clock/window missing")
    cities = {}
    for city, (name, code) in JMA_AREAS.items():
        current = parse_jma_xml(sources.get(f"jma/{name}_warning_xml"), code, now)
        fallback = parse_jma_json(sources.get(f"jma/{name}_warnings"), code, now)
        # Prefer newest current municipality-level statement, never a stale
        # XML over newer JSON; a canceled/absent warning is meaningful only
        # when the source explicitly gives a current city snapshot.
        if current["status"] == "ready" and fallback["status"] == "ready":
            if _time(fallback["issued_at_utc"]) > _time(current["issued_at_utc"]):
                current = fallback
        elif current["status"] != "ready" and fallback["status"] in ("ready", "caution"):
            current = fallback
        cities[city] = current
    cwa = parse_cwa_api(sources.get("cwa/official_warning_api"), now, end)
    if cwa["status"] != "ready":
        fallback = parse_cwa_js(sources.get("cwa/warnings"), now, end)
        if fallback["status"] in ("ready", "caution"):
            cwa = fallback
    urban = parse_wra_kml(sources.get("wra/inundation_warning"), "urban_inundation")
    river = parse_wra_kml(sources.get("wra/river_level_warning"), "river_level")
    cwa_city = [x for x in cwa["warnings"] if x.get("applies_taipei")]
    cities["taipei"] = {
        "status": cwa["status"], "source": cwa["source"], "active": cwa_city,
        "unknown_area_warnings": sum(x.get("applies_taipei") is None for x in cwa["warnings"]),
        "urban_inundation": {"status": urban["status"], "warnings": [
            x for x in urban["warnings"] if x.get("applies_taipei")]},
        "river_level": {"status": river["status"], "warnings": [
            x for x in river["warnings"] if x.get("applies_taipei")]}}
    return {"schema": "east_asia_official_hazards_v1",
            "as_of_utc": now.isoformat(), "window_end_utc": end.isoformat(),
            "cities": cities, "regional": {"cwa": cwa,
            "wra_urban_inundation": urban, "wra_river_level": river},
            "note": "Exact warning areas, validity and current-state coverage only. "
                    "Missing or outdated notices never mean no risk."}
