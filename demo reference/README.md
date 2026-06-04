# Aniline Should-Cost — Monte Carlo Demo

A lightweight reference demo that adapts **BioSTEAM**'s uncertainty-analysis
pattern to our aniline should-cost engine.

> ⚠️ **This is a reference demo, not a production model.**
> The market prices and parameter ranges are **placeholder numbers** chosen to
> show the mechanism end to end. The output (~1,100 USD/t) is **not** a
> defensible aniline price. Real values get plugged in once our Cat 2
> live-data pipeline (EIA / USITC) is wired up.

## What it shows

The goal is to prove the mechanism runs end to end — *not* to produce an
accurate price. It demonstrates three things:

1. **Cost build-up** — compute a fair price by stacking cost layers
   (feedstock → utility → markup → margin), the core of our Cat 3 engine.
2. **Monte Carlo band** — sample the uncertain inputs (yields, energy,
   markup) and produce a P10 / P50 / P90 price band instead of a single
   number.
3. **Sensitivity (tornado)** — rank which input drives price uncertainty
   the most, using Spearman rank-order correlation.

## What we borrowed from BioSTEAM

BioSTEAM (`BioSTEAMDevelopmentGroup/biosteam`) is a full process-simulation
TEA framework — too heavy to adopt wholesale for a 10-week prototype. We
borrowed only its uncertainty pattern:

| Pattern | What it does |
| --- | --- |
| Parameter = distribution + setter | Each uncertain input is declared with a distribution (triangular / uniform), not a single value |
| Latin Hypercube Sampling (LHS) | Covers the input space more evenly than plain random sampling, so the band stabilizes with fewer samples |
| Spearman rank-order sensitivity | Ranks inputs by how much they drive output-price uncertainty (tornado chart) |

**Key difference in philosophy:** BioSTEAM *simulates* the process to derive
yields and energy use, then takes prices as fixed manual inputs. We do the
opposite — yields and energy are fixed config constants from literature
(Ullmann's, Towler), and market prices are what vary (pulled from external
data). The live-data pipeline and time-series tracking are our own area and
have no equivalent in BioSTEAM.

## Run it

```bash
pip install numpy scipy
python aniline_montecarlo_demo.py
```

## Example output

```
[Baseline] fair price = 1,134.6 USD/t
  Cost breakdown by layer:
    feedstock             832.3 USD/t  ( 73.4%)
    utility                42.0 USD/t  (  3.7%)
    variable_markup        91.6 USD/t  (  8.1%)
    fixed_markup           65.6 USD/t  (  5.8%)
    margin                103.1 USD/t  (  9.1%)

[Monte Carlo, 1000 samples (LHS)]
  P10 = 1,094.1 USD/t
  P50 = 1,141.8 USD/t  (median)
  P90 = 1,187.2 USD/t

Tornado (input -> price sensitivity, Spearman rho)
  target_margin          + |##############################| +0.865
  variable_markup        + |############                  | +0.347
  fixed_markup           + |#########                     | +0.278
  ...
```

## How to read it

- **Baseline** — one calculation with every input at its most-likely value,
  plus an itemized breakdown of what makes up the price.
- **P10 / P50 / P90** — the price band after re-computing 1,000 times with
  inputs sampled from their uncertainty ranges. We report a range, honestly,
  rather than a single point.
- **Tornado** — feedstock is ~73% of the price (cost-pass-through), while the
  *uncertainty* is dominated by markup / margin, not the physical parameters.
  This mirrors our core thesis: the physical floor is firm, and the
  negotiable premium lives in the markup layer.

## Where the real values go

- `MARKET` dict → replace with monthly prices from EIA / USITC (currently a
  placeholder snapshot).
- `PARAMETERS` ranges → tighten once verified against primary sources.
- `fair_price_per_ton()` → refine the nitric-acid and hydrogen cost terms
  (currently simplified around benzene).
