import json
from pathlib import Path
import unittest

from contracts import (
    read_records, read_cases, to_mapping, SourceRecord, CaseRecord, ProjectLink,
    LINK_EMPLOYMENT, LINK_KIN, LINK_ACCEPTANCE,
)

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_sample_roundtrip(self):
        path = ROOT / "data/example.json"
        self.assertEqual(
            [to_mapping(r) for r in read_records(path)],
            json.loads(path.read_text(encoding="utf-8")),
        )

    def test_cases_roundtrip(self):
        path = ROOT / "data/cases.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([to_mapping(c) for c in read_cases(path)], raw)

    def test_missing_fields_rejected(self):
        with self.assertRaises(TypeError):
            SourceRecord()

    def test_bad_link_kind(self):
        with self.assertRaises(ValueError):
            ProjectLink(kind="shareholder", project_id="P", effective_date="2026-01-01")

    def test_employment_requires_end_date(self):
        with self.assertRaises(ValueError):
            ProjectLink(kind=LINK_EMPLOYMENT, project_id="P", effective_date="2026-01-01")

    def test_employment_start_after_end(self):
        with self.assertRaises(ValueError):
            ProjectLink(kind=LINK_EMPLOYMENT, project_id="P",
                        effective_date="2026-02-01", end_date="2026-01-01")

    def test_bad_date_format(self):
        with self.assertRaises(ValueError):
            ProjectLink(kind=LINK_KIN, project_id="P", effective_date="2026/01/01")
        with self.assertRaises(ValueError):
            ProjectLink(kind=LINK_KIN, project_id="P", effective_date="2026-02-30")

    def test_capacity_non_negative_int(self):
        with self.assertRaises(ValueError):
            SourceRecord(person_id="X", qualifications=["site_tech"],
                         project_links=[], capacity=-1)
        with self.assertRaises(ValueError):
            SourceRecord(person_id="X", qualifications=["site_tech"],
                         project_links=[], capacity=True)

    def test_case_requires_both_roles(self):
        with self.assertRaises(ValueError):
            CaseRecord(case_id="C", project_id="P", window_start="2026-01-01",
                       window_end="2026-01-31", required_roles=("site_tech",))

    def test_case_window_order(self):
        with self.assertRaises(ValueError):
            CaseRecord(case_id="C", project_id="P",
                       window_start="2026-02-01", window_end="2026-01-01")

    def test_case_bad_emergency(self):
        with self.assertRaises(ValueError):
            CaseRecord(case_id="C", project_id="P", window_start="2026-01-01",
                       window_end="2026-01-31", emergency="soon")


class ServiceSmokeTests(unittest.TestCase):
    def test_health_and_unknown_route(self):
        import threading
        import urllib.request
        import urllib.error
        from http.server import ThreadingHTTPServer
        from service import Handler, reset_store
        reset_store()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_port)
            with urllib.request.urlopen(base + "/health") as response:
                self.assertEqual(json.load(response), {"status": "ok"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + "/unknown")
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
