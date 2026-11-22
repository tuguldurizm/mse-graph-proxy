# CLAUDE.md: tuguldurizm/mse-graph-proxy (the deploy mirror)

**This repository is a deploy mirror. Don't develop here.** The source of truth for all code, tests,
data and docs is **tuguldurizm/sentry**. Read its `CLAUDE.md`, `docs/SYSTEM.md` (how everything is
wired) and `docs/CHANGELOG.md` (every change, and what is open). If sentry isn't in your session,
add it or ask the owner to start a session with both repositories.

## Why it exists

Render builds **both** live services from this repository's `main`:

| Render service | Builds | From sentry |
|---|---|---|
| `mse-graph` (`srv-dc0isa97lnhs738q9tvg`), https://mse-graph.onrender.com | Docker `./app/deploy/Dockerfile`, context `./app` | `mse_graph/`, `deploy/`, `data/`, `requirements.txt` → `app/` |
| `mse-graph-proxy` (`srv-dc0iiemc6qcc73efqmu0`), https://mse-graph-proxy.onrender.com | `pip install -r requirements.txt`, `uvicorn app:app` | `proxy/app.py` → `app.py`, `proxy/requirements.txt` → `requirements.txt` |

Only `README.md`, `render.yaml`, `tests/test_app.py`, `pytest.ini` and this file are its own.

## Deploying

1. The change is merged into sentry's default branch (`ccr-b3860e32-y7rkdd`).
2. Pull `main` here first. The owner sometimes edits files on GitHub, as with `6da1d03` "Update app.py".
3. From the sentry checkout: `python3 scripts/deploy_repo.py check`, then `… sync` (the target defaults
   to `../mse-graph-proxy`). Review `git diff` and run `pytest -q` here.
4. Commit on your session branch as `Deploy: <what> (sentry #<PR>)`, with
   `App code from tuguldurizm/sentry <sha>` in the body. Push it, open a PR into `main` and **merge it
   yourself**. That has been the owner's standing instruction since 2026-11-22 (sentry's `CLAUDE.md`,
   "Pull requests"). Don't merge if the tests fail or it conflicts.
5. **Auto-deploy never fires** (sentry issue #3: Render gets no GitHub webhooks for this repository).
   About a minute after the merge, check `list_deploys`. If a `new_commit` deploy of your commit already
   started, auto-deploy works: don't start a second one. Otherwise start the deploy with the Render MCP
   tool `trigger_deploy` (workspace `tea-dbgti56gekts738renl0`). Then check `list_deploys` / `list_logs`.
6. Record the deploy in sentry's `docs/CHANGELOG.md` and its "Live now" table in `docs/SYSTEM.md`.

Never put secrets in this repository: it is **public**. `PROXY_TOKEN`, `UPSTREAM_PROXY_URL` (Webshare),
`NEO4J_*` and `ODL_PROXY_URL` live only in the Render dashboard.
