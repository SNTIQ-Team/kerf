import copy
import hashlib
import json
import unittest

from api.court_observations import FILENAME, PROFILE, validate_metadata
from tests import test_decision_passages as fixtures
from tools.court_observations import attach


def court_row(identity):
    return {"id":identity,"reference":"1 A 4/01","court":"Verwaltungsgericht Test","court_short":"VG Test",
            "event_date":"2001-03-01","document_type":"Urteil","published_at":None,"ecli":None,"norm_mentions":[],
            "source":{"profile":"nrwe-court-metadata/1","url":"https://nrwe.justiz.nrw.de/test/1.html",
                      "media_type":"text/html","content_sha256":"a"*64,"observed_at":"2026-08-10T08:00:00Z",
                      "canonical_decision_id":"court:vg-test/2001-03-01/1_A_4-01","lineage_key":"fixture-origin",
                      "parser_version":"courts-parser/1.8.1"}}


class ProjectionTests(unittest.TestCase):
    setUp=fixtures.ApiTests.setUp

    def publish(self,rows):
        path=self.root/"durable-input.json"
        path.write_text(json.dumps({"profile":PROFILE,"observations":rows,"excluded":[]}))
        return attach(path,self.root)

    def test_pages_keep_legacy_and_expose_official_metadata_without_promoting_passages(self):
        original=self.client.get("/decision-observations").json()
        extra=court_row("nrw-test")
        duplicate=copy.deepcopy(extra)
        duplicate["id"]="rerelease"
        self.publish([extra,duplicate])
        responses=[self.client.get("/decision-observations",params={"offset":i,"limit":1}).json() for i in range(3)]
        self.assertEqual([p["total"] for p in responses],[2,2,2])
        rows=[r for p in responses for r in p["observations"]]
        self.assertEqual(rows,sorted(original["observations"]+[extra],key=lambda r:r["id"]))
        info=responses[0]["coverage"]
        self.assertEqual(info["passage_indexed_decisions"],1)
        self.assertEqual(info["metadata_only_decisions"],1)
        self.assertEqual(hashlib.sha256((self.root/FILENAME).read_bytes()).hexdigest(),info["database_sha256"])
        self.assertEqual(original["coverage"]["database_sha256"],info["passage_database_sha256"])
        self.assertEqual(self.client.get("/decision-passages?q=exact").json()["coverage"]["indexed_decisions"],1)

    def test_changed_or_missing_companion_file_fails_closed(self):
        self.publish([court_row("nrw-test")])
        path=self.root/FILENAME
        path.write_bytes(path.read_bytes()+b" ")
        self.assertEqual(self.client.get("/decision-observations").status_code,503)
        path.unlink()
        self.assertEqual(self.client.get("/decision-observations").status_code,503)

    def test_metadata_artifact_only_accepts_typed_official_provenance(self):
        for field,value in (("url","https://example.org/test/1.html"),("url","https://nrwe.justiz.nrw.de/private"),
                            ("observed_at","2100-01-01T00:00:00Z"),("content_sha256","bad")):
            row=court_row("nrw-test")
            row["source"][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                validate_metadata(row)
        row=court_row("nrw-test")
        row["summary"]="editorial material"
        with self.assertRaises(ValueError):
            self.publish([row])

    def test_duplicate_court_date_docket_without_ecli_and_future_legacy_priority(self):
        original=self.client.get("/decision-observations").json()["observations"][0]
        twin=court_row("nrw-spelling")
        for name in ("reference","court","event_date","document_type","ecli"):
            twin[name]=original[name]
        twin["court"]=twin["court"].upper()
        desc=self.publish([twin])
        self.assertEqual(desc["metadata_only"],0)
        self.assertEqual(self.client.get("/decision-observations").json()["observations"],[original])
