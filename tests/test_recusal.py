import unittest

from contracts import (
    ProjectLink, SourceRecord, CaseRecord,
    LINK_EMPLOYMENT, LINK_KIN, LINK_ACCEPTANCE,
    ROLE_SITE_TECH, ROLE_MATERIAL,
)
from recusal import evaluate_person, is_recused, classify_link

WINDOW_START = "2026-05-01"
WINDOW_END = "2026-05-31"


def make_case(case_id="C1", project_id="PRJ-X"):
    return CaseRecord(case_id=case_id, project_id=project_id,
                      window_start=WINDOW_START, window_end=WINDOW_END)


def make_person(pid, links, quals=("site_tech", "material_review")):
    return SourceRecord(person_id=pid, qualifications=list(quals),
                        project_links=links, capacity=3, region="本地")


def link(kind, project_id="PRJ-X", start="2026-05-10", end=None, evidence="E"):
    return ProjectLink(kind=kind, project_id=project_id,
                       effective_date=start, end_date=end, evidence=evidence)


class EmploymentWindowTests(unittest.TestCase):
    def test_overlaps_inside_window(self):
        person = make_person("P", [link(LINK_EMPLOYMENT, start="2026-05-10", end="2026-05-20")])
        self.assertTrue(is_recused(person, make_case()))

    def test_endpoint_touch_overlaps(self):
        # 闭区间：任职结束日恰为窗口起始日、任职起始日恰为窗口结束日均算重叠
        self.assertEqual(classify_link(
            link(LINK_EMPLOYMENT, start="2026-04-01", end="2026-05-01"),
            WINDOW_START, WINDOW_END), "active")
        self.assertEqual(classify_link(
            link(LINK_EMPLOYMENT, start="2026-05-31", end="2026-06-10"),
            WINDOW_START, WINDOW_END), "active")

    def test_gap_does_not_overlap(self):
        self.assertEqual(classify_link(
            link(LINK_EMPLOYMENT, start="2026-04-01", end="2026-04-30"),
            WINDOW_START, WINDOW_END), "outside_window")
        self.assertEqual(classify_link(
            link(LINK_EMPLOYMENT, start="2026-06-01", end="2026-06-30"),
            WINDOW_START, WINDOW_END), "outside_window")
        person = make_person("P", [link(LINK_EMPLOYMENT, start="2026-04-01", end="2026-04-30")])
        self.assertFalse(is_recused(person, make_case()))


class SingleDateRelationTests(unittest.TestCase):
    def test_kin_in_window_blocks(self):
        person = make_person("P", [link(LINK_KIN, start="2026-05-01")])
        result = evaluate_person(person, make_case())
        self.assertTrue(result["blocked"])
        self.assertIn(LINK_KIN, result["active"])

    def test_kin_endpoint_days(self):
        self.assertEqual(classify_link(link(LINK_KIN, start="2026-05-01"),
                                       WINDOW_START, WINDOW_END), "active")
        self.assertEqual(classify_link(link(LINK_KIN, start="2026-05-31"),
                                       WINDOW_START, WINDOW_END), "active")

    def test_kin_day_before_window_not_blocked(self):
        person = make_person("P", [link(LINK_KIN, start="2026-04-30")])
        result = evaluate_person(person, make_case())
        self.assertFalse(result["blocked"])
        self.assertIn(LINK_KIN, result["outside_window"])

    def test_acceptance_in_window_blocks(self):
        person = make_person("P", [link(LINK_ACCEPTANCE, start="2026-05-20")])
        result = evaluate_person(person, make_case())
        self.assertTrue(result["blocked"])
        self.assertEqual(list(result["active"]), [LINK_ACCEPTANCE])

    def test_acceptance_outside_window_kept_not_blocking(self):
        person = make_person("P", [link(LINK_ACCEPTANCE, start="2026-06-02")])
        result = evaluate_person(person, make_case())
        self.assertFalse(result["blocked"])
        self.assertIn(LINK_ACCEPTANCE, result["outside_window"])


class IndependentKindsTests(unittest.TestCase):
    """三类关系必须分别判断：不同人员各由一类关系命中，且全部三类都在 checked_kinds。"""

    def test_each_kind_independently_blocks(self):
        cases = [
            ("P-EMP", link(LINK_EMPLOYMENT, start="2026-05-01", end="2026-05-10")),
            ("P-KIN", link(LINK_KIN, start="2026-05-05")),
            ("P-ACC", link(LINK_ACCEPTANCE, start="2026-05-08")),
        ]
        for pid, rel in cases:
            result = evaluate_person(make_person(pid, [rel]), make_case())
            self.assertTrue(result["blocked"], pid)
            self.assertEqual(result["active"][rel.kind][0]["kind"], rel.kind)
            self.assertEqual(result["checked_kinds"],
                             [LINK_EMPLOYMENT, LINK_KIN, LINK_ACCEPTANCE])

    def test_other_project_relations_ignored(self):
        person = make_person("P", [
            link(LINK_EMPLOYMENT, project_id="PRJ-OTHER", start="2026-05-01", end="2026-05-31"),
            link(LINK_KIN, project_id="PRJ-OTHER", start="2026-05-05"),
            link(LINK_ACCEPTANCE, project_id="PRJ-OTHER", start="2026-05-08"),
        ])
        result = evaluate_person(person, make_case(project_id="PRJ-X"))
        self.assertFalse(result["blocked"])
        self.assertEqual(result["active"], {})

    def test_blocked_by_employment_but_other_kinds_still_classified(self):
        # 任职命中时，亲属窗口外关系仍应保留在 outside_window，证明三类均被执行
        person = make_person("P", [
            link(LINK_EMPLOYMENT, start="2026-05-01", end="2026-05-10"),
            link(LINK_KIN, start="2026-04-10"),
            link(LINK_ACCEPTANCE, start="2026-05-25"),
        ])
        result = evaluate_person(person, make_case())
        self.assertTrue(result["blocked"])
        self.assertEqual(set(result["active"]), {LINK_EMPLOYMENT, LINK_ACCEPTANCE})
        self.assertIn(LINK_KIN, result["outside_window"])


if __name__ == "__main__":
    unittest.main()
