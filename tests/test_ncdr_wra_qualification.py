from datetime import timedelta
from pathlib import Path
import sys
import unittest
from xml.etree.ElementTree import ParseError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from qualify_ncdr_wra import ROOT, SENDER, clock, parse_atom, parse_cap, taipei_as_of

PREFIX = "WRA_FloodWarn_202606041"
T = "2026-06-04T"
NS = "urn:oasis:names:tc:emergency:cap:1.2"


def fixture(hour, *, mode="Alert", code="6300600", prior=None, scope="Public", event="Flood"):
    identifier = f"{PREFIX}{hour - 10}0000_0000"
    url = f"{ROOT}/Capstorage/WRA/2026/Flood/{identifier}.cap"
    reference = (f"<references>{SENDER},{prior[0]},{prior[1].isoformat()}</references>"
                 if prior else "")
    data = f"""<alert xmlns="{NS}"><identifier>{identifier}</identifier>
    <sender>{SENDER}</sender><sent>{T}{hour}:00:00+08:00</sent>
    <status>Actual</status><msgType>{mode}</msgType><scope>{scope}</scope>{reference}
    <info><eventCode><valueName>profile:CAP-TWP:Event:1.0</valueName>
    <value>{event}</value></eventCode><certainty>Observed</certainty>
    <effective>{T}{hour}:15:00+08:00</effective><expires>{T}{hour+3}:15:00+08:00</expires>
    <parameter><valueName>severity_level</valueName>
    <value>二級警戒</value></parameter><area><areaDesc>臺北市大同區</areaDesc>
    <geocode><valueName>Taiwan_Geocode_103</valueName><value>{code}</value></geocode>
    </area></info></alert>""".encode()
    return data, url


class TestNcdr(unittest.TestCase):
    def test_effective_expiry_level_and_county(self):
        alert = parse_cap(*fixture(11))
        other = parse_cap(*fixture(12, code="1000212"))
        wenshan = parse_cap(*fixture(13, code="6300800"))
        self.assertFalse(taipei_as_of([alert], clock(T + "11:14:59+08:00")))
        row, = taipei_as_of([alert, other], clock(T + "11:15:00+08:00"))
        self.assertEqual((row["level"], row["geocode"]), ("二級警戒", "6300600"))
        self.assertEqual(row["meaning"], "conditional inundation warning")
        self.assertEqual(row["expires"]-row["effective"], timedelta(hours=3))
        self.assertFalse(taipei_as_of([alert, other], clock(T + "14:15:00+08:00")))
        self.assertEqual(wenshan["rows"][0]["geocode"], "6300800")

    def test_cancel_is_applied_by_issue_time_in_any_input_order(self):
        original = parse_cap(*fixture(11))
        update = parse_cap(*fixture(12, mode="Update", prior=original["key"]))
        cancel = parse_cap(*fixture(13, mode="Cancel", prior=update["key"]))
        unordered = [cancel, update, original]
        row, = taipei_as_of(unordered, clock(T + "12:59:59+08:00"))
        self.assertEqual(row["effective"], clock(T + "12:15:00+08:00"))
        self.assertFalse(taipei_as_of(unordered, clock(T + "13:00:00+08:00")))

    def test_invalid_cap_and_outside_source(self):
        payload, url = fixture(11)
        for bad in (b"<alert/>", payload.replace(b"Actual", b"Test"),
                    fixture(11, event="Other")[0], fixture(11, mode="Cancel")[0],
                    fixture(11, scope="Private")[0], b"<!DOCTYPE alert>" + payload):
            with self.assertRaises((ValueError, ParseError)):
                parse_cap(bad, url)
        with self.assertRaises(ValueError):
            parse_cap(payload, url.replace(ROOT, ROOT + ".invalid"))

    def test_atom_distinguishes_recent_empty_and_stale_archive(self):
        now = clock(T + "13:00:00+08:00")
        _, url = fixture(11)
        empty = b'<feed xmlns="http://www.w3.org/2005/Atom"><updated>2026-06-04T12:55:00+08:00</updated></feed>'
        stale = empty.decode().replace("12:55", "11:00").replace("</feed>", f"""
          <entry><link rel="alternate" href="{url}" /><link rel="alternate" href="{url.replace(ROOT, ROOT + ".invalid")}" /></entry></feed>""").encode()
        self.assertTrue(parse_atom(empty, now)["fresh"])
        self.assertEqual(parse_atom(empty, now)["entries"], 0)
        self.assertFalse(parse_atom(stale, now)["fresh"])
        self.assertEqual(list(parse_atom(stale, now)["wra_urls"].values()), [url])
        with self.assertRaises(ValueError):
            parse_atom(b"<feed/>", now)
