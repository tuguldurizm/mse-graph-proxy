# mse-graph-proxy

> **Deploy mirror.** Render builds both live services from `main` of this repository, but the code is
> developed in `tuguldurizm/sentry`: `app.py` is its `proxy/app.py`, and `app/` holds its `mse_graph/`,
> `deploy/` and `data/`. Change the code there, then sync it here (see `CLAUDE.md`). sentry's
> `docs/SYSTEM.md` describes the whole setup, and `docs/CHANGELOG.md` lists every change.

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

---

# app/: the hosted MSE connections graph

`app/` has the full app (Neo4j, the scraper pipeline and the web UI) packaged as **one container**
for Render. On startup it scrapes mse.mn, imports Open Data Lab Mongolia, loads Neo4j and serves
the UI. Progress is at `/api/status` and in the service logs.

- Render: Docker runtime, Dockerfile `./app/deploy/Dockerfile`, context `./app`
- `APP_PASSWORD`: when set, the site asks for a login (any username, this password)
- `SCRAPE_LIMIT`: scrape only the first N companies (0 = all)
- `PIPELINE_DEMO=1`: load the fictional demo data instead

Memory is tuned to fit a 512 MB instance. The free plan has no disk, so the graph and all its
history live in Neo4j Aura (`NEO4J_URI` on Render); the bundled Neo4j starts only when that URI is
local.
