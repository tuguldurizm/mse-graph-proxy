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
| Скайтел ХХК (Skytel) | Шунхлай Холдинг 50% (2010-12, when Altai Holding held the other half). The registry now lists Shunkhlai Holding as its only shareholder | Wikipedia, Skytel (Mongolia); registry |
| Медиа Групп ХХК (Media Group) | Шунхлай Холдинг 100% (filing 2025-12) | Journalism Trust Initiative report |
| NTV Broadcasting Co Ltd | Медиа Групп | Media Ownership Monitor (Global Media Registry) |
| Дижитал Концепт ХХК (GS25 master franchise) | Шунхлай Холдинг, АПУ ХК and Жи Эс Рэтайл Ко.Лтд (GS Retail); shares not given | registry; Asia Economy 2024, Inside Retail 2021 |
| СББ Трейд ХХК, АПУ Дэйри ХХК | АПУ ХК | registry |
| Шунхлай ХХК (petroleum, founded 1993), Шунхлай Петролиум, Шунхлай Трейдинг, Жи Эс Би Капитал ББСБ, Эс Системс | Шунхлай Холдинг (registry confirms) | sg.mn; isee.mn "TOP-100" article; registry |
| Шунхлайговь, АПУ Трейдинг, Спиртбалбурам, Mongolia Motors (Hyundai/Kia), Сантранс Ложистикс, SG Investment and Securities Brokerage, Zero Technology, DSG | group companies (no registration number yet, so not checked) | sg.mn; isee.mn |

APU's register number is set in `company_facts.csv` (`register_no` 2702673). mse.mn gives the
10-digit state registration number (9010001005), which Open Data Lab doesn't know.

## Checked against Open Data Lab (2026-11-21)

The pipeline compared the dataset with the registry pages of the 10 companies that have a number:

- **Confirmed:** Shunkhlai Holding owns Shunkhlai LLC, Shunkhlai Petroleum, Shunkhlai Trading,
  GSB Capital, S Systems, Skytel and Digital Concept, and П.Батсайхан is an officer of the holding.
- **Corrected here:**
  - SBB Trade and APU Dairy are owned by **APU JSC**, not by the holding directly.
  - Digital Concept's co-owners are APU JSC and GS Retail, which the registry spells Жи Эс Рэтайл Ко.Лтд.
  - Altai Holding is no longer a Skytel shareholder.
  - The rows that were taken back are deleted from the graph on the next load. Retracted research
    is not kept as history.
- **What the registry adds:** the holding's own shareholders are individuals: Пүрэв Батсайхан,
  Б Ганбат, Батмөнх Цэлмэг, Батхүү Ганхөлөг, Батхүү Цэлмүүнүжин and Халзан Тунгалаг. It also adds
  each subsidiary's officers, for example Жагдагдагва Мөнхнаран as executive director of the petroleum companies.
  These are registry records in the graph, so they're not repeated in these CSVs.

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
  The registry lists two of Г.Батхүү's children among the holding's shareholders.
* **The names are as found.** Where no Mongolian name was found, the English one is used (NTV Broadcasting,
  Mongolia Motors, GSB Capital, ...). The Heineken entity is written `HEINEKEN`. If mse.mn writes
  it differently, APU will show two Heineken holders until this file is changed to match.
* **Manual edges are never closed by a scrape.** Only mse.mn's own edges are. If a fact here goes
  out of date, change or remove the row, or add an `until` date. Changing any file makes the
  pipeline load the dataset again.
* sg.mn, apu.mn, mse.mn and the news sites could not be reached from where this was
  compiled. Facts were taken from search results that cite them. Verify them against the
  primary filings (mse.mn, the state registry) before you publish.
