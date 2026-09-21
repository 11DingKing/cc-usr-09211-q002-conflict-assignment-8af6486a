from dataclasses import dataclass, asdict
import json
from pathlib import Path

@dataclass(frozen=True)
class SourceRecord:
    person_id: str
    qualifications: list
    project_links: list
    capacity: int

def read_records(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("记录集合必须是数组")
    return [SourceRecord(**item) for item in raw]

def to_mapping(record):
    return asdict(record)
