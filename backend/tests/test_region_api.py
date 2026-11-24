"""地区发布接口测试：在进程内直接驱动 FastAPI 应用，不依赖 httpx。

覆盖路由优先级（不能被通用 /api/{module}/{entry_id} 抢走）、参数解析、
幂等发布与 409 冲突、以及接口默认不泄露未授权指标。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.main import app
from app.region_publication import registry


class Response:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def call(method: str, path: str, payload: dict | None = None) -> Response:
    """最小 ASGI 驱动器：构造 scope/receive/send 并跑完一次请求。"""
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode("utf-8"),
        "query_string": query.encode("utf-8"),
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    status_holder: dict[str, int] = {}
    chunks: list[bytes] = []

    async def receive() -> dict:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            status_holder["status"] = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    async def run() -> None:
        await app(scope, receive, send)

    asyncio.run(run())
    return Response(status_holder["status"], b"".join(chunks))


@pytest.fixture(autouse=True)
def clean_registry():
    registry.reset()
    yield
    registry.reset()


class TestRouting:
    def test_policy_route_not_swallowed_by_generic_module_route(self):
        response = call("GET", "/api/region-publication/policy")
        assert response.status_code == 200
        assert response.json()["policy"]["province_min_orders"] == 5

    def test_generic_module_route_still_works(self):
        response = call("GET", "/api/drill_path?size=1")
        assert response.status_code == 200
        assert "items" in response.json()

    def test_unknown_record_returns_404(self):
        response = call("GET", "/api/region-publication/records/missing")
        assert response.status_code == 404


class TestScenarioEndpoint:
    def test_scenario_report_default(self):
        payload = call("GET", "/api/region-publication/scenario").json()
        report = payload["report"]
        assert [row["label"] for row in report["rows"]] == ["丙"]
        assert report["totals"] == {"order_count": 8, "amount": 1000.0}
        assert report["pending_confirmations"] == ["北一区"]

    def test_approval_query_param(self):
        response = call(
            "GET",
            "/api/region-publication/scenario?approved_region=%E5%8C%97%E4%B8%80%E5%8C%BA",
        )
        payload = response.json()
        assert payload["approved_regions"] == ["北一区"]
        assert payload["report"]["totals"] == {"order_count": 14, "amount": 1500.0}
        labels = [row["label"] for row in payload["report"]["rows"]]
        assert labels == ["北一区", "丙"]

    def test_resend_does_not_change_report(self):
        payload = call("GET", "/api/region-publication/scenario?with_resend=true").json()
        assert payload["report"]["totals"] == {"order_count": 8, "amount": 1000.0}
        internal = call(
            "GET",
            "/api/region-publication/scenario?with_resend=true&include_metrics=true",
        ).json()
        assert internal["decision"]["audit"]["valid_orders"] == 14
        assert internal["input"]["row_count"] == 17

    def test_default_decision_hides_unauthorized_metrics(self):
        payload = call("GET", "/api/region-publication/scenario").json()
        assert "input" not in payload
        assert "audit" not in payload["decision"]
        for row in payload["decision"]["provinces"]:
            if row["province"] in {"甲", "乙"}:
                assert "order_count" not in row
                assert "amount" not in row
        for row in payload["decision"]["regions"]:
            if row["region"] == "北一区":
                assert "order_count" not in row
                assert "amount" not in row

    def test_default_response_cannot_reconstruct_hidden_region(self):
        """默认响应里不能出现 14/17/6 等可反推北一区的计数。"""
        payload = call("GET", "/api/region-publication/scenario").json()
        text = json.dumps(payload, ensure_ascii=False)
        # 14 = 全部有效订单，6 = 北一区合计，17 = 带重送后的原始行数。
        for forbidden in ('"valid_orders"', '"unique_orders"', '"row_count"', '"audit"'):
            assert forbidden not in text
        numbers = set()

        def collect(value):
            if isinstance(value, bool):
                return
            if isinstance(value, (int, float)):
                numbers.add(float(value))
            elif isinstance(value, dict):
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(payload)
        # 门槛 5 本身是公开策略；8 与 1000 是丙的已披露指标。
        assert 8.0 in numbers and 1000.0 in numbers
        assert numbers.isdisjoint({14.0, 17.0, 6.0, 4.0, 2.0, 500.0, 400.0, 100.0})

    def test_include_metrics_for_internal_review(self):
        payload = call(
            "GET", "/api/region-publication/scenario?include_metrics=true"
        ).json()
        counts = {row["province"]: row["order_count"] for row in payload["decision"]["provinces"]}
        assert counts == {"甲": 4, "乙": 2, "丙": 8}


class TestPublishEndpoint:
    def test_publish_twice_lands_once(self):
        first = call(
            "POST", "/api/region-publication/scenario/publish", {"scope_id": "s1"}
        )
        second = call(
            "POST", "/api/region-publication/scenario/publish", {"scope_id": "s1"}
        )
        assert first.json()["created"] is True
        assert second.json()["created"] is False
        records = call("GET", "/api/region-publication/records").json()
        assert records["total"] == 1

    def test_resend_same_scope_is_idempotent(self):
        call("POST", "/api/region-publication/scenario/publish", {"scope_id": "s2"})
        response = call(
            "POST",
            "/api/region-publication/scenario/publish",
            {"scope_id": "s2", "with_resend": True},
        )
        assert response.json()["created"] is False
        assert response.json()["record"]["report"]["totals"] == {
            "order_count": 8,
            "amount": 1000.0,
        }

    def test_conflicting_version_returns_409(self):
        call("POST", "/api/region-publication/scenario/publish", {"scope_id": "s3"})
        response = call(
            "POST",
            "/api/region-publication/scenario/publish",
            {"scope_id": "s3", "approved_regions": ["北一区"]},
        )
        assert response.status_code == 409

    def test_publish_requires_orders(self):
        response = call("POST", "/api/region-publication/publish", {"scope_id": "s4"})
        assert response.status_code == 400

    def test_evaluate_rejects_empty_orders(self):
        response = call("POST", "/api/region-publication/evaluate", {"scope_id": "s5"})
        assert response.status_code == 400
