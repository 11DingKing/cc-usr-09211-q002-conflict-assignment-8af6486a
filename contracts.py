"""数据契约：读取人员履历、工程关系与调查案件样本。

日期统一为本地自然日字符串 ``YYYY-MM-DD``；履历起止日均为闭区间端点。
原始记录按来源顺序读取，字段值不在读取时改写，仅做结构与取值校验。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import json
from pathlib import Path

# 三类法定/约定回避关系
LINK_EMPLOYMENT = "employment"        # 曾在该工程任职
LINK_KIN = "kin_declaration"          # 近亲属从业申报
LINK_ACCEPTANCE = "acceptance_signoff"  # 曾签署验收意见
LINK_TYPES = (LINK_EMPLOYMENT, LINK_KIN, LINK_ACCEPTANCE)

# 每案至少覆盖的两类岗位资质
ROLE_SITE_TECH = "site_tech"          # 现场技术
ROLE_MATERIAL = "material_review"     # 材料审查
CASE_ROLES = (ROLE_SITE_TECH, ROLE_MATERIAL)

EMERGENCY_LEVELS = ("normal", "urgent", "critical")
VALID_VERDICTS = ("pass", "conditional", "fail")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _is_date(value):
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        return False
    try:
        year, month, day = (int(part) for part in value.split("-"))
        import datetime
        datetime.date(year, month, day)
    except (ValueError, TypeError):
        return False
    return True


@dataclass(frozen=True)
class ProjectLink:
    """人员与某个工程之间的一条关系记录。

    kind 取 LINK_TYPES 之一；project_id 指向案件对应的工程。
    effective_date 为关系生效日（任职起始/申报受理/验收签署日）。
    end_date 仅任职关系使用，表示任职结束日；其余关系为闭区间单日关系。
    evidence 为来源编号，便于关系版本核对。
    """

    kind: str
    project_id: str
    effective_date: str
    end_date: str | None = None
    evidence: str = ""

    def __post_init__(self):
        _require(self.kind in LINK_TYPES, f"未知关系类型: {self.kind!r}")
        _require(isinstance(self.project_id, str) and self.project_id, "project_id 不能为空")
        _require(_is_date(self.effective_date), f"生效日期格式应为 YYYY-MM-DD: {self.effective_date!r}")
        if self.kind == LINK_EMPLOYMENT:
            _require(self.end_date is not None and _is_date(self.end_date),
                     "任职关系必须提供合法 end_date")
            _require(self.effective_date <= self.end_date, "任职起始日不得晚于结束日")
        else:
            # 非任职关系按单日生效日处理；若提供 end_date 必须不早于生效日
            if self.end_date is not None:
                _require(_is_date(self.end_date), "end_date 格式应为 YYYY-MM-DD")
                _require(self.effective_date <= self.end_date, "生效日不得晚于 end_date")


@dataclass(frozen=True)
class SourceRecord:
    """外部核验人员：资质、与工程的关系履历、容量、所在区域。"""

    person_id: str
    qualifications: list
    project_links: list
    capacity: int
    region: str = ""

    def __post_init__(self):
        _require(isinstance(self.person_id, str) and self.person_id, "person_id 不能为空")
        _require(isinstance(self.qualifications, list) and self.qualifications,
                 "qualifications 必须是非空数组")
        for q in self.qualifications:
            _require(isinstance(q, str) and q, "资质项必须是非空字符串")
        _require(isinstance(self.project_links, list), "project_links 必须是数组")
        _require(isinstance(self.capacity, int) and not isinstance(self.capacity, bool)
                 and self.capacity >= 0, "capacity 必须是非负整数")
        _require(isinstance(self.region, str), "region 必须是字符串")
        # dataclass(frozen=True) 下在 __post_init__ 中做结构化转换；
        # 兼容 dataclasses.replace 传入已是 ProjectLink 的情况
        links = []
        for item in self.project_links:
            links.append(item if isinstance(item, ProjectLink) else ProjectLink(**item))
        object.__setattr__(self, "project_links", links)

    def links_for(self, project_id):
        return [link for link in self.project_links if link.project_id == project_id]


@dataclass(frozen=True)
class CaseRecord:
    """调查案件：调查窗口、工程、地区、紧急程度与必备岗位资质。"""

    case_id: str
    project_id: str
    window_start: str
    window_end: str
    region: str = ""
    emergency: str = "normal"
    required_roles: list = field(default_factory=lambda: list(CASE_ROLES))

    def __post_init__(self):
        _require(isinstance(self.case_id, str) and self.case_id, "case_id 不能为空")
        _require(isinstance(self.project_id, str) and self.project_id, "project_id 不能为空")
        _require(_is_date(self.window_start), "window_start 应为 YYYY-MM-DD")
        _require(_is_date(self.window_end), "window_end 应为 YYYY-MM-DD")
        _require(self.window_start <= self.window_end, "调查窗口起始不得晚于结束")
        _require(self.emergency in EMERGENCY_LEVELS, f"紧急程度取值非法: {self.emergency!r}")
        roles = list(self.required_roles)
        _require(len(roles) == len(set(roles)), "必备岗位不得重复")
        _require(set(roles) <= set(CASE_ROLES), "存在系统不支持的岗位")
        # 业务底线：现场技术与材料审查两类资质每案必须覆盖
        _require(set(CASE_ROLES) <= set(roles), "每案至少覆盖现场技术与材料审查两类资质")
        object.__setattr__(self, "required_roles", roles)


def read_records(path):
    """读取人员样本（SourceRecord 数组）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("记录集合必须是数组")
    return [SourceRecord(**item) for item in raw]


def read_cases(path):
    """读取案件样本（CaseRecord 数组）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("案件集合必须是数组")
    return [CaseRecord(**item) for item in raw]


def to_mapping(record):
    data = asdict(record)
    return data
