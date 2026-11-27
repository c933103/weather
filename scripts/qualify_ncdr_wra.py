"""Evidence-only NCDR/WRA adapter qualification; never publishes a forecast."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree as ET

HOST = "alerts.ncdr.nat.gov.tw"
ROOT = f"https://{HOST}"
ACTIVE = f"{ROOT}/webapi/RssAtomFeeds.ashx"
ARCHIVE = f"{ROOT}/webapi/RssAtomFeed.ashx?AlertType=8"
TAIPEI = f"{ROOT}/webapi/RssAtomFeed.ashx?County={quote('臺北市')}"
CAP_PATH = re.compile(r"/Capstorage/WRA/(\d{4})/Flood/(WRA_FloodWarn_\d{14}_\d{4})\.cap")
ATOM = "{http://www.w3.org/2005/Atom}"
CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"
MAX_BYTES = 2_000_000
DISTRICTS = {f"630{i:02d}00" for i in range(1, 13)}
EVENT = "profile:CAP-TWP:Event:1.0"
SENDER = "ddmt01@wra.gov.tw"
FIXED = [
    "2026/Flood/WRA_FloodWarn_20260604183730_0003.cap",
    "2026/Flood/WRA_FloodWarn_20260626045730_0000.cap",
    "2026/Flood/WRA_FloodWarn_20261023113730_0000.cap",
    "2026/Flood/WRA_FloodWarn_20261023120730_0000.cap",
]


def clock(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must include an offset")
    return parsed


def cap_url(url):
    p = urlsplit(url)
    return CAP_PATH.fullmatch(p.path) if (
        p.scheme == "https" and p.netloc == HOST and not p.query and not p.fragment
    ) else None


def xml(data, root_tag):
    if len(data) > MAX_BYTES or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        raise ValueError("XML size or declaration rejected")
    root = ET.fromstring(data)
    if root.tag != root_tag:
        raise ValueError("unexpected XML root/namespace")
    return root


def parse_atom(data, now):
    root = xml(data, ATOM + "feed")
    updated = clock(root.findtext(ATOM + "updated", ""))
    wra_urls = {}
    entries = root.findall(ATOM + "entry")
    for entry in entries:
        for link in entry.findall(ATOM + "link"):
            url = link.get("href", "")
            match = cap_url(url)
            if link.get("rel") == "alternate" and match:
                wra_urls[match[2]] = url
    age = now - updated
    return {"updated": updated.isoformat(), "age_seconds": int(age.total_seconds()),
            "fresh": timedelta(minutes=-5) <= age <= timedelta(hours=1),
            "entries": len(entries), "wra_urls": wra_urls}


def named(node, tag):
    return [(p.findtext(CAP + "valueName"), p.findtext(CAP + "value"))
            for p in node.findall(CAP + tag)]


def parse_cap(data, url):
    match = cap_url(url)
    if not match:
        raise ValueError("unexpected WRA CAP URL")
    root = xml(data, CAP + "alert")
    def field(key):
        return root.findtext(CAP + key, "").strip()
    if (field("identifier") != match[2] or field("sender") != SENDER or
            field("status") != "Actual" or field("scope") != "Public"):
        raise ValueError("unexpected CAP identity, issuer, or publication status")
    mode = field("msgType")
    if mode not in {"Alert", "Update", "Cancel"}:
        raise ValueError("unexpected flood CAP message type")
    sent = clock(field("sent"))
    refs = set()
    for ref in field("references").split():
        sender, identifier, issued = ref.split(",", 2)
        issued = clock(issued)
        if sender != SENDER or not re.fullmatch(r"WRA_FloodWarn_\d{14}_\d{4}", identifier) or issued >= sent:
            raise ValueError("invalid CAP reference")
        refs.add((identifier, issued))
    if mode in {"Update", "Cancel"} and not refs:
        raise ValueError("missing CAP lifecycle reference")
    rows, recognized = [], False
    for info in root.findall(CAP + "info"):
        if (EVENT, "Flood") not in named(info, "eventCode"):
            continue
        recognized = True
        if mode == "Cancel":
            continue
        start = clock(info.findtext(CAP + "effective", ""))
        end = clock(info.findtext(CAP + "expires", ""))
        level = dict(named(info, "parameter")).get("severity_level")
        if end <= start or level not in {"一級警戒", "二級警戒"}:
            raise ValueError("invalid WRA validity interval or severity")
        for area in info.findall(CAP + "area"):
            for key, code in named(area, "geocode"):
                if key == "Taiwan_Geocode_103" and code in DISTRICTS:
                    rows.append({"district": area.findtext(CAP + "areaDesc", ""), "geocode": code,
                                 "level": level, "effective": start, "expires": end,
                                 "meaning": "conditional inundation warning", "source_url": url})
    if not recognized:
        raise ValueError("missing WRA Flood event code")
    return {"key": (field("identifier"), sent), "type": mode, "refs": refs, "rows": rows}


def taipei_as_of(messages, as_of):
    messages = {m["key"]: m for m in messages if m["key"][1] <= as_of}
    superseded = {k for m in messages.values() for k in m["refs"]}
    return [row for key, msg in sorted(messages.items()) if key not in superseded
            for row in msg["rows"] if row["effective"] <= as_of < row["expires"]]


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_delay(value, now):
    """Honor server backoff without keeping the qualification runner idle indefinitely."""
    try:
        if value is None:
            delay = 20
        elif value.isdigit():
            delay = int(value)
        else:
            delay = math.ceil((parsedate_to_datetime(value) - now).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None
    return max(1, delay) if delay <= 60 else None


def get(url, *, is_cap=False):
    if (is_cap and not cap_url(url)) or (not is_cap and url not in {ACTIVE, ARCHIVE, TAIPEI}):
        raise ValueError("unexpected source URL")
    for attempt in range(2):
        try:
            with build_opener(NoRedirects()).open(Request(url, headers={
                    "User-Agent": "Weather NCDR qualification / c933103/weather",
                    "Accept": "application/atom+xml,application/cap+xml,application/xml,text/xml",
            }), timeout=10) as response:
                mime = response.headers.get_content_type()
                allowed = {"application/xml", "text/xml",
                           "application/cap+xml" if is_cap else "application/atom+xml"}
                if response.status != 200 or mime not in allowed:
                    raise ValueError(f"unexpected HTTP status or XML MIME: {response.status} {mime}")
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError("payload too large")
                return data, {"mime": mime, "fetched_utc": datetime.now(timezone.utc).isoformat()}
        except HTTPError as error:
            with error:
                delay = retry_delay(error.headers.get("Retry-After"), datetime.now(timezone.utc))
                if error.code not in {429, 503} or attempt or delay is None:
                    raise RuntimeError(f"NCDR HTTP {error.code}: {url}") from None
                print(f"NCDR HTTP {error.code} for {url}; retrying once after {delay}s", flush=True)
            time.sleep(delay)
        except (URLError, TimeoutError) as error:
            if attempt:
                raise RuntimeError(f"NCDR transport failure: {url}") from error
            time.sleep(1)


def main():
    feeds, messages = {}, []
    for label, url in [("active", ACTIVE), ("flood_archive", ARCHIVE), ("taipei_archive", TAIPEI)]:
        data, transport = get(url)
        feeds[label] = {**transport, **parse_atom(data, clock(transport["fetched_utc"]))}
    # Fixed historical positives and lifecycle examples are independent of
    # whether there are active warnings when the qualification runs.
    for path in FIXED:
        url = f"{ROOT}/Capstorage/WRA/{path}"
        data, _ = get(url, is_cap=True)
        messages.append(parse_cap(data, url))
    positives = {r["geocode"] for m in messages for r in m["rows"]}
    if positives != {"6300600", "6300800"} or [m["type"] for m in messages] != ["Alert", "Alert", "Update", "Cancel"]:
        raise ValueError("historical positives or lifecycle examples did not verify")
    active_urls = list(feeds["active"]["wra_urls"].values())
    # Bounded qualification, not a complete active-alert collection service.
    def fetch_cap(url):
        data, _ = get(url, is_cap=True)
        return parse_cap(data, url)
    sampled_urls = active_urls[:12] if feeds["active"]["fresh"] else []
    with ThreadPoolExecutor(max_workers=4) as pool:
        active_messages = list(pool.map(fetch_cap, sampled_urls))
    live_rows = taipei_as_of(active_messages, datetime.now(timezone.utc))
    complete_sample = len(sampled_urls) == len(active_urls) and feeds["active"]["fresh"]
    live_state = ("unassessed" if not complete_sample else
                  "taipei_warning_verified" if live_rows else "no_taipei_warning_in_sample")
    # A quiet feed doesn't complete live positive qualification.
    result = {"feeds": {k: {**v, "wra_urls": list(v["wra_urls"].values())}
                        for k, v in feeds.items()},
              "verified_historical_taipei_geocodes": sorted(positives),
              "historical_cancel_links_to_update": messages[-2]["key"] in messages[-1]["refs"],
              "active_wra_cap_link_count": len(active_urls),
              "active_wra_cap_validated_count": len(active_messages),
              "active_feed_qualification": live_state,
              "active_taipei_warned_district_codes": sorted({row["geocode"] for row in live_rows}),
              "status": "runner_evidence_only", "forecast_reporting_enabled": False}
    if not result["historical_cancel_links_to_update"]:
        raise ValueError("historical cancellation reference changed")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
