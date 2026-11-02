"""The app as Databricks Apps serves it: every middleware built the way the Starlette in the Apps image builds it
(cls(app=app, ...), 0.38) as well as the later way (cls(app, ...)), and the read endpoints called with the headers
the Apps proxy sends, and with them missing. 608bca4's upload limit took its inner app under another name, so the
Apps image's Starlette couldn't build the stack and every request was a plain 500."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

PROXY_HEADERS = {"Authorization": "Bearer dapi-token", "X-Forwarded-Email": "op@example.com",
                 "X-Forwarded-User": "1234567890@1234567890123456", "X-Forwarded-Preferred-Username": "op@example.com",
                 "X-Forwarded-Host": "app.example.databricksapps.com", "X-Forwarded-Proto": "https",
                 "X-Real-Ip": "10.0.0.1", "X-Request-Id": "req-1"}


def _inner(scope, receive, send):
    raise AssertionError("not called")


@pytest.mark.parametrize("build", [lambda cls, kwargs: cls(app=_inner, **kwargs), lambda cls, kwargs: cls(_inner, **kwargs)],
                         ids=["keyword, as Starlette 0.38 does", "positional, as later Starlette does"])
def test_every_middleware_builds_either_way_starlette_builds_it(build):
    for middleware in main.app.user_middleware:
        cls, kwargs = middleware.cls, dict(middleware.kwargs)
        assert build(cls, kwargs) is not None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [])
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.mark.parametrize("headers", [PROXY_HEADERS, {}], ids=["as the Apps proxy sends them", "with no proxy headers"])
def test_the_read_endpoints_answer_as_json(client, headers):
    for path in ("/api/config", "/api/overview", "/api/ingestion-jobs?limit=100"):
        r = client.get(path, headers=headers)
        assert r.headers.get("content-type", "").startswith("application/json"), (path, r.status_code, r.text[:200])
    assert client.get("/api/config", headers=headers).status_code == 200


def test_a_write_with_no_proxy_user_is_refused_and_one_with_it_gets_past_the_identity_check(client):
    assert client.post("/api/ingestion-jobs", json={"name": "x", "csv": ""}).status_code == 403
    r = client.post("/api/ingestion-jobs", json={"name": "x", "csv": ""}, headers=PROXY_HEADERS)
    assert r.status_code != 403 and r.headers.get("content-type", "").startswith("application/json")
