# mse-graph-proxy

A small, allow-listed fetch proxy for the MSE connections graph scraper. It lets the scraper reach
`mse.mn` and `opendatalab.mn` from networks that can't reach them directly.

It is **not** an open proxy:

- every request needs the secret `PROXY_TOKEN` in the `X-Proxy-Token` header;
- only hosts in `ALLOWED_HOSTS` (default `mse.mn,opendatalab.mn`) and their subdomains can be
  fetched, and every redirect hop is re-checked;
- only GET over http/https is allowed, and responses are capped at 25 MB (`MAX_BYTES`).

## API

```
GET /healthz                       -> {"ok": true, "allowed_hosts": [...], "token_configured": true}
GET /fetch?url=<url-encoded URL>   header X-Proxy-Token: <PROXY_TOKEN>
```

The upstream status and body are returned as they are. `X-Final-Url` gives the URL after redirects.

## Deploy on Render

`render.yaml` defines the service: free plan, Singapore region, and a `PROXY_TOKEN` that Render
generates. Use **New + → Blueprint** with this repo, or create a Python web service:

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`

## Use from the scraper

```bash
export MSE_PROXY_URL=https://<service>.onrender.com
export MSE_PROXY_TOKEN=<PROXY_TOKEN from Render → Environment>
python -m mse_graph.cli -v scrape --limit 5
```

A free Render instance sleeps when idle, so the first request after a pause can take about a minute.

## Test

```bash
pip install -r requirements.txt pytest && pytest -q
```
