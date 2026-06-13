from __future__ import annotations

import json
from typing import Any, cast

from starlette.requests import Request

from app.shared.http.respond import accepted, created, no_content, ok


def _request() -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "state": {}})
    req.state.request_id = "req_test"
    return req


def _body(response: Any) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(response.body))


def test_ok_envelope_and_status() -> None:
    response = ok(_request(), {"hello": "world"})
    assert response.status_code == 200
    body = _body(response)
    assert body["data"] == {"hello": "world"}
    assert body["meta"]["requestId"] == "req_test"
    assert body["meta"]["timestamp"]


def test_ok_passes_extra_meta() -> None:
    response = ok(_request(), [], nextCursor="act_122")
    body = _body(response)
    assert body["meta"]["nextCursor"] == "act_122"


def test_created_status() -> None:
    response = created(_request(), {"id": "wrk_1"})
    assert response.status_code == 201
    assert _body(response)["data"] == {"id": "wrk_1"}


def test_accepted_status() -> None:
    response = accepted(_request(), {"buildId": "bld_1"})
    assert response.status_code == 202


def test_no_content_status_and_empty_body() -> None:
    response = no_content()
    assert response.status_code == 204
    assert response.body == b""
