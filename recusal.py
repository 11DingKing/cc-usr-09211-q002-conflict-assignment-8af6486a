"""回避关系判定。

三类关系分别判断，互不合并、互不省略：

1. ``employment`` 任职：履历为闭区间 [effective_date, end_date]，
   与调查窗口 [window_start, window_end] 存在含端点的重叠即回避；
2. ``kin_declaration`` 近亲属申报：申报生效日落入窗口（含端点）才回避；
3. ``acceptance_signoff`` 验收意见签署：签署日落入窗口（含端点）才回避。

只统计与本案工程 project_id 相同的关系记录；他工程履历不影响本案。
关系生效日落在窗口外的记录进入 outside_window 桶并保留，供核验解释。
"""
from __future__ import annotations

from contracts import (
    LINK_EMPLOYMENT,
    LINK_KIN,
    LINK_ACCEPTANCE,
)


def _in_window(date_str, window_start, window_end):
    return window_start <= date_str <= window_end


def _employment_overlaps(link, window_start, window_end):
    # 闭区间相交判定，起止日都包含
    return link.effective_date <= window_end and link.end_date >= window_start


def classify_link(link, window_start, window_end):
    """返回 'active'（窗口内，产生回避）或 'outside_window'（窗口外，保留展示）。"""
    if link.kind == LINK_EMPLOYMENT:
        return "active" if _employment_overlaps(link, window_start, window_end) else "outside_window"
    # 单日生效关系：申报受理日 / 验收签署日落入窗口才生效
    return "active" if _in_window(link.effective_date, window_start, window_end) else "outside_window"


def evaluate_person(record, case):
    """判定单个人员对单案的三类关系，按关系类型分别给出桶与命中明细。

    返回 dict：
      blocked: bool
      active: {kind: [link 映射...]}  仅窗口内关系
      outside_window: {kind: [...]}   窗口外但同工程的关系，保留说明
      checked_kinds: 三类关系全部列出，证明每一类都被独立判断
    """
    buckets = {
        "active": {kind: [] for kind in (LINK_EMPLOYMENT, LINK_KIN, LINK_ACCEPTANCE)},
        "outside_window": {kind: [] for kind in (LINK_EMPLOYMENT, LINK_KIN, LINK_ACCEPTANCE)},
    }
    for link in record.links_for(case.project_id):
        bucket = classify_link(link, case.window_start, case.window_end)
        buckets[bucket][link.kind].append({
            "kind": link.kind,
            "project_id": link.project_id,
            "effective_date": link.effective_date,
            "end_date": link.end_date,
            "evidence": link.evidence,
        })

    active = {kind: items for kind, items in buckets["active"].items() if items}
    outside = {kind: items for kind, items in buckets["outside_window"].items() if items}
    return {
        "person_id": record.person_id,
        "case_id": case.case_id,
        "project_id": case.project_id,
        "blocked": bool(active),
        "active": active,
        "outside_window": outside,
        "checked_kinds": [LINK_EMPLOYMENT, LINK_KIN, LINK_ACCEPTANCE],
    }


def is_recused(record, case):
    """只要三类中任意一类在窗口内命中即回避；窗口外关系不放宽也不误伤。"""
    return evaluate_person(record, case)["blocked"]
