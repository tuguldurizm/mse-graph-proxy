# Stakes confirmed from annual reports

Subsidiaries and associates that companies name in their own annual reports or financial statements
(issuers.mse.mn PDFs, read by `mse_graph/reports.py`), **after someone checked the finding against the
report**. The owner decided on 2026-11-27 that checked findings become `OWNS` stakes.

- Each row of `shareholders.csv` is one stake: the publishing (listed) company in `holder_ticker`, the
  subsidiary in `company_ticker`, the percent and year as the report gives them. Its `source` is
  `report:<the report's URL>`; the UI shows it as "Жилийн тайлан (шалгасан)".
- A subsidiary the graph doesn't know yet gets a row in `companies.csv` (ticker `~AR-…`, unlisted).
- Add rows with `python -m mse_graph.cli reports --confirm <report URL> --finding <n>` (in an
  "OpenDataLab access" session; `cli reports --kind subsidiary` lists the findings with their `n`), then
  merge a PR. Every start loads this folder like the other manual datasets. Deleting a row ends the stake.
- Shareholder findings are not confirmed this way: a listed company's owners are the ones mse.mn
  reports (owner, 2026-11-25).

The folder is loaded only once it has a CSV file; until the first confirmation it holds this README only.
Source: the companies' own reports, published on mse.mn.
