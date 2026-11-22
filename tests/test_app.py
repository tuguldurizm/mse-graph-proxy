import httpx
import pytest
from fastapi.testclient import TestClient

import app as proxy_app


@pytest.fixture
def client(monkeypatch):
    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect-out":
            return httpx.Response(302, headers={"location": "https://evil.example.com/x"})
        if request.url.path == "/redirect-in":
            return httpx.Response(302, headers={"location": "/mn/companies"})
        return httpx.Response(200, text=f"<html>{request.url}</html>",
                              headers={"content-type": "text/html; charset=utf-8"})

    monkeypatch.setattr(proxy_app, "PROXY_TOKEN", "secret")
    monkeypatch.setattr(proxy_app, "client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    return TestClient(proxy_app.app)


def test_requires_token(client):
    assert client.get("/fetch", params={"url": "https://mse.mn/"}).status_code == 401
    assert client.get("/fetch", params={"url": "https://mse.mn/"},
                      headers={"X-Proxy-Token": "wrong"}).status_code == 401


def test_fetches_allowed_hosts(client):
    h = {"X-Proxy-Token": "secret"}
    r = client.get("/fetch", params={"url": "https://mse.mn/mn/companies"}, headers=h)
    assert r.status_code == 200 and "mse.mn/mn/companies" in r.text
    assert client.get("/fetch", params={"url": "https://data.opendatalab.mn/x.csv"}, headers=h).status_code == 200


def test_blocks_other_hosts_and_schemes(client):
    h = {"X-Proxy-Token": "secret"}
    for url in ("https://example.com/", "https://mse.mn.evil.com/", "file:///etc/passwd",
                "http://169.254.169.254/latest/meta-data/"):
        assert client.get("/fetch", params={"url": url}, headers=h).status_code == 403, url


def test_redirects_are_rechecked(client):
    h = {"X-Proxy-Token": "secret"}
    assert client.get("/fetch", params={"url": "https://mse.mn/redirect-out"}, headers=h).status_code == 403
    r = client.get("/fetch", params={"url": "https://mse.mn/redirect-in"}, headers=h)
    assert r.status_code == 200 and r.headers["x-final-url"] == "https://mse.mn/mn/companies"


@pytest.fixture
def routed(monkeypatch):
    """A proxy with an upstream forward proxy configured for opendatalab.mn."""
    seen = []

    def via(name):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((name, request.url.host))
            if request.url.path == "/to-odl":
                return httpx.Response(302, headers={"location": "https://www.opendatalab.mn/search/1"})
            return httpx.Response(200, text="ok")
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(proxy_app, "PROXY_TOKEN", "secret")
    monkeypatch.setattr(proxy_app, "UPSTREAM_PROXY_URL", "http://user-rotate:hunter2@p.webshare.io:80")
    monkeypatch.setattr(proxy_app, "UPSTREAM_PROXY_HOSTS", ["opendatalab.mn"])
    monkeypatch.setattr(proxy_app, "client", via("direct"))
    monkeypatch.setattr(proxy_app, "upstream_client", via("upstream"))
    return TestClient(proxy_app.app), seen


def test_upstream_proxy_carries_its_hosts_only(routed):
    c, seen = routed
    h = {"X-Proxy-Token": "secret"}
    assert c.get("/fetch", params={"url": "https://www.opendatalab.mn/search/2737779"}, headers=h).status_code == 200
    assert c.get("/fetch", params={"url": "https://mse.mn/mn/companies"}, headers=h).status_code == 200
    assert seen == [("upstream", "www.opendatalab.mn"), ("direct", "mse.mn")]


def test_upstream_proxy_rechosen_on_redirect(routed):
    c, seen = routed
    r = c.get("/fetch", params={"url": "https://mse.mn/to-odl"}, headers={"X-Proxy-Token": "secret"})
    assert r.status_code == 200
    assert seen == [("direct", "mse.mn"), ("upstream", "www.opendatalab.mn")]


def test_upstream_proxy_never_leaks_credentials(routed, monkeypatch):
    c, _ = routed
    body = c.get("/healthz").json()
    assert body["upstream_proxy"] == {"hosts": ["opendatalab.mn"], "route": "http://p.webshare.io:80"}

    def boom(request):
        raise httpx.ProxyError("cannot reach http://user-rotate:hunter2@p.webshare.io:80 as user-rotate")
    monkeypatch.setattr(proxy_app, "upstream_client", httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    r = c.get("/fetch", params={"url": "https://www.opendatalab.mn/search/1"}, headers={"X-Proxy-Token": "secret"})
    assert r.status_code == 502
    assert "hunter2" not in r.text and "user-rotate" not in r.text and "p.webshare.io:80" in r.text


def test_no_upstream_proxy_by_default(client):
    assert client.get("/healthz").json()["upstream_proxy"] is None
