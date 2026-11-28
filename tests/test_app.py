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


def test_a_non_ascii_token_is_refused_not_an_error(client):
    """hmac.compare_digest raises TypeError on non-ASCII str: that was a 500 instead of a 401."""
    r = client.get("/fetch", params={"url": "https://mse.mn/"}, headers={"X-Proxy-Token": "нууц".encode()})
    assert r.status_code == 401


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
    h = {"X-Proxy-Token": "secret"}
    r = c.get("/fetch", params={"url": "https://mse.mn/to-odl"}, headers=h, follow_redirects=False)
    # a hop to opendatalab.mn goes back to the caller, which asks for it after the crawl delay ...
    assert r.status_code == 302 and r.headers["location"] == "https://www.opendatalab.mn/search/1"
    assert seen == [("direct", "mse.mn")]
    # ... and that request leaves through the upstream proxy
    assert c.get("/fetch", params={"url": r.headers["location"]}, headers=h).status_code == 200
    assert seen == [("direct", "mse.mn"), ("upstream", "www.opendatalab.mn")]


def test_opendatalab_redirects_go_back_to_the_caller(monkeypatch):
    """opendatalab.mn asks for 10 s between requests; the gateway used to follow its redirects at once.
    Sleeping inside the request would eat the caller's 90 s timeout, so the 3xx goes back instead."""
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/search/000207696":
            return httpx.Response(302, headers={"location": "/search/5562236"})
        if request.url.path == "/away":
            return httpx.Response(302, headers={"location": "https://evil.example.com/x"})
        return httpx.Response(200, text="page")

    monkeypatch.setattr(proxy_app, "PROXY_TOKEN", "secret")
    monkeypatch.setattr(proxy_app, "client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    c, h = TestClient(proxy_app.app), {"X-Proxy-Token": "secret"}
    r = c.get("/fetch", params={"url": "https://www.opendatalab.mn/search/000207696"}, headers=h,
              follow_redirects=False)
    assert r.status_code == 302 and r.headers["x-upstream-status"] == "302"
    assert r.headers["location"] == "https://www.opendatalab.mn/search/5562236"      # absolute
    assert r.headers["x-final-url"] == "https://www.opendatalab.mn/search/000207696"
    assert seen == ["https://www.opendatalab.mn/search/000207696"]                   # one request, no hop
    # the target is still checked: no 3xx that points the caller off the allow-list
    assert c.get("/fetch", params={"url": "https://opendatalab.mn/away"}, headers=h).status_code == 403
    assert c.get("/healthz").json()["pass_redirect_hosts"] == ["opendatalab.mn"]


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
