import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from service import Handler, reset_store


class ApiClient:
    def __init__(self, base):
        self.base = base

    def request(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)


class HttpFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_store()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        port = cls.server.server_port
        cls.api = ApiClient(f"http://127.0.0.1:{port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_01_full_assignment_flow(self):
        status, body = self.api.request("GET", "/persons")
        self.assertEqual(status, 200)
        self.assertTrue(body["persons"])
        self.assertIn("relation_version", body["persons"][0])

        status, body = self.api.request("GET", "/cases/CASE-A")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "open")

        # 生成方案
        status, body = self.api.request("POST", "/cases/CASE-A/plans")
        self.assertEqual(status, 200)
        proposal = body["proposal"]
        self.assertTrue(proposal["plan"]["feasible"])
        chosen = {a["role"]: a["person_id"] for a in proposal["plan"]["assignments"]}
        self.assertEqual(set(chosen), {"site_tech", "material_review"})
        versions = proposal["person_versions"]
        self.assertTrue(versions)

        # 缺少 reviewer 被拒
        status, body = self.api.request("POST", "/cases/CASE-A/confirmations", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "bad_reviewer")

        # 负责人确认（携带生成时版本）
        status, body = self.api.request("POST", "/cases/CASE-A/confirmations",
                                        {"reviewer": "王组长",
                                         "expected_versions": versions})
        self.assertEqual(status, 200)
        self.assertEqual(body["confirmed_by"], "王组长")

        # 重复确认 409
        status, body = self.api.request("POST", "/cases/CASE-A/confirmations",
                                        {"reviewer": "李组长"})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "already_confirmed")

        # 已确认案件重新生成方案被拒
        status, body = self.api.request("POST", "/cases/CASE-A/plans")
        self.assertEqual(status, 409)

    def test_02_infeasible_case_returns_core(self):
        status, body = self.api.request("POST", "/cases/CASE-C/plans")
        self.assertEqual(status, 200)
        plan = body["proposal"]["plan"]
        self.assertFalse(plan["feasible"])
        diagnosis = plan["diagnosis"]
        self.assertEqual(diagnosis["code"], "no_feasible_team")
        self.assertEqual(diagnosis["minimal_constraint_cores"][0]["roles"],
                         ["site_tech"])
        # 三类回避核查痕迹齐全
        flattened = [kind for t in diagnosis["recusal_trace"]
                     for kind in t["active_kinds"]]
        for kind in ("employment", "kin_declaration", "acceptance_signoff"):
            self.assertIn(kind, flattened)

        status, body = self.api.request("POST", "/cases/CASE-C/confirmations",
                                        {"reviewer": "王组长"})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "infeasible_plan")

    def test_03_late_relation_removal_and_opinion_flag(self):
        # 先为 CASE-B 建组确认（CASE-A 已占 P002，因此 CASE-B 的方案应选其他人）
        status, body = self.api.request("POST", "/cases/CASE-B/plans")
        self.assertEqual(status, 200)
        plan_b = body["proposal"]["plan"]
        if plan_b["feasible"]:
            self.api.request("POST", "/cases/CASE-B/confirmations",
                             {"reviewer": "赵组长",
                              "expected_versions": body["proposal"]["person_versions"]})

        # 给 CASE-A 的材料审查 P001 补一条窗口内验收签署关系
        status, body = self.api.request("POST", "/persons/P001/relations", {
            "link": {"kind": "acceptance_signoff", "project_id": "PRJ-A",
                     "effective_date": "2026-05-18", "end_date": None,
                     "evidence": "HTTP-LATE-1"}
        })
        self.assertEqual(status, 200)
        actions = {(a["case_id"], a["action"]) for a in body["affected_cases"]}
        self.assertIn(("CASE-A", "removed"), actions)
        case_a = next(a for a in body["affected_cases"] if a["case_id"] == "CASE-A")
        self.assertEqual(case_a["removed_roles"], ["material_review"])
        replacement = case_a["replacement"]["plan"]
        self.assertTrue(replacement["feasible"])
        fixed_roles = {f["role"] for f in replacement["fixed_roles"]}
        self.assertEqual(fixed_roles, {"site_tech"})

        # 补位确认后，新在任人员可以登记意见
        self.api.request("POST", "/cases/CASE-A/confirmations",
                         {"reviewer": "王组长"})  # 先确认补位
        status, body = self.api.request("POST", "/cases/CASE-A/opinions", {
            "role": "material_review",
            "author_person_id": next(
                a["person_id"] for a in
                self.api.request("GET", "/cases/CASE-A")[1]["assignments"]
                if a["role"] == "material_review" and a["status"] == "active"),
            "scope": "防水涂料批次复检",
            "verdict": "conditional",
        })
        self.assertEqual(status, 201)
        opinion_id = body["opinion_id"]

        # 查询案件全貌，验证留痕
        status, body = self.api.request("GET", "/cases/CASE-A")
        self.assertEqual(status, 200)
        statuses = [a["status"] for a in body["assignments"]]
        self.assertIn("removed", statuses)
        self.assertIn("active", statuses)

    def test_04_bad_requests_and_404(self):
        status, body = self.api.request("GET", "/cases/NO-SUCH-CASE")
        self.assertEqual(status, 404)

        status, body = self.api.request("POST", "/persons/P001/relations",
                                        {"link": {"kind": "bad", "project_id": "P",
                                                  "effective_date": "2026-05-01"}})
        self.assertEqual(status, 400)

        status, body = self.api.request("POST", "/cases/CASE-A/opinions", {
            "role": "material_review", "author_person_id": "P001",
            "scope": "x", "verdict": "bad"})
        self.assertEqual(status, 400)

        req = urllib.request.Request(
            self.api.base + "/cases/CASE-A/plans",
            data=b"{not json", headers={"Content-Type": "application/json"},
            method="POST")
        try:
            urllib.request.urlopen(req)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertEqual(json.load(exc)["code"], "invalid_json")

        status, body = self.api.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
