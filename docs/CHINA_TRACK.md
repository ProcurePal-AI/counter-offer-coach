# China Track — Decision Memo & Research Brief

*Status: proposal. Owner: Martin. Last updated: June 2026.*

## The decision: parallel track, not main mechanism (for now)

Recommendation: keep the **US aniline demo as the main line** for the current
ten-week sprint, and run China as a **structured parallel track** that becomes
promotable to main only when two gates clear. Reasons, briefly: the US line is
nearly complete end-to-end on free, fully-licensed data (USITC + EDGAR + EIA,
3.1–3.5 built, 124 tests); the China line is blocked on exactly two things that
code cannot solve — a SunSirs subscription **with derived-use rights in
writing**, and a sourced Chinese energy basis for the utilities layer. Building
China-first would trade a finishable demo for two procurement/research
dependencies outside the team's control. Building it as a parallel track costs
little (the connectors in this PR) and converts the multi-country vision from a
claim into a demonstrated fact the moment the gates clear.

**Gate 1 — data licence.** A SunSirs subscription is affordable (RMB pricing),
but affordability is not the unlock: the contract must explicitly grant the
right to store their data and use it as an input to a commercial model
("derived use"). Get that clause in writing in the order form before any
ingest. The connector (`pipeline/sunsirs.py`) is deliberately licence-gated: it
ingests licensed data exports only, has no scraping path, and its API stub
raises until real API docs arrive with the subscription.

**Gate 2 — Chinese energy basis** (see research brief below).

## What is already country-blind (reused as-is)

Storage layer + schema (region column carries country tags; no migration),
the connector pattern, the cost-engine math (stoichiometry, markup algebra,
Monte Carlo), and now the **region-family guard in engine/prices.py** — the
correctness change that makes multi-country data safe at all: a US floor month
can no longer silently resolve to a CN/DE price, and vice versa.

## What this PR adds (buildable today, all tested)

* `engine/prices.py` — region-family contamination guard (the critical fix).
* `pipeline/fx.py` — monthly USD/CNY from ECB reference rates (Frankfurter,
  free/keyless); RMB→USD happens at ingest, store stays USD-only.
* `pipeline/sunsirs.py` — licence-gated CN spot connector (region `CN_SPOT`,
  `assessment_type=spot`, grade passthrough).
* `config/margin_anchors.yaml` + `pipeline/margin_anchors.py` — 3.3's country
  parameterization: US auto-loads the EDGAR peer median; CN is a Wanhua
  (SSE 600309) template that **fails loudly until ratios are researched and
  cited** (annual report at cninfo.com.cn; multi-year median, same discipline
  as the EDGAR pipeline).

## Research brief: the Chinese utilities basis (Gate 2)

The utilities layer is the one place the skeleton cannot be reused by swapping
a feed, because no free EIA-equivalent API exists for Chinese industrial
energy. **However**, the research already done changes the shape of the task:
NDRC-monitored industrial electricity prices are *regulated and nearly flat*
(e.g. Beijing industrial 35kV+: 0.80 RMB/kWh, constant for months, 20-year
range 0.56–0.82). A *banded constant per region, sourced and cited in config*
is therefore a defensible utilities basis for a CN demo — a live monthly feed
adds little where the underlying price barely moves. Gas and coal (steam) move
more and deserve real series.

Sources, in order of practical value:

1. **Provincial State Grid catalog tariffs / NDRC Price Monitoring Center** —
   the official industrial electricity prices (the "36-city" monthly series).
   Free to read on provincial grid sites; manual extraction; the right citation
   basis for config constants. Search: "目录电价" (catalog tariff) + province,
   or NDRC 价格监测中心.
2. **Shanghai Petroleum & Natural Gas Exchange (SHPGX)** — Chinese natural gas
   (LNG/pipeline) traded prices; the gas-and-steam analog of Henry Hub.
3. **Coal indices for steam cost** — Bohai-Rim Steam-Coal Price Index (BSPI) /
   CCTD China Coal Market: if a CN plant's steam is coal-fired, steam derives
   from coal the way US steam derives from gas in `prices.py`; the boiler
   derivation pattern is reusable with a coal price and coal-boiler efficiency.
4. **NBS (stats.gov.cn / data.stats.gov.cn)** — monthly producer price indices
   for electricity/heat/gas production: free *shape* signals to sanity-check
   the constants.
5. **CEIC (paid)** — the clean machine-readable version of #1. Note: Berkeley
   library access is an *academic* licence and does not cover commercial use
   at 1TCC; a company licence would be needed. Do not blur this line.
6. **Explicitly unusable**: GlobalPetrolPrices (CC BY-NC-ND — non-commercial,
   no-derivatives), unlicensed scraping of any assessor.

Deliverable for Gate 2: a `config/utilities_cn.yaml` (or extension of the route
config) with cited RMB-basis constants + bands for industrial electricity,
gas, and steam-via-coal, converted through `fx.py` — plus, if SHPGX/coal series
are licensable or freely quotable, small connectors following the EIA pattern.

## Promotion criteria (parallel → main)

The China track replaces or joins the US demo as a headline result when:
licence signed with derived-use rights; CN utilities config sourced and cited;
Wanhua margin anchor filled and cited; `predict.py` runs end-to-end for
(aniline, CN) producing a floor that sits credibly below SunSirs CN spot; and
the premium gap behaves (floor below market, gap stable-ish vs. cost cycle).
At that point the deck gains the sentence the vision wants: *the same engine,
two countries, two data ecosystems, zero engine changes.*
