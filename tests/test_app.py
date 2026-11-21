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

