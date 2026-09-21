"""调查团队规划与不可行诊断。

硬约束（任一不满足即不得入队）：
  * 岗位资质匹配（现场技术 / 材料审查分别核对）；
  * 三类回避关系在调查窗口内均无命中（窗口外的关系不放宽、也不遮掩）；
  * 确认在担案件量 + 本案分工数 ≤ 个人容量；
  * 两个岗位必须由不同人员担任，保证两类审查相互独立。

软偏好（用于在合格组合之间排序，全部可解释）：
  * 跨区支援人数越少越好，紧急程度越高，跨区延误权重越大；
  * 在担负载越轻、余量越大的人员优先，团队负载尽量均衡。

无合格组合时，按 Hall 定理给出包含最小的岗位集合 S，使
|可胜任候选人并集 N(S)| < |S|，并逐人列出被排除的具体约束，
同时附带三类回避关系的全量核查痕迹，证明没有任何一类被省略。
"""
from __future__ import annotations

from itertools import combinations, product

from contracts import CASE_ROLES
from recusal import evaluate_person

# 紧急程度对跨区延误的放大系数：越紧急越不宜长途跨区
URGENCY_WEIGHT = {"normal": 1, "urgent": 2, "critical": 3}
CROSS_REGION_COST = 10  # 每名跨区成员的基础代价（与负载边际代价同量纲展示）


def _role_qualification(role):
    return {
        "site_tech": "site_tech",
        "material_review": "material_review",
    }[role]


def candidate_audit(persons, case, loads):
    """逐岗位、逐人员给出资格判定与三类关系核查结果。

    返回 {role: [{person_id, eligible, reason, recusal, load, capacity, slack, region}]}
    """
    audit = {}
    for role in case.required_roles:
        qualification = _role_qualification(role)
        entries = []
        for person in persons:
            evaluation = evaluate_person(person, case)
            load = loads.get(person.person_id, 0)
            slack = person.capacity - load
            entry = {
                "person_id": person.person_id,
                "region": person.region,
                "load": load,
                "capacity": person.capacity,
                "slack": slack,
                "has_qualification": qualification in person.qualifications,
                "recusal": evaluation,
                "eligible": False,
                "reason": None,
            }
            if qualification not in person.qualifications:
                entry["reason"] = "missing_qualification"
            elif evaluation["blocked"]:
                entry["reason"] = "recused"
            elif slack < 1:
                entry["reason"] = "no_capacity"
            else:
                entry["eligible"] = True
                entry["reason"] = "eligible"
            entries.append(entry)
        audit[role] = entries
    return audit


def _eligible_ids(audit):
    return {role: [e["person_id"] for e in entries if e["eligible"]]
            for role, entries in audit.items()}


def _minimal_deficient_sets(role_lists):
    """枚举包含最小（inclusion-minimal）的 Hall 违约岗位子集。"""
    roles = list(role_lists)
    deficient = []
    for size in range(1, len(roles) + 1):
        for subset in combinations(roles, size):
            # 若已包含在更小的违约集合中，则不是最小
            if any(set(smaller) <= set(subset) for smaller in deficient):
                continue
            union = set()
            for role in subset:
                union.update(role_lists[role])
            if len(union) < len(subset):
                deficient.append(subset)
    return deficient


def _exclusion_detail(audit, roles):
    """对违约岗位集合涉及的全部非候选人，逐人列出排除约束。"""
    details = {}
    for role in roles:
        items = []
        for entry in audit[role]:
            if entry["eligible"]:
                continue
            item = {
                "person_id": entry["person_id"],
                "constraint": entry["reason"],
                "load": entry["load"],
                "capacity": entry["capacity"],
            }
            if entry["reason"] == "recused":
                item["active_relation_kinds"] = sorted(entry["recusal"]["active"].keys())
                item["active_relations"] = entry["recusal"]["active"]
            items.append(item)
        details[role] = items
    return details


def build_diagnosis(audit):
    """构造不可行诊断：最小 Hall 违约岗位集 + 逐人排除约束 + 三类核查痕迹。"""
    role_lists = _eligible_ids(audit)
    deficient_sets = _minimal_deficient_sets(role_lists)
    reports = []
    involved_roles = set()
    for subset in deficient_sets:
        union = set()
        for role in subset:
            union.update(role_lists[role])
        reports.append({
            "roles": list(subset),
            "candidate_union": sorted(union),
            "required_seats": len(subset),
            "available_candidates": len(union),
            "shortage": len(subset) - len(union),
        })
        involved_roles.update(subset)
    if not involved_roles:
        # 理论上不可行时必有违约集合；兜底覆盖全岗位
        involved_roles = set(audit)
    # 全量核查痕迹：每个被排除人员的三类关系分别结论
    recusal_trace = []
    for role in involved_roles:
        for entry in audit[role]:
            evaluation = entry["recusal"]
            recusal_trace.append({
                "role": role,
                "person_id": entry["person_id"],
                "checked_kinds": evaluation["checked_kinds"],
                "active_kinds": sorted(evaluation["active"].keys()),
                "outside_window_kinds": sorted(evaluation["outside_window"].keys()),
            })
    return {
        "code": "no_feasible_team",
        "message": "不存在同时满足资质、回避与容量的分工组合",
        "minimal_constraint_cores": reports,
        "exclusions": _exclusion_detail(audit, sorted(involved_roles)),
        "recusal_trace": recusal_trace,
        "note": "三类关系（任职、近亲属申报、验收签署）均已逐人独立判定，未省略任何一类",
    }


def _fixed_assignment(role, pid, persons_by_id, loads, case, case_ok):
    person = persons_by_id[pid]
    same_region = person.region == case.region
    entry = {
        "role": role,
        "person_id": pid,
        "region": person.region,
        "cross_region": not same_region,
        "fixed": True,
        "reasons": ["撤换补位：该岗位原成员仍在岗，保持不变"],
    }
    if case_ok:
        entry["reasons"].append("任职、近亲属申报、验收签署三类关系复核仍无窗口内命中")
    else:
        entry["reasons"].append("警告：固定成员当前复核未通过，需要人工介入")
    return entry


def plan_case(case, persons, loads, *, max_combinations=20000, fixed=None):
    """为单案生成分工方案。

    persons: SourceRecord 列表；loads: 已确认在担案件量 person_id -> int。
    fixed: 撤换补位时仍在岗的 {role: person_id}，这些岗位直接保留、不参与选择，
           且新候选人不得与固定人员重复。
    返回 feasible 方案（含打分与逐条理由）或 infeasible 诊断。
    """
    fixed = dict(fixed or {})
    persons_by_id = {p.person_id: p for p in persons}
    full_audit = candidate_audit(persons, case, loads)
    roles = list(case.required_roles)
    open_roles = [role for role in roles if role not in fixed]
    fixed_pids = set(fixed.values())

    # 补位模式下仅针对空缺岗位做枚举与诊断，但候选要排除已占其他岗位的固定人员
    audit = {role: [entry for entry in full_audit[role]
                    if entry["person_id"] not in fixed_pids]
             for role in open_roles}
    role_lists = _eligible_ids(audit)

    candidates = []
    for combo in product(*(role_lists[role] for role in open_roles)):
        if len(set(combo)) != len(combo):
            continue  # 同一人员不得兼任两个岗位
        candidates.append(combo)
        if len(candidates) > max_combinations:
            raise RuntimeError("候选组合数超出上限，需扩大求解器")

    if not candidates:
        return {
            "feasible": False,
            "case_id": case.case_id,
            "project_id": case.project_id,
            "replacement_for_roles": open_roles,
            "fixed_roles": [{"role": r, "person_id": pid} for r, pid in fixed.items()],
            "diagnosis": build_diagnosis(audit),
            "audit": audit,
        }

    urgency = URGENCY_WEIGHT[case.emergency]

    def score(combo):
        cross = sum(1 for pid in combo if persons_by_id[pid].region != case.region)
        # 边际负载代价 (load+1)^2 - load^2 = 2*load+1，鼓励把新任务分给低负载者
        marginal = sum(2 * loads.get(pid, 0) + 1 for pid in combo)
        weighted_cross = cross * CROSS_REGION_COST * urgency
        return weighted_cross + marginal, cross, marginal

    scored = sorted(((score(combo), combo) for combo in candidates),
                    key=lambda item: (item[0][0], item[1]))
    (total, cross, marginal), best = scored[0]

    # 固定成员的跨区数与负载仅用于展示，不参与本次选择打分
    fixed_cross = sum(1 for pid in fixed.values()
                      if persons_by_id[pid].region != case.region)

    assignments = []
    for role, pid in fixed.items():
        person = persons_by_id[pid]
        case_ok = not next(
            e for e in full_audit[role] if e["person_id"] == pid
        )["recusal"]["blocked"]
        assignments.append(_fixed_assignment(
            role, pid, persons_by_id, loads, case, case_ok))
    for role, pid in zip(open_roles, best):
        person = persons_by_id[pid]
        same_region = person.region == case.region
        reasons = [
            f"持有岗位资质 {_role_qualification(role)}",
            "任职、近亲属申报、验收签署三类关系在调查窗口内均无命中",
            f"当前在担 {loads.get(pid, 0)} 件 / 容量 {person.capacity}，确认后仍有余量",
        ]
        reasons.append("与案件同区域，无跨区支援成本" if same_region
                       else f"由 {person.region or '未登记区域'} 跨区支援 {case.region or '未登记区域'}")
        if fixed:
            reasons.append("补位人选已避开仍在岗成员，避免同一人兼任")
        assignments.append({
            "role": role,
            "person_id": pid,
            "region": person.region,
            "cross_region": not same_region,
            "reasons": reasons,
        })

    result = {
        "feasible": True,
        "case_id": case.case_id,
        "project_id": case.project_id,
        "emergency": case.emergency,
        "assignments": assignments,
        "score": {
            "total": total,
            "cross_region_members": cross + fixed_cross,
            "cross_region_weighted": cross * CROSS_REGION_COST * urgency,
            "load_marginal": marginal,
            "urgency_weight": urgency,
        },
        "selection_reasons": [
            f"在 {len(candidates)} 个合格组合中总分最低",
            f"新增成员跨区支援 {cross} 人；紧急程度 {case.emergency} 对跨区代价的权重为 {urgency}",
            "各岗位由不同人员担任，现场技术与材料审查相互独立",
            "所有候选均已通过三类回避关系的窗口内判定，窗口外关系仅记录不放宽",
        ],
        "alternatives_considered": len(candidates),
        "audit": full_audit,
    }
    if fixed:
        result["replacement_for_roles"] = open_roles
        result["fixed_roles"] = [{"role": r, "person_id": pid} for r, pid in fixed.items()]
    return result
