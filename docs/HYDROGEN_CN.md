# Hydrogen — China Basis: the Two-Branch Model (Step 4, RESOLVED)

*Decision doc. The full source-by-source derivation with figures lives in
`docs/research/hydrogen_cn_basis.md`. Config: `config/hydrogen_cn.yaml`.
Code: `engine/prices.py` (CN hydrogen block) + `pipeline/sunsirs.py` (thermal
coal mapping, VAT invariant). Tests: `tests/test_hydrogen_cn.py`.*

## The question this resolves

The CN floor needs a hydrogen cost. US hydrogen derives from gas (SMR);
Chinese hydrogen derives from coal — but *which* coal economics depends on
who the producer is. Two sourcing realities exist in China:

1. **Standalone / merchant** — hydrogen made on purpose by unabated coal
   gasification. Full cash cost.
2. **Integrated (Wanhua-class)** — the "one head, four tails" complex:
   a coal gasifier sized to the MDI line's CO demand plus on-site PDH units
   (C₃H₈ → C₃H₆ + H₂, ~3.8 wt% H₂) throw off captive by-product hydrogen
   that is piped to the aniline unit. Its opportunity cost is what the H₂
   would otherwise be worth **as fuel** — far below gasification cash cost.
   Wanhua's Yantai park co-locates the PDH plant with the MDI chain and the
   aniline/nitrobenzene/nitric-acid plants (Metso MDI-integration project
   records; the 2018 EIA expansion scope lists all three aniline-chain
   plants on site).

## The two formulas

Both consume the same input: the licensed SunSirs **thermal coal** series
(`chemical_id = thermal_coal`, region `CN_SPOT`, ex-VAT, USD/kg at ingest).

| Branch | Formula ($/kg H₂) | At 700 RMB/t coal |
|---|---|---|
| `standalone_gasification` (default) | coal $/GJ × **0.232** ÷ **0.50** | ≈ 1.99 |
| `integrated_byproduct` | coal $/GJ × **0.120** (H₂ LHV fuel value) | ≈ 0.51 |

* **0.232 GJ coal / kg H₂** (band 0.218–0.258): Gan et al. 2026 (CJCHE)
  verified against its own economics; NETL 2022 and Mukherjee 2014 bound it.
* **÷ 0.50** (band 0.45–0.55): coal ≈ 50 % of full CN production cost
  (China Hydrogen Alliance via IEA/ICSC 2023).
* **× 0.120**: by-product H₂'s alternative use is fuel, so fuel-equivalent
  energy value. A floor by design (omits PSA separation) — the same floor
  philosophy as the engine's gas-SMR hydrogen and ammonia-only nitric acid.

**Falsification window:** CN unabated coal-H₂ is 1.16–2.32 $/kg (IEA/ICSC
2023 Table 15). The standalone branch sits inside it across normal coal
prices; output outside ~1.0–3.3 $/kg = bug (units/VAT/FX/grade), not market.

## Branch selection

`config/hydrogen_cn.yaml → hydrogen_cn.sourcing`. Default is
`standalone_gasification` — conservative because it can only **overstate**
the floor. The by-product branch is structurally lower (0.120 < 0.464
effective factor), so flipping an integrated supplier to it only lowers the
floor — it can never create a floor ≤ price violation. Per-supplier
selection (e.g., "supplier is Wanhua → integrated") belongs in the Phase-2
calibration layer where supplier identity enters; the engine seam is ready.

Diagnostic rule: **if the CN floor ever violates floor ≤ observed price,
check this branch selection first** — the asset is probably coasting on
captive by-product hydrogen while the model charges it gasification cash
cost.

## What changed in the code (this batch)

1. `engine/prices.py`: region-routed hydrogen — `region_family == "CN"` →
   coal two-branch; else gas-SMR unchanged. Plus `cn_coal_price_usd_per_gj`
   and `hydrogen_price_usd_per_ton_cn`.
2. `pipeline/sunsirs.py`: thermal-coal commodity aliases; **VAT invariant
   implemented** — every SunSirs series is stripped of 13 % VAT in RMB
   *before* FX conversion (it was specified but never coded; all previously
   ingested SunSirs rows are ~13 % high and must be re-ingested).
3. `config/hydrogen_cn.yaml`: both branches, fully cited, with bands.
4. `data/licensed/` git-ignored; the committed SunSirs aniline CSV moved
   there and untracked (see Open Items re: git history).

## Open items

* **Confirm the SunSirs thermal-coal grade** vs the 23.0 GJ/t (5500 kcal
  NAR) assumption — one line in `config/hydrogen_cn.yaml` if different.
* **Pull the thermal-coal series** in the same SunSirs session as
  benzene/ammonia; place in `data/licensed/`, dry-run, `--write`.
* **Re-ingest all SunSirs series** after the VAT fix (stored rows predate it).
* **Leo's confirmation** of the Wanhua hydrogen routing upgrades the
  integrated branch from "industry-blueprint inference" to "EIA-cited";
  the conservative default stands until then.
* **Git history still contains the licensed CSV** (commit history predates
  the untracking). Purging requires `git filter-repo` + force push — team
  decision, not done unilaterally here.
