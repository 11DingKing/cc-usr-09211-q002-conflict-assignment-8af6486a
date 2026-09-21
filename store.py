"""线程安全的分派存储：方案版本、容量复核、确认互斥、撤换与意见留痕。

关键规则：
  * 方案生成时固化所涉人员的关系版本；负责人确认时再次核对版本与容量，
    任一不符即拒绝（409），必须基于最新数据重新生成方案；
  * 确认动作全程持锁，两个负责人同时争抢同一人员时按容量裁决，只成功一个；
  * 调查开始后新发现且窗口内生效的利益关系，立即撤换当事人在担岗位，
    原历史意见保留但标记 needs_recheck，并自动生成仅补空缺岗位的替换方案；
  * 撤换释放的容量立即归还，被撤换人员因新关系命中而自动退出候选。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from contracts import (
    ProjectLink,
    SourceRecord,
    CaseRecord,
    CASE_ROLES,
    VALID_VERDICTS,
)
from planning import plan_case
from recusal import classify_link


class StoreError(Exception):
    status = 400

    def __init__(self, message, code=None, details=None):
        super().__init__(message)
        self.message = message
        self.code = code or "bad_request"
        self.details = details or {}


class NotFoundError(StoreError):
    status = 404

    def __init__(self, message, code="not_found"):
        super().__init__(message, code=code)


class ConflictError(StoreError):
    status = 409


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Assignment:
    role: str
    person_id: str
    status: str = "active"            # active / removed
    reviewer: str = ""
    confirmed_at: str = ""
    removed_at: str | None = None
    removed_reason: str | None = None
    replacement_for: str | None = None  # 补位时标记原撤换记录序号


@dataclass
class Opinion:
    opinion_id: str
    case_id: str
    role: str
    author_person_id: str
    scope: str
    verdict: str
    created_at: str
    status: str = "valid"             # valid / needs_recheck
    flag_reason: str | None = None


class Store:
    def __init__(self, persons, cases):
        self._lock = threading.RLock()
        self._persons = {p.person_id: p for p in persons}
        self._person_order = [p.person_id for p in persons]
        self._cases = {c.case_id: c for c in cases}
        self._extra_links: dict[str, list[ProjectLink]] = {pid: [] for pid in self._persons}
        self._seq = 0

        # case_id -> {"plan", "status": proposed/confirmed/replacement_proposed, ...}
        self._proposals: dict[str, dict] = {}
        self._assignments: dict[str, list[Assignment]] = {}
        self._opinions: dict[str, list[Opinion]] = {}

    # ---------- 基础读取 ----------

    def _effective_person(self, person_id):
        record = self._persons[person_id]
        extras = self._extra_links.get(person_id, [])
        if not extras:
            return record
        return dataclasses.replace(record, project_links=record.project_links + list(extras))

    def _persons_snapshot(self):
        return [self._effective_person(pid) for pid in self._person_order]

    def person_version(self, person_id):
        """关系版本：对该人员全部关系记录的规范化哈希。"""
        record = self._effective_person(person_id)
        material = json.dumps(
            [asdict(link) for link in record.project_links],
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]

    def _assigned_versions(self, person_ids):
        return {pid: self.person_version(pid) for pid in person_ids}

    def _active_load(self):
        """统计每名人员当前仍在担（未撤换）的确认分工数。"""
        load = {pid: 0 for pid in self._persons}
        for assignments in self._assignments.values():
            for item in assignments:
                if item.status == "active":
                    load[item.person_id] += 1
        return load

    def list_persons(self):
        with self._lock:
            result = []
            for pid in self._person_order:
                record = self._effective_person(pid)
                result.append({
                    **{k: v for k, v in asdict(record).items()},
                    "relation_version": self.person_version(pid),
                    "active_load": self._active_load()[pid],
                })
            return result

    def list_cases(self):
        with self._lock:
            return [asdict(c) for c in self._cases.values()]

    def case_view(self, case_id):
        with self._lock:
            case = self._require_case(case_id)
            view = {
                "case": asdict(case),
                "state": self._case_state(case_id),
                "assignments": [asdict(a) for a in self._assignments.get(case_id, [])],
                "opinions": [asdict(o) for o in self._opinions.get(case_id, [])],
                "proposal": self._public_proposal(case_id),
            }
            return view

    def _require_case(self, case_id):
        try:
            return self._cases[case_id]
        except KeyError:
            raise NotFoundError(f"案件不存在: {case_id}")

    def _case_state(self, case_id):
        assignments = self._assignments.get(case_id, [])
        proposal = self._proposals.get(case_id)
        if not assignments and not proposal:
            return "open"
        if proposal and proposal["status"] == "proposed":
            return "awaiting_confirmation"
        active = [a for a in assignments if a.status == "active"]
        removed = [a for a in assignments if a.status == "removed"]
        if removed and proposal and proposal["status"] == "replacement_proposed":
            return "replacing"
        if removed and len(active) < len(self._cases[case_id].required_roles):
            return "blocked_pending_replacement"
        if active:
            return "in_progress"
        return "open"

    def _public_proposal(self, case_id):
        proposal = self._proposals.get(case_id)
        if proposal is None:
            return None
        return {
            "status": proposal["status"],
            "reviewer": proposal.get("reviewer"),
            "created_at": proposal["created_at"],
            "person_versions": proposal["person_versions"],
            "plan": proposal["plan"],
        }

    # ---------- 方案生成 ----------

    def create_plan(self, case_id):
        with self._lock:
            case = self._require_case(case_id)
            if self._assignments.get(case_id):
                raise ConflictError("案件已有确认分工，补位方案由新发现关系自动生成",
                                    code="team_confirmed")
            result = plan_case(case, self._persons_snapshot(), self._active_load())
            proposal = {
                "status": "proposed",
                "created_at": _now(),
                "person_versions": self._assigned_versions(
                    [a["person_id"] for a in result["assignments"]]
                ) if result["feasible"] else {},
                "plan": result,
                "reviewer": None,
            }
            self._proposals[case_id] = proposal
            return self._public_proposal(case_id)

    # ---------- 确认 ----------

    def confirm_plan(self, case_id, reviewer, expected_versions=None):
        with self._lock:
            case = self._require_case(case_id)
            proposal = self._proposals.get(case_id)
            if proposal is None:
                raise NotFoundError(f"案件尚无待确认方案: {case_id}", code="no_proposal")
            if proposal["status"] not in ("proposed", "replacement_proposed"):
                raise ConflictError("方案已确认，不得重复确认", code="already_confirmed")
            plan = proposal["plan"]
            if not plan["feasible"]:
                raise ConflictError("不可行方案不能确认", code="infeasible_plan")

            proposed_pids = [a["person_id"] for a in plan["assignments"]
                             if not a.get("fixed")]
            all_pids = [a["person_id"] for a in plan["assignments"]]

            # 1) 关系版本核对：固化版本 vs 当前版本
            current_versions = self._assigned_versions(all_pids)
            if current_versions != proposal["person_versions"]:
                changed = {pid: {"proposed": proposal["person_versions"].get(pid),
                                 "current": current_versions.get(pid)}
                           for pid in all_pids
                           if current_versions.get(pid) != proposal["person_versions"].get(pid)}
                raise ConflictError("生成后人员关系数据已变更，须基于最新版本重新生成方案",
                                    code="relation_version_stale", details={"changed": changed})
            if expected_versions is not None and expected_versions != current_versions:
                raise ConflictError("确认请求携带的关系版本与当前版本不一致",
                                    code="client_version_stale",
                                    details={"current": current_versions})

            # 2) 关系与资质实时复核（不依赖版本哈希的二道校验）
            load = self._active_load()
            persons_by_id = {p.person_id: p for p in self._persons_snapshot()}
            for assignment in plan["assignments"]:
                pid = assignment["person_id"]
                person = persons_by_id[pid]
                if assignment["role"] not in person.qualifications:
                    raise ConflictError(f"{pid} 已不具备岗位资质 {assignment['role']}",
                                        code="qualification_changed")
                from recusal import evaluate_person
                if evaluate_person(person, case)["blocked"]:
                    raise ConflictError(f"{pid} 当前命中窗口内回避关系",
                                        code="recusal_changed")

            # 3) 容量复核：新占岗位（非固定）逐人校验
            occupiers = {}
            for pid in proposed_pids:
                occupiers[pid] = occupiers.get(pid, 0) + 1
            for pid, need in occupiers.items():
                person = persons_by_id[pid]
                used = load[pid]
                if used + need > person.capacity:
                    competitors = []
                    for other_id, assignments in self._assignments.items():
                        if other_id == case_id:
                            continue
                        for item in assignments:
                            if item.status == "active" and item.person_id == pid:
                                competitors.append(other_id)
                    raise ConflictError(
                        f"人员 {pid} 容量不足：在担 {used} 件，本次需 {need} 件，容量 {person.capacity}",
                        code="capacity_conflict",
                        details={"person_id": pid, "active_load": used,
                                 "capacity": person.capacity,
                                 "competing_cases": sorted(set(competitors))},
                    )

            # 4) 落位：固定岗位已存在，仅登记新岗位（两个确认并发时由锁保证只赢一个）
            existing = self._assignments.setdefault(case_id, [])
            existing_roles = {a.role for a in existing if a.status == "active"}
            now = _now()
            for assignment in plan["assignments"]:
                if assignment.get("fixed") or assignment["role"] in existing_roles:
                    continue
                existing.append(Assignment(
                    role=assignment["role"],
                    person_id=assignment["person_id"],
                    reviewer=reviewer,
                    confirmed_at=now,
                ))
            proposal["status"] = "confirmed"
            proposal["reviewer"] = reviewer
            proposal["confirmed_at"] = now
            return {
                "case_id": case_id,
                "state": self._case_state(case_id),
                "confirmed_by": reviewer,
                "confirmed_at": now,
                "person_versions": current_versions,
                "assignments": [asdict(a) for a in self._assignments[case_id]],
            }

    # ---------- 新发现利益关系 -> 撤换 + 补位 ----------

    def register_relation(self, person_id, link_payload, *, source="late_disclosure"):
        with self._lock:
            if person_id not in self._persons:
                raise NotFoundError(f"人员不存在: {person_id}")
            link = ProjectLink(**link_payload)
            self._seq += 1
            discovery_seq = self._seq
            self._extra_links[person_id].append(link)
            entry = {
                "seq": discovery_seq,
                "person_id": person_id,
                "source": source,
                "discovered_at": _now(),
                "link": asdict(link),
                "relation_version_after": self.person_version(person_id),
            }

            affected = []
            for case_id, case in self._cases.items():
                if link.project_id != case.project_id:
                    continue
                in_window = classify_link(link, case.window_start, case.window_end) == "active"
                assignments = self._assignments.get(case_id, [])
                hit = [a for a in assignments
                       if a.status == "active" and a.person_id == person_id]
                pending = self._proposals.get(case_id)
                if not hit:
                    # 本人不在担，但可能是待确认补位方案中的拟派人员：
                    # 新关系使其版本变化/被回避，须作废并自动重建补位方案
                    if pending and pending["status"] == "replacement_proposed":
                        proposed_pids = {a["person_id"]
                                         for a in pending["plan"].get("assignments", [])
                                         if not a.get("fixed")}
                        if person_id in proposed_pids:
                            self._proposals.pop(case_id, None)
                            affected.append({
                                "case_id": case_id,
                                "action": "replacement_rebuilt",
                                "person_id": person_id,
                                "in_window": in_window,
                                "replacement": self._build_replacement(case),
                            })
                    continue
                if not in_window:
                    # 同工程但窗口外：不触发撤换，留痕说明，绝不静默
                    affected.append({"case_id": case_id, "action": "not_recused_outside_window",
                                     "link": asdict(link)})
                    continue

                removed_opinions = []
                for assignment in hit:
                    assignment.status = "removed"
                    assignment.removed_at = _now()
                    assignment.removed_reason = (
                        f"调查中新发现窗口内关系 {link.kind}（生效日 {link.effective_date}，"
                        f"证据 {link.evidence or '无编号'}，发现序号 #{discovery_seq}）"
                    )
                    # 历史意见保留原件，仅把需重新核验的部分标出
                    for opinion in self._opinions.get(case_id, []):
                        if (opinion.author_person_id == person_id
                                and opinion.role == assignment.role
                                and opinion.status == "valid"):
                            opinion.status = "needs_recheck"
                            opinion.flag_reason = (
                                f"签署人 {person_id} 因新发现 {link.kind} 关系被撤换"
                                f"（发现序号 #{discovery_seq}），该岗位此前意见覆盖部分须重新核验"
                            )
                            removed_opinions.append(opinion.opinion_id)

                affected.append({
                    "case_id": case_id,
                    "action": "removed",
                    "removed_roles": sorted({a.role for a in hit}),
                    "person_id": person_id,
                    "flagged_opinion_ids": removed_opinions,
                })

                # 作废可能存在的待确认方案，生成仅补空缺的替换方案
                self._proposals.pop(case_id, None)
                replacement = self._build_replacement(case)
                affected[-1]["replacement"] = replacement

            return {"discovery": entry, "affected_cases": affected}

    def _build_replacement(self, case):
        assignments = self._assignments[case.case_id]
        active = {a.role: a.person_id for a in assignments if a.status == "active"}
        removed_roles = [a.role for a in assignments if a.status == "removed"
                         and a.role not in active]
        if not removed_roles:
            return {"status": "none"}
        result = plan_case(
            case, self._persons_snapshot(), self._active_load(),
            fixed=active,
        )
        proposal = {
            "status": "replacement_proposed",
            "created_at": _now(),
            "person_versions": self._assigned_versions(
                [a["person_id"] for a in result["assignments"]]
            ) if result["feasible"] else {},
            "plan": result,
            "reviewer": None,
            "removed_roles": removed_roles,
        }
        self._proposals[case.case_id] = proposal
        return self._public_proposal(case.case_id)

    # ---------- 历史意见 ----------

    def record_opinion(self, case_id, role, author_person_id, scope, verdict):
        with self._lock:
            case = self._require_case(case_id)
            if role not in case.required_roles:
                raise StoreError(f"岗位 {role} 不属于本案分工", code="unknown_role")
            if verdict not in VALID_VERDICTS:
                raise StoreError(f"结论取值非法: {verdict}", code="bad_verdict")
            if not isinstance(scope, str) or not scope.strip():
                raise StoreError("scope 须说明意见覆盖的核验部分", code="bad_scope")
            assignments = self._assignments.get(case_id, [])
            current = next((a for a in assignments
                            if a.role == role and a.status == "active"), None)
            if current is None:
                raise ConflictError("该岗位当前无在任人员，不能登记意见",
                                    code="role_unassigned")
            if current.person_id != author_person_id:
                raise ConflictError(
                    f"意见登记人 {author_person_id} 不是岗位 {role} 的在任人员 {current.person_id}",
                    code="not_assignee",
                )
            self._seq += 1
            opinion = Opinion(
                opinion_id=f"op-{self._seq}",
                case_id=case_id,
                role=role,
                author_person_id=author_person_id,
                scope=scope.strip(),
                verdict=verdict,
                created_at=_now(),
            )
            self._opinions.setdefault(case_id, []).append(opinion)
            return asdict(opinion)
