# Ochir Undraa Group (Очир Ундраа Групп)

A hand-curated dataset of the Ochir Undraa Group: Очир Ундраа Холдинг ХХК, its companies in mining
services (explosives and blasting), finance and insurance, the people who run them, and the
**second-step companies**: other companies that the group's people own or run.
The group was founded in 2000 (Mongolian Mining Journal) and has no company listed on the MSE.

Researched 2026-11-22 (`manifest.json` → `observed_at`). Source for every registry fact: **Open Data
Lab Mongolia** (opendatalab.mn). Every row's `source` column holds its URL.

## How it was compiled, and how far to trust it

This session ran in the Default environment, which can't reach opendatalab.mn, and moving it to the
"OpenDataLab access" environment was declined. oug.mn didn't resolve either. **Every fact here comes
from search-engine results** that quote those pages (the method used for `data/mcs/` numbers), not
from reading them. That is weaker than the Shunkhlai dataset.

The pipeline checks this. Every company with a number is read in each registry import, whatever the
day's shard, and `cli validate-manual` compares these rows with what the registry says. The first daily
run after the merge (04:12 Ulaanbaatar) reads all seven pages and the two seeds. Correct any row it
contradicts.

## Companies

| Company | Reg. no. | What | Evidence for the group |
|---|---|---|---|
| Очир Ундраа Холдинг ХХК (Ochir Undraa Holding) | not found | group holding | oug.mn governance page; worki.mn |
| Очир-Ундраа ХХК (Ochir Undraa LLC) | 2659603 | founded 2007-10-29. Imports chemicals and explosives, holds a mining licence (Darkhan-Uul) and bids for ammonium nitrate tenders | registry: shareholder ОЧУН Капитал Хувийн ХОС; spa.gov.mn |
| Очир Ундраа ОМЗ ББСБ (OCHUN Finance) | 2734745 | NBFI, founded 2007-11-13 | registry: shareholders Очир Ундраа Холдинг (2024-04-15) and Тардай Гантөмөр |
| Мера ХХК (MERA) | 2619687 | founded 2007-06-06. Imports industrial explosives and does blasting | bcmongolia.org lists MERA as a group company |
| Мера Сервис ХХК (MERA Service) | 5607752 | founded 2012, Ömnögovi. Blasting and contract drilling | oug.mn company list. Its page also came up for Тардай Гантөмөр |
| ОЧУН ҮЦК (OCHUN Securities) | 6273475 | broker, registered 2018-02-01 | oug.mn (ОЧУН Групп); bcmongolia.org |
| ОЧУН Капитал Хувийн ХОС (OCHUN Capital private fund) | 6504914 | fund management, founded 2019-12-09 | registry: beneficial owner Тардай Гантөмөр |
| ОЧУН Групп, Практикал Даатгал (Practical Insurance), Азот (Azot), Бласттек (Blasttech, blast.mn), МЕРА Инженеринг | not found | | oug.mn company list; bcmongolia.org |

The "Evidence" column is why each company counts as the group's. Edges with no registry evidence are
drawn as `OWNS` from the holding (or from ОЧУН Групп for the insurer and the broker) with **no
percentage**, as in the Shunkhlai dataset. Look-through ownership ignores them.

## People

| Person | Role | Source |
|---|---|---|
| **Тардай Гантөмөр** (Т.Гантөмөр) | shareholder of the holding and general director of the group. Shareholder of OMZ NBFI. Beneficial owner and manager of ОЧУН Капитал Хувийн ХОС | oug.mn governance; Mongolian Mining Journal; registry |
| Батболд Билигүүн (Б.Билигүүн) | executive director of the holding; chairman of the board of OMZ NBFI | oug.mn governance; registry |
| Ганбаатар Цогтгэрэл | executive director of OMZ NBFI since 2018-03-28 | registry; ikon.mn |
| Гантөмөр Мөнхзул | executive director of Очир-Ундраа ХХК since 2018-05-25. Her patronymic suggests a relative of Т.Гантөмөр (not confirmed) | registry |
| Дамдинбал Энхтуул | executive director of ОЧУН ҮЦК | registry |

## Second-step companies (the group's people → their other companies)

Open Data Lab's robots.txt disallows name search, and the registry pages don't link a person to their
other companies. So these came from search results for each person's name on opendatalab.mn. The
companies with a number are in `data/discovery/seeds.csv`, so discovery reads their pages as new
`~<reg no>` companies. Any link to the group's people then comes from the registry itself, not from
this file.

| Company | Reg. no. | Link | Confidence |
|---|---|---|---|
| Бүтээгч ХХК | 5772842 | founded 2013-11-22. Search results name Тардай Гантөмөр as its founder and an individual shareholder | medium: seeded |
| Өү Юу Жи ХХК ("OUG", the group's initials) | 5612837 | founded 2012-06-25, foreign trade. Came up for Тардай Гантөмөр, and its director is also its beneficial owner | medium: seeded |
| Очун Капитал ХХК (an older, removed record) | not found | Тардай Гантөмөр director from 2012-06-25 | low |
| Арц-Алтан тэвш | not found | Тардай Гантөмөр director (registered 2006-02-20, gambling-activity code 9200) | low: the same name may be a different person |
| Нэмэх төгрөг ББСБ | not found | Ганбаатар Цогтгэрэл executive director | low |

Discovery also follows the registry's beneficial-owner chains of every company above, up to three
steps out, so more companies of the group will appear as the pages are read.

## Not the same group

**Оюуны Ундраа групп** (Oyuny Undraa Group, О.Тэнүүн, С.Отгонбаяр) is a different group. **МЕСА
инженеринг** (5727847) is not MERA Engineering. **Очир төв тоног төхөөрөмж** (5667011) has no known link.

## Caveats

* The registry gives no percentages, so none are given here.
* Manual edges are never closed by a scrape. If a fact goes out of date or the registry contradicts it,
  change or remove the row. Changing any file makes the pipeline load the dataset again, and retracted
  rows are deleted.
* Verify against the primary registry (УБЕГ) before you publish. Open Data Lab asks the same.
