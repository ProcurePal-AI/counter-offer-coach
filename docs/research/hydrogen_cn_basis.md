# Hydrogen Cost — China Basis (Checklist Item 4)

*Counter-Offer Coach research note. Derived entirely from three primary sources supplied
by the team; every number below is traceable to a table or stated figure in one of them.
Prepared to replace the US gas-SMR hydrogen derivation when the engine runs on the CN basis.*

**Sources (primary):**
1. **[GAN26]** Gan, Li, Bao, Xu, Wang, Cui (2026), *Comprehensive evaluation of coal
   gasification-based cogeneration systems for hydrogen production with carbon capture*,
   Chinese Journal of Chemical Engineering (pre-proof, doi 10.1016/j.cjche.2026.04.010).
   Aspen Plus model of a Chinese entrained-flow coal-to-H₂ plant with MDEA/MEA capture.
2. **[MUK14]** Mukherjee, Kumar, Hosseini, Yang, Fennell (2014), *Comparative Assessment of
   Gasification Based Coal Power Plants…*, Energy & Fuels 28:1028–1040. IGCC electricity+H₂
   coproduction benchmark (Illinois #6).
3. **[IEA23]** Zhu, ICSC/IEA (2023), *Hydrogen economy and the role for coal*. Carries
   NETL (2022) efficiency comparison (Table 6), Argus regional costs (Table 14), and
   China Hydrogen Alliance production costs for China (Table 15).

---

## 1. The conversion factor (the number the SMR-incident discipline demands)

The factor is expressed in **GJ of coal per kg of H₂**, deliberately energy-based so it is
coal-grade agnostic — the price series' heating value enters separately, which prevents the
grade-mismatch failure mode.

| Source | Plant basis | Factor (GJ coal / kg H₂) | Notes |
|---|---|---|---|
| [GAN26] Tables 13/15 | CN entrained-flow, **with CCUS**, PSA recovery 79% | **0.2318 (LHV)** | Computed 12,776,400 GJ/y ÷ 55,127 t/y. **Internally verified:** 0.2318 × their 2.11 $/GJ = 0.489 $/kg = their stated annual coal cost ÷ annual H₂ exactly. Equivalent: 7.62 kg coal/kg H₂ at LHV 30.36 MJ/kg; implied H₂-only efficiency 51.8 % LHV. |
| [IEA23] Table 6 (NETL 2022) | Shell gasifier, unabated | 0.2183 (HHV) | 141.9 MJ/kg H₂ HHV ÷ 65 % |
| [IEA23] Table 6 (NETL 2022) | Shell gasifier, with CCUS | 0.2214 (HHV) | ÷ 64.1 % — capture costs <1 efficiency point on coal (unlike SMR) |
| [MUK14] Table 8 | IGCC coproduction, all coal charged to H₂ | 0.2580 (LHV) | Conservative upper bound (ignores 152 MW electricity coproduct) |
| [MUK14], energy-allocated | Same, coal split by energy share of outputs | 0.1999 (LHV) | Optimistic lower bound |

**Adopted for the engine: central 0.232 GJ/kg, uncertainty band 0.218–0.258**, carried into
the Monte Carlo like every other Cat 1 parameter. The central value is taken from [GAN26]
because it is (a) a Chinese plant configuration, (b) the only source whose factor we could
verify against its own economics to three decimals, and (c) inclusive of realistic PSA
recovery losses.

## 2. From factor to cost (the formula the connector implements)

```
coal_usd_per_GJ   = (coal_rmb_per_t / fx_cny_usd) / heating_value_GJ_per_t      # ex-VAT price!
h2_fuel_usd_per_kg = 0.232 * coal_usd_per_GJ                                    # band 0.218–0.258
h2_full_usd_per_kg = h2_fuel_usd_per_kg / 0.50                                  # band 0.45–0.55
```

The **coal share of full production cost ≈ 50 %** comes from the China Hydrogen Alliance
Research Institute claim reported in [IEA23] (commentary to Table 15: "the cost of coal
accounts for almost 50 % of the production cost of hydrogen in China"). Heating value
default: **23.0 GJ/t for 5500 kcal/kg NAR thermal coal** — the standard Chinese benchmark
grade — *to be confirmed against the actual SunSirs series spec before --write* (see §5).

**Falsification window (built-in sanity check):** [IEA23] Table 15 gives China **unabated**
coal-gasification H₂ at **1.16–2.32 $/kg** (Sheng 2022, CNY-converted). Our formula lands
inside that window for coal between roughly 410 and 820 RMB/t — i.e., across the normal
Chinese thermal-coal range. If the engine ever computes a CN hydrogen cost outside
~1.0–3.3 $/kg at prevailing coal prices, treat it as a bug (units, FX, VAT, or grade), not
a market signal.

## 3. Sensitivity (also in `h2_cn_sensitivity.csv`; chart in fig1)

| Coal price (RMB/t, 5500 kcal, ex-VAT) | $/GJ | H₂ fuel $/kg | H₂ full $/kg (central) | Aniline H₂ line ($/t, 65.6 kg/t) |
|---|---|---|---|---|
| 500 | 3.06 | 0.71 | 1.42 | 93 |
| 700 | 4.29 | 0.99 | 1.99 | 130 |
| 900 | 5.51 | 1.28 | 2.55 | 168 |
| 1100 | 6.74 | 1.56 | 3.12 | 205 |

**Materiality correction to earlier guidance:** at ~2 $/kg the hydrogen line contributes
≈ **130 $/t aniline — roughly 10 % of the floor**, not the "few percent" previously
assumed from the small mass fraction. Reason: per-kg hydrogen is ~20–30× the price of
benzene. Hydrogen is still well behind benzene (~60–70 % of floor) but it is the largest
*utilities-side* line and getting the basis right is justified, not gold-plating.

## 4. Structural findings that affect the engine design

* **CCUS is NOT the right basis for today's CN aniline floor.** [IEA23] states explicitly
  that *no fossil-with-CCUS hydrogen projects are currently operating in China* and coal
  supplies 56.5 % of Chinese hydrogen (gas 22.3 %, industrial by-product most of the rest).
  [GAN26]'s 1.98 $/kg LCOH includes capture capex, a 43 $/t carbon tax, **and CO₂ sales
  revenue** — a structure a Chinese aniline complex does not have. We therefore take only
  [GAN26]'s *physical conversion factor* (verified, capture barely affects it per NETL:
  <1 efficiency point) and anchor the *cost level* on the unabated Table 15 window.
* **The derivation pattern is unchanged from the US engine**: `energy price × factor →
  $/kg H₂`, exactly like gas-SMR. Only the energy commodity (thermal coal vs natural gas),
  the factor (0.232 GJ/kg vs the SMR MMBtu figure), and a non-fuel adder differ. One new
  branch in `prices.py`, selected by region family.

### Config-ready block (drop into the CN route config when utilities land)

```yaml
hydrogen_cn:
  basis: coal_gasification_unabated        # IEA23: no CCUS H2 operating in CN today
  conversion_gj_coal_per_kg_h2:
    value: 0.232
    band: [0.218, 0.258]
    source: "Gan et al. 2026 CJCHE Tables 13/15 (verified vs own economics); NETL 2022 via IEA/ICSC 2023 Table 6; Mukherjee 2014 Table 8 bounds"
  coal_share_of_full_cost:
    value: 0.50
    band: [0.45, 0.55]
    source: "China Hydrogen Alliance Research Institute, reported in IEA/ICSC 2023 (Table 15 commentary)"
  coal_heating_value_gj_per_t:
    value: 23.0                            # 5500 kcal/kg NAR — CONFIRM vs SunSirs series spec
    source: "standard CN benchmark grade; placeholder until SunSirs grade confirmed"
  sanity_window_usd_per_kg: [1.16, 2.32]   # IEA/ICSC 2023 Table 15, CN unabated (Sheng 2022)
  vat_note: "coal price must be ex-VAT (13% stripped) before entering this formula"
```

## 5. NOT automated / not in the supplied PDFs — explicit comments

1. **How Chinese ANILINE producers actually source hydrogen (the Step-4 gating question).**
   None of the three PDFs says whether Wanhua-class integrated complexes gasify coal for
   H₂, take it as by-product, or buy merchant. [IEA23] confirms by-product H₂ is a
   material share (~20 %) of Chinese supply, so the question is real, and if the answer is
   "captive by-product," the cost above is an *upper bound* (by-product H₂ carries a
   credit/transfer cost, typically lower). **This remains Leo's one-conversation check
   (Wanhua annual report process descriptions / industry profiles) and it gates which
   branch the config selects.** Until answered, `coal_gasification_unabated` is the
   conservative default — defensible because it can only overstate the floor's H₂ line,
   never understate it, preserving the floor-≤-price discipline... with one caveat: an
   overstated input *raises* the floor, so if the CN floor ever violates floor ≤ observed
   price, re-check this line first.
2. **The thermal-coal price series itself.** The figures and CSV are parameterized over
   400–1300 RMB/t; the live input is the licensed SunSirs thermal-coal series (or
   BSPI/CCTD), ingested via the existing connector pattern. Not pulled here — licensing
   and ingest are Gate 1 work, and no real SunSirs prices appear in this note.
3. **Coal grade/heating-value match.** 23.0 GJ/t assumes 5500 kcal/kg NAR. If the SunSirs
   series is 5000 kcal or a different basis (GAR/NAR), change `coal_heating_value_gj_per_t`
   — the factor itself does not change (that's why it's energy-based). One-line check
   against the series description at ingest time.
4. **VAT and FX.** The formula requires the **ex-VAT** coal price; SunSirs publishes
   VAT-inclusive RMB prices, so the global 13 %-strip invariant applies *before* this
   formula, and the FX rate used must be recorded per the fx.py convention. Order:
   strip VAT in RMB, then convert.
5. **Plant-level capex localization.** The 50 % coal-share adder is a Chinese-industry
   aggregate, not a Wanhua-specific number; a tighter adder would need Chinese plant-level
   capex/opex data none of the three PDFs contains. The 0.45–0.55 band carries that
   uncertainty into the Monte Carlo honestly.
6. **The US-vs-CN comparison claim, corrected.** Earlier conversation asserted Chinese
   coal-H₂ is "generally cheaper than US gas-SMR H₂." At 2023-era US gas (~3.3 $/GJ) and
   CN coal above ~700 RMB/t, that is **not** reliably true — [IEA23] Table 14 shows US
   Gulf Coast gas+CCUS H₂ at 1.73 $/kg incl. capex vs China coal+CCUS 3.86–3.94. The
   correct engine-level statement is: each region's H₂ derives from its own energy basis;
   neither is assumed cheaper a priori.

## Files

* `fig1_h2_cost_vs_coal_price.png` — cost curve with bands, IEA falsification window, and
  right axis showing the line's impact per tonne of aniline.
* `fig2_conversion_factor_sources.png` — the three-source factor comparison + [GAN26]
  LCOH structure.
* `h2_cn_sensitivity.csv` — the sensitivity table, 400–1300 RMB/t.
