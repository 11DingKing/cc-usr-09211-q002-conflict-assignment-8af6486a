import concurrent.futures
import unittest

from contracts import (
    ProjectLink, SourceRecord, CaseRecord,
    LINK_EMPLOYMENT, LINK_KIN, LINK_ACCEPTANCE,
)
from store import Store, StoreError, ConflictError, NotFoundError


def person(pid, quals, capacity=3, region="本地", links=()):
    return SourceRecord(person_id=pid, qualifications=list(quals), capacity=capacity,
                        region=region, project_links=list(links))


def case(case_id="C1", project_id="PRJ-X", start="2026-05-01", end="2026-05-31",
         region="本地", emergency="normal"):
    return CaseRecord(case_id=case_id, project_id=project_id, window_start=start,
                      window_end=end, region=region, emergency=emergency)


def link(kind, project_id="PRJ-X", start="2026-05-10", end=None, evidence="E"):
    return ProjectLink(kind=kind, project_id=project_id, effective_date=start,
                       end_date=end, evidence=evidence)


def build_store():
    """两名本地现场技术 P002 容量 1（争抢点）、材料 P001；外地备选 P003 双资质容量 5。"""
    persons = [
        person("P001", ["material_review"], capacity=3),
        person("P002", ["site_tech"], capacity=1),
        person("P003", ["site_tech", "material_review"], capacity=5, region="外地"),
    ]
    cases = [
        case("C1"),
        case("C2", project_id="PRJ-Y"),
    ]
    return Store(persons, cases)


class ConfirmationTests(unittest.TestCase):
    def test_confirm_requires_plan(self):
        store = build_store()
        with self.assertRaises(NotFoundError):
            store.confirm_plan("C1", "负责人甲")

    def test_unknown_case_404(self):
        store = build_store()
        with self.assertRaises(NotFoundError):
            store.create_plan("NOPE")

    def test_concurrent_confirmations_only_one_wins(self):
        # 多个轮次消除线程调度偶然性：两案方案都在零负载时预先生成，
        # 因此都锁定容量仅为 1 的 P002；随后两个确认并发落库，只能赢一个
        versions = {}

        def attempt(case_id):
            try:
                self.store.confirm_plan(case_id, f"负责人-{case_id}",
                                        expected_versions=versions[case_id])
                return case_id, "confirmed"
            except StoreError as exc:
                return case_id, exc.code

        for _ in range(5):
            self.store = build_store()
            for cid in ("C1", "C2"):
                versions[cid] = self.store.create_plan(cid)["person_versions"]
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(attempt, cid) for cid in ("C1", "C2")]
                results = [f.result() for f in futures]
            statuses = {cid: status for cid, status in results}
            self.assertEqual(sorted(statuses.values()),
                             ["capacity_conflict", "confirmed"], results)
            # 无论谁胜出，P002 在担量只能是 1
            self.assertEqual(self.store._active_load()["P002"], 1)
            # 恰有一案进入调查，另一案仍待确认
            states = {cid: self.store.case_view(cid)["state"] for cid in ("C1", "C2")}
            self.assertEqual(sorted(states.values()),
                             ["awaiting_confirmation", "in_progress"])

    def test_infeasible_plan_cannot_confirm(self):
        persons = [
            person("ST", ["site_tech"], links=[link(LINK_KIN)]),
            person("MA", ["material_review"]),
        ]
        store = Store(persons, [case("C1")])
        store.create_plan("C1")
        with self.assertRaisesRegex(ConflictError, "不可行"):
            store.confirm_plan("C1", "甲")

    def test_stale_relation_version_rejected(self):
        store = build_store()
        proposal = store.create_plan("C1")
        versions = dict(proposal["person_versions"])
        # 建方案后补一条窗口外关系：不改变回避结论，但关系版本变了，确认必须被拒
        store.register_relation("P002", {
            "kind": LINK_KIN, "project_id": "PRJ-OTHER",
            "effective_date": "2025-01-01", "end_date": None, "evidence": "V1"})
        with self.assertRaises(ConflictError) as ctx:
            store.confirm_plan("C1", "甲", expected_versions=versions)
        self.assertEqual(ctx.exception.code, "relation_version_stale")
        self.assertIn("P002", ctx.exception.details["changed"])
        # 重新生成方案后用新版本可确认
        new_proposal = store.create_plan("C1")
        store.confirm_plan("C1", "甲",
                           expected_versions=new_proposal["person_versions"])

    def test_client_supplied_wrong_versions_rejected(self):
        store = build_store()
        store.create_plan("C1")
        with self.assertRaises(ConflictError) as ctx:
            store.confirm_plan("C1", "甲", expected_versions={"P002": "deadbeef"})
        self.assertEqual(ctx.exception.code, "client_version_stale")

    def test_duplicate_confirmation_rejected(self):
        store = build_store()
        proposal = store.create_plan("C1")
        store.confirm_plan("C1", "甲", expected_versions=proposal["person_versions"])
        with self.assertRaises(ConflictError) as ctx:
            store.confirm_plan("C1", "乙")
        self.assertEqual(ctx.exception.code, "already_confirmed")


class ReplacementAndOpinionTests(unittest.TestCase):
    def _confirmed_store(self):
        store = build_store()
        proposal = store.create_plan("C1")
        store.confirm_plan("C1", "负责人甲",
                           expected_versions=proposal["person_versions"])
        return store

    def test_late_in_window_relation_triggers_removal_and_replacement(self):
        store = self._confirmed_store()
        store.record_opinion("C1", "site_tech", "P002", "3号楼钢筋间距抽测", "pass")
        result = store.register_relation("P002", {
            "kind": LINK_ACCEPTANCE, "project_id": "PRJ-X",
            "effective_date": "2026-05-15", "end_date": None,
            "evidence": "LATE-1"})
        affected = {a["case_id"]: a for a in result["affected_cases"]}
        self.assertEqual(affected["C1"]["action"], "removed")
        self.assertEqual(affected["C1"]["removed_roles"], ["site_tech"])

        view = store.case_view("C1")
        self.assertEqual(view["state"], "replacing")
        # 撤换留痕：原分工状态为 removed 且保留原因
        removed = [a for a in view["assignments"] if a["status"] == "removed"]
        self.assertEqual(len(removed), 1)
        self.assertIn("acceptance_signoff", removed[0]["removed_reason"])

        # 补位方案：材料岗固定为 P001，现场技术只能由外地 P003 补
        replacement = affected["C1"]["replacement"]["plan"]
        self.assertTrue(replacement["feasible"])
        by_role = {a["role"]: a for a in replacement["assignments"]}
        self.assertTrue(by_role["material_review"].get("fixed"))
        self.assertEqual(by_role["material_review"]["person_id"], "P001")
        self.assertEqual(by_role["site_tech"]["person_id"], "P003")
        self.assertNotIn("P002",
                         [a["person_id"] for a in replacement["assignments"]])

        # 负责人确认补位（再次核对版本容量），团队恢复完整
        confirmed = store.confirm_plan("C1", "负责人乙")
        active = [a for a in confirmed["assignments"] if a["status"] == "active"]
        self.assertEqual({a["person_id"] for a in active}, {"P001", "P003"})
        self.assertEqual(store.case_view("C1")["state"], "in_progress")

    def test_historical_opinions_preserved_and_flagged(self):
        store = self._confirmed_store()
        store.record_opinion("C1", "site_tech", "P002", "现场测量批次A", "conditional")
        store.record_opinion("C1", "material_review", "P001", "材料质保书批次B", "pass")
        store.register_relation("P002", {
            "kind": LINK_KIN, "project_id": "PRJ-X",
            "effective_date": "2026-05-20", "end_date": None, "evidence": "LATE-2"})
        view = store.case_view("C1")
        opinions = {o["author_person_id"]: o for o in view["opinions"]}
        # 原意见没有被删除，仅状态改标；另一岗位意见不受影响
        self.assertEqual(opinions["P002"]["status"], "needs_recheck")
        self.assertIn("重新核验", opinions["P002"]["flag_reason"])
        self.assertEqual(opinions["P002"]["verdict"], "conditional")  # 结论原文保留
        self.assertEqual(opinions["P002"]["scope"], "现场测量批次A")
        self.assertEqual(opinions["P001"]["status"], "valid")
        # 补位确认前，该岗位处于空缺状态
        with self.assertRaises(ConflictError) as ctx:
            store.record_opinion("C1", "site_tech", "P002", "补登记", "pass")
        self.assertEqual(ctx.exception.code, "role_unassigned")
        # 负责人确认补位（再次核对版本容量）
        store.confirm_plan("C1", "乙")
        # 补位后：被撤换人登记被拒（岗位已在任，是别人）
        with self.assertRaises(ConflictError) as ctx:
            store.record_opinion("C1", "site_tech", "P002", "补登记", "pass")
        self.assertEqual(ctx.exception.code, "not_assignee")
        new_op = store.record_opinion("C1", "site_tech", "P003", "撤换后复测批次A", "pass")
        self.assertEqual(new_op["status"], "valid")

    def test_outside_window_late_relation_does_not_remove(self):
        store = self._confirmed_store()
        result = store.register_relation("P002", {
            "kind": LINK_EMPLOYMENT, "project_id": "PRJ-X",
            "effective_date": "2026-06-01", "end_date": "2026-06-10",
            "evidence": "LATE-OUT"})
        affected = result["affected_cases"][0]
        self.assertEqual(affected["action"], "not_recused_outside_window")
        self.assertEqual(store.case_view("C1")["state"], "in_progress")

    def test_late_relation_on_other_project_ignored(self):
        store = self._confirmed_store()
        result = store.register_relation("P002", {
            "kind": LINK_KIN, "project_id": "PRJ-OTHER",
            "effective_date": "2026-05-20", "end_date": None, "evidence": "X"})
        self.assertEqual(result["affected_cases"], [])

    def test_replacement_infeasible_lists_minimal_core(self):
        # 唯一现场技术 P002 被撤；另一现场技术 P003 恰好也在此时命中窗口内关系
        persons = [
            person("P001", ["material_review"], capacity=3),
            person("P002", ["site_tech"], capacity=1),
            person("P003", ["site_tech"], capacity=5, region="外地",
                   links=[link(LINK_ACCEPTANCE, start="2026-05-12")]),
        ]
        store = Store(persons, [case("C1")])
        proposal = store.create_plan("C1")
        store.confirm_plan("C1", "甲", expected_versions=proposal["person_versions"])
        result = store.register_relation("P002", {
            "kind": LINK_KIN, "project_id": "PRJ-X",
            "effective_date": "2026-05-20", "end_date": None, "evidence": "LATE"})
        replacement = result["affected_cases"][0]["replacement"]["plan"]
        self.assertFalse(replacement["feasible"])
        cores = replacement["diagnosis"]["minimal_constraint_cores"]
        self.assertEqual(cores[0]["roles"], ["site_tech"])
        # 固定的材料岗位仍保留在方案说明中
        self.assertEqual(replacement["fixed_roles"],
                         [{"role": "material_review", "person_id": "P001"}])
        with self.assertRaises(ConflictError) as ctx:
            store.confirm_plan("C1", "乙")
        self.assertEqual(ctx.exception.code, "infeasible_plan")

    def test_pending_replacement_rebuilt_when_new_candidate_is_recused(self):
        # 撤换 P002 后唯一补位现场技术是 P003；补位尚未确认时，P003 又被发现
        # 窗口内关系 -> 系统自动重建补位方案（不再含 P003）；无可补人员时给出最小核
        store = self._confirmed_store()
        first = store.register_relation("P002", {
            "kind": LINK_KIN, "project_id": "PRJ-X",
            "effective_date": "2026-05-20", "end_date": None, "evidence": "D1"})
        replacement1 = first["affected_cases"][0]["replacement"]["plan"]
        self.assertEqual(
            next(a["person_id"] for a in replacement1["assignments"]
                 if a["role"] == "site_tech" and not a.get("fixed")),
            "P003")

        second = store.register_relation("P003", {
            "kind": LINK_KIN, "project_id": "PRJ-X",
            "effective_date": "2026-05-21", "end_date": None, "evidence": "D2"})
        rebuild = next(a for a in second["affected_cases"] if a["case_id"] == "C1")
        self.assertEqual(rebuild["action"], "replacement_rebuilt")
        plan2 = rebuild["replacement"]["plan"]
        self.assertFalse(plan2["feasible"])
        self.assertEqual(plan2["diagnosis"]["minimal_constraint_cores"][0]["roles"],
                         ["site_tech"])
        # 固定材料岗位仍保留，撤换留痕仍在
        self.assertEqual(plan2["fixed_roles"],
                         [{"role": "material_review", "person_id": "P001"}])
        view = store.case_view("C1")
        self.assertEqual({a["status"] for a in view["assignments"]},
                         {"active", "removed"})

    def test_opinion_validation(self):
        store = self._confirmed_store()
        with self.assertRaises(StoreError):
            store.record_opinion("C1", "site_tech", "P002", "结论", "maybe")
        with self.assertRaises(StoreError):
            store.record_opinion("C1", "material_review", "P001", "  ", "pass")
        with self.assertRaises(ConflictError) as ctx:
            store.record_opinion("C1", "material_review", "P002", "越权登记", "pass")
        self.assertEqual(ctx.exception.code, "not_assignee")


if __name__ == "__main__":
    unittest.main()
