# MCS Group companies (from open data)

The companies of **MCS Group** (М-Си-Эс Групп), Mongolia, as recorded in the state legal-entity registry
republished by [Open Data Lab Mongolia](https://www.opendatalab.mn). The data was collected on 2026-11-21.
Source: Open Data Lab Mongolia (opendatalab.mn). Their terms ask that facts be checked against the
primary registry (УБЕГ) before you publish them.

| File | What it holds |
|---|---|
| `mcs_companies.csv` | Every registry page that was checked, one row each. `mcs_status` is `current` (40 companies) or `not linked` (checked and excluded). For each row: the `evidence` for the link, the current shareholders, beneficial owners and officers, founding date, address, main activities and the registry URL |
| `labels.csv` | English names, business segment and a short ownership note for each MCS company. Written by hand; the build merges it in |
| `graph/` | The 40 MCS companies in this repo's CSV hand-off format, as unlisted companies (ticker `~<reg no>`): registry shareholders, officers and beneficial owners. To load into Neo4j: `python -m mse_graph.cli load data/mcs/graph` |

## How a company was counted as MCS

The two group holding companies are **М Си Эс холдинг** (2628236) and **М Си Эс монголиа** (6210619).
Both are owned by the six founding shareholders: Ж.Оджаргал, Ж.Од, Д.Энхцэцэг, Д.Бадамцэцэг,
Б.Отгончимэг and О.Чимгээ. Any other company counts as MCS when its *current* registry record shows at
least one of these:

1. a shareholder that is an MCS company already found, or one of the group's holding vehicles:
   NORDSTAR HOLDING PTE. LTD., NOMADCOM PTE. LTD., М Си Эс Пропертийз Холдинг, Энержиресурс Корпораци;
2. a declared beneficial owner (эцсийн өмчлөгч) who is **Жамбалжамц Оджаргал** or **Жамбалжамц Од**;
3. an entry, with its registration number, in the beneficial-owner chain of an MCS company.

The rules are applied repeatedly until no more companies qualify. Rows marked *(хасагдсан)* (removed) are ignored.
The registry gives no ownership percentages, so `relationship` in `labels.csv` describes who holds
the company, not how much. A company is included even if MCS holds a minority stake.
Examples: Intelmind (an MCS Ventures investee) and the joint ventures with Shangri-La, Kerry,
Petrovis and Nomin Holding.

## The 40 companies, by segment

| Segment | Companies (reg. no.) |
|---|---|
| Holding | MCS Holding (2628236), MCS Mongolia (6210619) |
| Mining | Energy Resources (2887746), Enrestechnology (3614107), Ukhaa Khudag Water Supply (3614085), Tavan Tolgoi Airport (3614476), Khangad Exploration (2887134), Erdene Mongol (2718243), Nordstar Resource (5502993) |
| Energy & engineering | MCS International (2090007) |
| ICT | Unitel (5036895), Nomadcom Mongolia (6591019), Univision (5388457), MCS Com (2647214), IVLBS (6456308), Green Future (6453449), United Alliance (6307256) |
| Financial services | M Bank (6207618), Toki NBFI (6365744) |
| FMCG | MCS Coca-Cola (2663503), Total Distribution (5167647), Market Gate (6132618) |
| Property | MCS Property (5040159), MCS Tower (2864215), MCS Estates Brokers (6701752), International City Center (6088694) |
| Hospitality | Shangri-La Ulaanbaatar Hotel (5149037), Shangri-La Ulaanbaatar (2876388), Sky Resort (5228697) |
| Healthcare | International Medical Center / Intermed (5387051) |
| Services | Uniservice Solution (2878593), M-Armor (5509882) |
| Automotive | Top Motors (6472486) |
| Ventures & technology | MCS Ventures (6594409), E-Bazaar Trade (6388094), M Plus (6403492), Intelmind (6180574), Tekpak Group (6782965), UBCab (5751888), UB Transport Solutions (6263402) |

## Known gaps

The search covers only companies whose registration numbers could be found. Open Data Lab's
`robots.txt` disallows name search (`/search?…`), and its pages do not link a shareholder to that
shareholder's own page. So candidates came from search-engine results for opendatalab.mn pages,
from tender-bidder pages, and from the beneficial-owner chains of companies already found. Companies
the group names, or that appear in the records, and are **not** in the list yet:

* **No registration number found:** Building Maintenance Services, DDish TV, Orbitnet, Monsat
  (Unitel Group satellite/TV), Metagro (agriculture), United Power, Energy Resources Rail, Tavan
  Tolgoi Power Plant Water Supply (Mongolian Mining Corporation's chart), and the holding companies named as
  shareholders: М Си Эс Пропертийз Холдинг, М Си Эс Инвестмент, М Си Эс Техноложис, Юбикаб Холдинг,
  Дижитал Капитал, Энержиресурс Корпораци.
* **Number found but no registry page (HTTP 404), probably dissolved or merged:** Энерко (2110768),
  Энержиресурс роуд (5372992), Трансгоби (5254159), Энержиресурс майнинг (5277647), Говийн зам (5366062),
  Интермед сентер (5762596).
* **Registered abroad, not in the Mongolian registry:** Mongolian Mining Corporation (Cayman, HKEX 975),
  Mongolian Mining Corp. Pte. Ltd., NORDSTAR HOLDING PTE. LTD. and NOMADCOM PTE. LTD. (Singapore),
  Baruun Naran S.a.r.l. (Luxembourg), Intens Power Limited, Shangri-La Mongolia (UK).

To add a company once its registration number is known:

```bash
python scripts/mcs/odl_fetch.py data/opendatalab/registry <reg no> [...]   # 11 s between requests (robots.txt asks 10)
python scripts/mcs/build_mcs.py data/opendatalab/registry data/mcs         # reclassify; prints companies lacking a label
```

`data/opendatalab/` is git-ignored, so the raw registry pages are not committed.
