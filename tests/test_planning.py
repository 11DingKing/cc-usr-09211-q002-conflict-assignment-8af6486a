import unittest

from contracts import (
    ProjectLink, SourceRecord, CaseRecord,
    LINK_EMPLOYMENT, LINK_KIN, LINK_ACCEPTANCE,
)
from planning import plan_case, build_diagnosis, candidate_audit


def person(pid, quals, capacity=3, region="本地", links=()):
    return SourceRecord(person_id=pid, qualifications=list(quals), capacity=capacity,
                        region=region, project_links=list(links))


def case(case_id="C1", project_id="PRJ-X", region="本地", emergency="normal",
         start="2026-05-01", end="2026-05-31"):
    return CaseRecord(case_id=case_id, project_id=project_id, window_start=start,
                      window_end=end, region=region, emergency=emergency)


def kin(pid_project, date):
    return ProjectLink(kind=LINK_KIN, project_id=pid_project, effective_date=date,
                       end_date=None, evidence="E")


class FeasibleSelectionTests(unittest.TestCase):
    def test_local_pair_preferred_over_cross_region(self):
        persons = [
            person("LOCAL-ST", ["site_tech"], region="本地"),
            person("LOCAL-MA", ["material_review"], region="本地"),
            person("REMOTE-ST", ["site_tech"], region="外地"),
            person("REMOTE-MA", ["material_review"], region="外地"),
        ]
        result = plan_case(case(), persons, {})
        self.assertTrue(result["feasible"])
        chosen = {a["role"]: a["person_id"] for a in result["assignments"]}
        self.assertEqual(chosen, {"site_tech": "LOCAL-ST", "material_review": "LOCAL-MA"})
        self.assertEqual(result["score"]["cross_region_members"], 0)

    def test_urgent_case_avoids_cross_region_even_when_local_busier(self):
        # 本地现场技术负载很高，外地有空闲：普通案件允许跨区权衡，但本案紧急，
        # 本地仍因无跨区延误而胜出（容量给足，确保是偏好比较而非硬约束出局）
        persons = [
            person("LOCAL-ST", ["site_tech"], capacity=9, region="本地"),
            person("REMOTE-ST", ["site_tech"], region="外地"),
            person("LOCAL-MA", ["material_review"], region="本地"),
        ]
        loads = {"LOCAL-ST": 2}
        result_normal = plan_case(case(emergency="normal"), persons, loads)
        result_critical = plan_case(case(emergency="critical"), persons, loads)
        chosen_normal = next(a for a in result_normal["assignments"] if a["role"] == "site_tech")
        chosen_critical = next(a for a in result_critical["assignments"] if a["role"] == "site_tech")
        # normal: 跨区代价 10*1=10，本地边际 2*2+1=5 -> 本地仍胜（5<10）
        self.assertEqual(chosen_normal["person_id"], "LOCAL-ST")
        self.assertEqual(chosen_critical["person_id"], "LOCAL-ST")
        # 将本地负载推高到跨区更划算：边际 2*6+1=13 > 10（normal 时选外地）
        result_heavy = plan_case(case(emergency="normal"), persons, {"LOCAL-ST": 6})
        chosen_heavy = next(a for a in result_heavy["assignments"] if a["role"] == "site_tech")
        self.assertEqual(chosen_heavy["person_id"], "REMOTE-ST")
        self.assertTrue(chosen_heavy["cross_region"])
        # critical 跨区代价 30，本地边际 13 仍小于 30，继续选本地
        result_heavy_critical = plan_case(case(emergency="critical"), persons,
                                          {"LOCAL-ST": 6})
        chosen_hc = next(a for a in result_heavy_critical["assignments"]
                         if a["role"] == "site_tech")
        self.assertEqual(chosen_hc["person_id"], "LOCAL-ST")

    def test_load_balancing_prefers_idle_person(self):
        persons = [
            person("BUSY", ["site_tech", "material_review"], region="本地"),
            person("IDLE", ["site_tech", "material_review"], region="本地"),
        ]
        result = plan_case(case(), persons, {"BUSY": 2})
        chosen = {a["person_id"] for a in result["assignments"]}
        self.assertEqual(chosen, {"IDLE", "BUSY"})  # 两岗位不能同人，IDLE 占其一
        # 两个岗位中 IDLE 拿下负载代价最低的位置——这里两角色等价，验证总量与解释
        self.assertTrue(any("在担 2 件" in r for a in result["assignments"]
                            for r in a["reasons"] if a["person_id"] == "BUSY"))

    def test_two_roles_must_be_different_people(self):
        # 只有一人兼具两资质但容量够：也不能同时占两岗 -> 不可行
        persons = [person("SOLO", ["site_tech", "material_review"])]
        result = plan_case(case(), persons, {})
        self.assertFalse(result["feasible"])
        cores = result["diagnosis"]["minimal_constraint_cores"]
        # 两个岗位的并集只有一人，违反 |N(S)|>=|S|
        self.assertTrue(any(set(core["roles"]) == {"site_tech", "material_review"}
                            for core in cores))

    def test_capacity_zero_excluded(self):
        persons = [
            person("FULL", ["site_tech"], capacity=1),
            person("MA", ["material_review"]),
        ]
        result = plan_case(case(), persons, {"FULL": 1})
        self.assertFalse(result["feasible"])
        exclusions = result["diagnosis"]["exclusions"]["site_tech"]
        self.assertEqual(exclusions[0]["constraint"], "no_capacity")


class InfeasibleDiagnosisTests(unittest.TestCase):
    def test_minimal_core_single_role_with_three_kind_exclusions(self):
        # 现场技术岗位无合格候选人：三人分别因任职、亲属、验收签署三类关系被排除
        st1 = person("ST-EMP", ["site_tech"], links=[
            ProjectLink(kind=LINK_EMPLOYMENT, project_id="PRJ-X",
                        effective_date="2026-05-02", end_date="2026-05-10", evidence="E1")])
        st2 = person("ST-KIN", ["site_tech"], links=[kin("PRJ-X", "2026-05-03")])
        st3 = person("ST-ACC", ["site_tech"], links=[
            ProjectLink(kind=LINK_ACCEPTANCE, project_id="PRJ-X",
                        effective_date="2026-05-04", end_date=None, evidence="E3")])
        ma = person("MA", ["material_review"])
        persons = [st1, st2, st3, ma]
        result = plan_case(case(), persons, {})
        self.assertFalse(result["feasible"])
        diagnosis = result["diagnosis"]
        cores = diagnosis["minimal_constraint_cores"]
        self.assertEqual(len(cores), 1)
        self.assertEqual(cores[0]["roles"], ["site_tech"])
        self.assertEqual(cores[0]["available_candidates"], 0)
        # 最小核必须逐人给出排除原因，且三类关系各出现一次，无一类被省略
        by_person = {item["person_id"]: item
                     for item in diagnosis["exclusions"]["site_tech"]}
        self.assertEqual(by_person["ST-EMP"]["active_relation_kinds"], ["employment"])
        self.assertEqual(by_person["ST-KIN"]["active_relation_kinds"], ["kin_declaration"])
        self.assertEqual(by_person["ST-ACC"]["active_relation_kinds"],
                         ["acceptance_signoff"])
        trace_kinds = {
            (t["person_id"], tuple(t["active_kinds"]))
            for t in diagnosis["recusal_trace"]
        }
        self.assertIn(("ST-EMP", ("employment",)), trace_kinds)
        self.assertIn(("ST-KIN", ("kin_declaration",)), trace_kinds)
        self.assertIn(("ST-ACC", ("acceptance_signoff",)), trace_kinds)
        # 每个被排查人员都列出全部三类受检关系
        for t in diagnosis["recusal_trace"]:
            self.assertEqual(t["checked_kinds"],
                             ["employment", "kin_declaration", "acceptance_signoff"])

    def test_minimal_core_is_inclusion_minimal(self):
        # site_tech 无候选；material_review 有候选。最小核只能是 site_tech，
        # 不能把两岗位的大集合也当作“最小约束”
        persons = [
            person("ST-BAD", ["site_tech"], links=[kin("PRJ-X", "2026-05-03")]),
            person("MA-OK", ["material_review"]),
        ]
        result = plan_case(case(), persons, {})
        self.assertFalse(result["feasible"])
        cores = result["diagnosis"]["minimal_constraint_cores"]
        self.assertEqual(cores, [{"roles": ["site_tech"], "candidate_union": [],
                                  "required_seats": 1, "available_candidates": 0,
                                  "shortage": 1}])

    def test_relation_outside_window_does_not_save_or_break(self):
        # 仅有窗口外任职 + 窗口内无其他关系的人员必须保持合格，禁止“悄悄扩大回避”
        p = person("P", ["site_tech"], links=[
            ProjectLink(kind=LINK_EMPLOYMENT, project_id="PRJ-X",
                        effective_date="2026-03-01", end_date="2026-04-30", evidence="E")])
        ma = person("MA", ["material_review"])
        result = plan_case(case(), [p, ma], {})
        self.assertTrue(result["feasible"])
        st = next(a for a in result["assignments"] if a["role"] == "site_tech")
        self.assertEqual(st["person_id"], "P")

    def test_diagnosis_combines_missing_qualification_and_recusal(self):
        # 同岗位候选池：一人无资质、一人被回避，核仍为该岗位且分别列明
        persons = [
            person("NO-QUAL", ["material_review"]),
            person("RECUSED", ["site_tech"], links=[kin("PRJ-X", "2026-05-03")]),
            person("MA", ["material_review"]),
        ]
        result = plan_case(case(), persons, {})
        self.assertFalse(result["feasible"])
        exclusions = {e["person_id"]: e["constraint"]
                      for e in result["diagnosis"]["exclusions"]["site_tech"]}
        self.assertEqual(exclusions["NO-QUAL"], "missing_qualification")
        self.assertEqual(exclusions["RECUSED"], "recused")


class SampleDataTests(unittest.TestCase):
    def test_sample_cases(self):
        from contracts import read_records, read_cases
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        persons = read_records(root / "data/example.json")
        cases = {c.case_id: c for c in read_cases(root / "data/cases.json")}

        plan_a = plan_case(cases["CASE-A"], persons, {})
        self.assertTrue(plan_a["feasible"])
        roles = {a["role"] for a in plan_a["assignments"]}
        self.assertEqual(roles, {"site_tech", "material_review"})
        self.assertTrue(plan_a["assignments"])  # 解释非空
        self.assertTrue(all(a["reasons"] for a in plan_a["assignments"]))

        plan_c = plan_case(cases["CASE-C"], persons, {})
        self.assertFalse(plan_c["feasible"])
        cores = plan_c["diagnosis"]["minimal_constraint_cores"]
        self.assertEqual(cores[0]["roles"], ["site_tech"])


if __name__ == "__main__":
    unittest.main()
