"""调查回避分派 HTTP 接口。

路由：
  GET  /health                       健康探针
  GET  /persons                      人员履历、关系版本与当前在担量
  GET  /cases                        案件清单
  GET  /cases/{case_id}              案件全貌：分工、意见、待确认方案
  POST /cases/{case_id}/plans        生成分工方案（可行方案或不可行最小约束核）
  POST /cases/{case_id}/confirmations  负责人确认（复核关系版本与容量；并发争抢只胜一个）
  POST /persons/{person_id}/relations  登记调查中新发现的利益关系（触发撤换与补位）
  POST /cases/{case_id}/opinions     登记核验意见（撤换后历史意见保留并标记重核）

数据路径可用 PERSONS_FILE / CASES_FILE 环境变量覆盖。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from contracts import read_records, read_cases
from store import Store, StoreError

BASE_DIR = Path(__file__).resolve().parent
PERSONS_FILE = os.environ.get("PERSONS_FILE", str(BASE_DIR / "data" / "example.json"))
CASES_FILE = os.environ.get("CASES_FILE", str(BASE_DIR / "data" / "cases.json"))

_STORE = None


def get_store():
    global _STORE
    if _STORE is None:
        _STORE = Store(read_records(PERSONS_FILE), read_cases(CASES_FILE))
    return _STORE


def reset_store():
    """测试辅助：清空进程内状态，重新读取样本。"""
    global _STORE
    _STORE = None
    return get_store()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默常规访问日志
        return

    # ---------- 响应工具 ----------

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StoreError(f"请求体不是合法 JSON: {exc}", code="invalid_json")
        if not isinstance(payload, dict):
            raise StoreError("请求体必须是 JSON 对象", code="invalid_json")
        return payload

    # ---------- 路由 ----------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        try:
            store = get_store()
            if path == "/persons":
                self._write_json(200, {"persons": store.list_persons()})
            elif path == "/cases":
                self._write_json(200, {"cases": store.list_cases()})
            elif path.startswith("/cases/"):
                case_id = path[len("/cases/"):].rstrip("/")
                self._write_json(200, store.case_view(case_id))
            else:
                self._write_json(404, {"error": "接口不存在"})
        except StoreError as exc:
            self._write_json(exc.status, {"error": exc.message, "code": exc.code, **exc.details})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            store = get_store()
            body = self._read_body()
            if path.startswith("/cases/") and path.endswith("/plans"):
                case_id = path[len("/cases/"):-len("/plans")].strip("/")
                self._write_json(200, {"proposal": store.create_plan(case_id)})
            elif path.startswith("/cases/") and path.endswith("/confirmations"):
                case_id = path[len("/cases/"):-len("/confirmations")].strip("/")
                reviewer = body.get("reviewer")
                if not isinstance(reviewer, str) or not reviewer.strip():
                    raise StoreError("reviewer 必须是非空字符串", code="bad_reviewer")
                expected = body.get("expected_versions")
                if expected is not None and not isinstance(expected, dict):
                    raise StoreError("expected_versions 必须是 person_id -> version 的对象",
                                     code="bad_versions")
                self._write_json(200, store.confirm_plan(
                    case_id, reviewer.strip(), expected_versions=expected))
            elif path.startswith("/persons/") and path.endswith("/relations"):
                person_id = path[len("/persons/"):-len("/relations")].strip("/")
                link = body.get("link")
                if not isinstance(link, dict):
                    raise StoreError("link 必须是关系记录对象", code="bad_link")
                self._write_json(200, store.register_relation(person_id, link))
            elif path.startswith("/cases/") and path.endswith("/opinions"):
                case_id = path[len("/cases/"):-len("/opinions")].strip("/")
                try:
                    result = store.record_opinion(
                        case_id,
                        role=body["role"],
                        author_person_id=body["author_person_id"],
                        scope=body["scope"],
                        verdict=body["verdict"],
                    )
                except KeyError as exc:
                    raise StoreError(f"缺少字段: {exc.args[0]}", code="missing_field")
                self._write_json(201, result)
            else:
                self._write_json(404, {"error": "接口不存在"})
        except StoreError as exc:
            self._write_json(exc.status,
                            {"error": exc.message, "code": exc.code, **exc.details})
        except (TypeError, ValueError) as exc:
            # 契约层校验抛出的参数错误
            self._write_json(400, {"error": str(exc), "code": "contract_violation"})


def _json_default(value):
    if isinstance(value, (tuple, set)):
        return sorted(value) if all(isinstance(v, str) for v in value) else list(value)
    raise TypeError(f"不可序列化的类型: {type(value)!r}")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
