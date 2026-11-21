# Shunkhlai Group (Шунхлай Групп)

A hand-curated dataset of the Shunkhlai Group: Shunkhlai Holding LLC, its subsidiaries
and joint ventures, and the people who link them. The only listed company is **APU JSC
(APU)**. Its node comes from the mse.mn scrape, so this dataset refers to it by ticker
and doesn't overwrite it.

Researched 2026-11-21 (`manifest.json` → `observed_at`). The pipeline loads it automatically and
then compares it with the Open Data Lab registry (see "Hand-curated datasets" in the main README;
`cli validate-manual` shows the result). Every row's `source` column holds its URL.

## Registration numbers

The numbers come from search-engine results for opendatalab.mn registry pages, the same method
used for `data/mcs/`. The registry import then reads each page itself, through the pipeline's
normal 11-second crawl delay. Companies with a number use the ticker `~<reg no>`.

| Company | Reg. no. | Registry page |
|---|---|---|
| Шунхлай холдинг (Shunkhlai Holding; registered as Шунхлайгрупп until 2023-09-05) | 2737779 | [link](https://www.opendatalab.mn/search/2737779) |
| Шунхлай (Shunkhlai LLC, fuel import) | 2028468 | [link](https://www.opendatalab.mn/search/2028468) |
| Шунхлай трейдинг | 5506239 | [link](https://www.opendatalab.mn/search/5506239) |
| Шунхлай петролиум | 5512972 | [link](https://www.opendatalab.mn/search/5512972) |
| Скайтел (Skytel) | 2576376 | [link](https://www.opendatalab.mn/search/2576376) |
| Дижитал концепт (GS25) | 6531342 | [link](https://www.opendatalab.mn/search/6531342) |
| АПУ дэйри | 6229654 | [link](https://www.opendatalab.mn/search/6229654) |
| СББ трейд | 2695456 | [link](https://www.opendatalab.mn/search/2695456) |
| Жи Эс Би капитал ББСБ | 5416078 | [link](https://www.opendatalab.mn/search/5416078) |
| Эс системс (S Systems) | 6923046 | [link](https://www.opendatalab.mn/search/6923046) |
| АПУ ХК (listed, from mse.mn) | 2702673 | [link](https://www.opendatalab.mn/search/2702673) |

Not found yet: Шунхлайговь, АПУ трейдинг, Спиртбалбурам, Медиа групп, Сантранс ложистикс, Mongolia
Motors, SG Investment, Zero Technology, DSG and NTV. The registry also has a **Сантранс монгол** (rail
freight), which a loan record links to Шунхлай Групп. That may be the logistics arm, but this isn't confirmed.

## What it says

| Company | Owner(s) | Source |
|---|---|---|
| АПУ ХК (APU) | Шунхлай Холдинг ХХК 56.03%, HEINEKEN 25%, Steppe Beverage KFT 5.53% (as of 2025-04) | Wikipedia, APU Company |
| Скайтел ХХК (Skytel) | Шунхлай Холдинг 50%, Алтай Холдинг 50% (since 2010-12) | Wikipedia, Skytel (Mongolia) |
| Медиа Групп ХХК (Media Group) | Шунхлай Холдинг 100% (filing 2025-12) | Journalism Trust Initiative report |
| NTV Broadcasting Co Ltd | Медиа Групп | Media Ownership Monitor (Global Media Registry) |
| Дижитал Концепт ХХК (GS25 master franchise) | Шунхлай Холдинг + GS Retail Co., Ltd. (JV, split unknown) | Asia Economy 2024, Inside Retail 2021 |
| Шунхлай ХХК (petroleum, founded 1993), Шунхлай Петролиум, Шунхлай Трейдинг, Шунхлайговь, АПУ Трейдинг, Спиртбалбурам, СББ Трейд | group companies | sg.mn; isee.mn "TOP-100" article |
| АПУ Дэйри, Mongolia Motors (Hyundai/Kia), Сантранс Ложистикс, GSB Capital NBFI, SG Investment and Securities Brokerage, S Systems, Zero Technology, DSG | group companies | sg.mn management team page |

People: **П.Батсайхан** (Purev Batsaikhan), founder. He was General Director of Shunkhlai LLC
1993–1996, has been President of Shunkhlai Holding since 2008 (MarketScreener), and has chaired
APU's board since 2002 (APU 2024 half-year report).

## Caveats

* **"Group company" edges have no percentage.** The sources name these companies as part of
  the group, but not who holds them directly (the holding, APU or another subsidiary) or how
  much. They are drawn as `OWNS` from Шунхлай Холдинг ХХК with no percentage, so look-through
  ownership (which uses only edges with percentages) ignores them.
* **The sources disagree in places.** Capital Markets Mongolia gives APU as 56.63% Shunkhlai and 5.6% Steppe
  Beverage. The Media Ownership Monitor describes Media Group as owned 50/50 by Г.Батхүү and
  П.Батсайхан (from a 2014 asset declaration), and Г.Батхүү as chairman of the group's board.
  Neither claim is included here. The newer filing (100% Shunkhlai Holding) is used instead.
* **The names are as found.** Where no Mongolian name was found, the English one is used (NTV Broadcasting,
  Mongolia Motors, GSB Capital, ...). The Heineken entity is written `HEINEKEN`. If mse.mn writes
  it differently, APU will show two Heineken holders until this file is changed to match.
* **Manual edges are never closed by a scrape.** Only mse.mn's own edges are. If a fact here goes
  out of date, change or remove the row, or add an `until` date. Changing any file makes the
  pipeline load the dataset again.
* sg.mn, apu.mn, mse.mn and the news sites could not be reached from where this was
  compiled. Facts were taken from search results that cite them. Verify them against the
  primary filings (mse.mn, the state registry) before you publish.
