"""
prices.py -- price resolver for the should-cost engine (Cat 3 seam).

Turns the raw market signals in the Postgres store into a per-ton price for any
chemical the engine needs, so the cost calculators (feedstock 3.1, utility 3.2)
can ask resolve_price_usd_per_ton(chemical_id, period, region, conn) without
caring whether a price was a direct lookup or a derivation.

Resolution rules, by chemical:
  * Direct market feed (e.g. benzene from USITC 2.3): read price_observations,
    skipping NULL unit values, and convert $/kg -> $/ton.
  * Hydrogen: DERIVED from natural gas. Merchant/captive H2 is overwhelmingly
    made by steam methane reforming (SMR), whose cost is gas-dominated, so we
    price the gas feedstock energy: gas $/MMBtu x NG_MMBTU_PER_TON_H2.
  * Nitric acid: DERIVED from ammonia (USGS annual, pipe 2.4, now in
    price_observations). Ostwald is 1:1 molar NH3->HNO3, so the ammonia feedstock
    cost is ammonia $/ton x T_NH3_PER_T_HNO3. Like hydrogen, this is a feedstock
    floor (omits Ostwald conversion), so it sits below market by design.

Everything here is price *derivation*. Cost *assembly* (stoichiometry, yields)
lives in feedstock.py. Keeping them apart means a better price construction
(e.g. the gas-shaped ammonia model) drops in here without touching the calculator.

NOTE on NULL prices: USITC retains incomplete observations with
price_usd_per_kg = NULL, and the storage DDL allows it. Every price query here
therefore filters `price_usd_per_kg IS NOT NULL` -- a NULL must never become a
price.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any  # conn is a psycopg2 connection (DB-agnostic type to avoid a hard import)

# --- Derivation constants --------------------------------------------------
# Natural gas consumed per metric ton of H2 via steam methane reforming, gas
# feedstock-energy basis (MMBtu). Real-plant/efficiency sources cluster 155-180:
#   - Clean Air Task Force: ~10,000 kg/hr SMR uses ~1,800 MMBtu/hr gas
#     -> ~180 MMBtu/ton H2 (with CCS). https://www.catf.us/hydrogen-converter/
#   - SMR efficiency 65-75% (Wikipedia "Steam reforming") on H2 LHV ~120 GJ/ton
#     -> ~155-175 MMBtu/ton.
# Central 165; derivation assumption (wide band in Monte Carlo). Gas feedstock
# energy ONLY -- omits SMR conversion cost (~40% of SMR H2 cost; Nikolaidis &
# Poullikkas 2017), so a floor proxy. (The planning doc's "~12 MMBtu/ton" is
# physically impossible -- H2 holds ~114 MMBtu/ton -- and is not used.)
NG_MMBTU_PER_TON_H2 = 165.0

# Ammonia consumed per metric ton of HNO3 (100% basis) via the Ostwald process.
# Stoichiometry is 1:1 molar (NH3 + 2 O2 -> HNO3 + H2O), so the theoretical mass
# ratio is MW_NH3 / MW_HNO3 = 17.031 / 63.013 = 0.2703 t NH3 / t HNO3 (exact;
# the registry MWs). Modern plants run ~94-96% ammonia conversion, so real
# consumption is ~0.2703 / 0.95 ~= 0.284 t/t. Central 0.284; wide Monte Carlo
# band ~0.270 (theoretical floor) to ~0.30. This prices the AMMONIA FEEDSTOCK of
# nitric acid only -- it omits the Ostwald conversion cost (utilities, platinum
# catalyst, capital), so like NG_MMBTU_PER_TON_H2 it is a feedstock-cost FLOOR
# that sits below market by design (the gap is the premium the Bayesian phase
# estimates). Derivation assumption, not a market quote.
T_NH3_PER_T_HNO3 = 0.284

# ---------------------------------------------------------------------------
# China-basis hydrogen (Step 4 of the China-basis checklist).
#
# Chinese hydrogen is coal-based, not gas-based (coal supplies 56.5% of CN H2;
# IEA/ICSC 2023). Two sourcing realities exist, so two branches:
#
#   standalone_gasification -- merchant / non-integrated producers. Full cash
#     cost of unabated coal gasification:
#       coal $/GJ x GJ_COAL_PER_KG_H2 / CN_COAL_SHARE_OF_FULL_COST
#     Factor 0.232 GJ(LHV)/kg verified against Gan et al. 2026 (CJCHE,
#     doi 10.1016/j.cjche.2026.04.010) Tables 13/15 own economics; band
#     0.218 (NETL 2022 via IEA/ICSC 2023 Table 6) to 0.258 (Mukherjee 2014,
#     Energy & Fuels 28:1028, all-coal-charged bound). Coal share ~50% of full
#     cost: China Hydrogen Alliance via IEA/ICSC 2023 Table 15 commentary.
#     Unabated basis deliberate: IEA/ICSC 2023 reports no fossil-with-CCUS H2
#     operating in China.
#
#   integrated_byproduct -- Wanhua-class complexes ("one head, four tails"):
#     on-site PDH (C3H8 -> C3H6 + H2, ~3.8 wt% H2) and CO-sized coal gasifiers
#     yield captive by-product H2 whose alternative use is FUEL, so its
#     opportunity cost is fuel-equivalent value:
#       coal $/GJ x H2_FUEL_VALUE_GJ_PER_KG  (H2 LHV 120 MJ/kg)
#     A floor by design (omits PSA separation cost), consistent with how this
#     module already floors hydrogen (gas energy only) and nitric acid
#     (ammonia only). Wanhua Yantai co-locates PDH + MDI + aniline (Metso
#     project records; 2018 EIA expansion scope), so this branch applies to
#     integrated suppliers; sourcing default lives in config/hydrogen_cn.yaml.
#
# Sanity window (engine self-check, not an input): CN unabated coal-H2 is
# 1.16-2.32 $/kg (China Hydrogen Alliance / Sheng 2022 via IEA/ICSC 2023
# Table 15). Standalone-branch output far outside ~1.0-3.3 $/kg at prevailing
# coal prices indicates a units/VAT/FX/grade bug, not a market move.
# ---------------------------------------------------------------------------
GJ_COAL_PER_KG_H2 = 0.232          # central; Monte Carlo band 0.218-0.258
CN_COAL_SHARE_OF_FULL_COST = 0.50  # band 0.45-0.55
H2_FUEL_VALUE_GJ_PER_KG = 0.120    # H2 LHV
COAL_GJ_PER_TON = 23.0             # 5500 kcal/kg NAR benchmark; CONFIRM vs SunSirs grade
THERMAL_COAL_CHEMICAL_ID = "thermal_coal"  # price_observations id (SunSirs, CN_SPOT, ex-VAT)
CN_H2_SOURCING_DEFAULT = "standalone_gasification"  # conservative: can only overstate the floor


def cn_coal_price_usd_per_gj(conn: Any, period: str, region: str) -> float:
    """Thermal-coal energy price ($/GJ) from the CN-family price_observations.

    The stored series is USD/kg (SunSirs RMB/t, VAT-stripped and FX-converted at
    ingest by pipeline/sunsirs.py); divide by the benchmark heating value.
    """
    price_kg = _price_observation_usd_per_kg(conn, THERMAL_COAL_CHEMICAL_ID, period, region)
    if price_kg is None:
        raise PriceUnavailable(
            f"{THERMAL_COAL_CHEMICAL_ID}: no CN-family observation at/<= {period}; "
            "ingest the licensed SunSirs thermal-coal series (pipeline/sunsirs.py)"
        )
    return price_kg * 1000.0 / COAL_GJ_PER_TON


def hydrogen_price_usd_per_ton_cn(conn: Any, period: str, region: str,
                                  sourcing: str | None = None) -> float:
    """CN-basis hydrogen $/ton via the two-branch coal derivation (see header)."""
    if sourcing is None:
        sourcing = _hydrogen_cn_sourcing_from_config()
    coal_gj = cn_coal_price_usd_per_gj(conn, period, region)
    if sourcing == "integrated_byproduct":
        return coal_gj * H2_FUEL_VALUE_GJ_PER_KG * 1000.0
    if sourcing == "standalone_gasification":
        return coal_gj * GJ_COAL_PER_KG_H2 / CN_COAL_SHARE_OF_FULL_COST * 1000.0
    raise ValueError(f"unknown hydrogen_cn sourcing {sourcing!r}; "
                     "expected 'standalone_gasification' or 'integrated_byproduct'")


def _hydrogen_cn_sourcing_from_config() -> str:
    """Read the sourcing branch from config/hydrogen_cn.yaml; safe default if absent."""
    try:
        import yaml
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "hydrogen_cn.yaml"
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        sourcing = cfg.get("hydrogen_cn", {}).get("sourcing", CN_H2_SOURCING_DEFAULT)
        return str(sourcing)
    except FileNotFoundError:
        return CN_H2_SOURCING_DEFAULT


HENRY_HUB_UTILITY = "natural_gas_henry_hub"  # utility_observations.utility, $/MMBtu
ELECTRICITY_UTILITY = "electricity_industrial"  # utility_observations.utility, $/kWh

# Steam is not a market series; it is RAISED in a gas-fired boiler, so it derives
# from natural gas the same way hydrogen does. To deliver 1 GJ of steam energy a
# boiler burns 1/efficiency GJ of gas; gas is priced per MMBtu (1 GJ = 0.9478171
# MMBtu). So steam $/GJ = gas $/MMBtu x MMBTU_PER_GJ / BOILER_EFFICIENCY. ~80% is
# a standard industrial gas-boiler efficiency (Towler & Sinnott). This is the gas
# ENERGY cost of steam only -- it omits boiler capital, water treatment, and O&M,
# so like the H2 and HNO3 constants it is a floor; band the efficiency in Monte
# Carlo (~0.75-0.85).
MMBTU_PER_GJ = 0.9478171
BOILER_EFFICIENCY = 0.80


class PriceUnavailable(LookupError):
    """No usable price source for this chemical is in the store yet."""


# --- Region families (cross-country contamination guard) --------------------
# Multi-country connectors (comtrade.py, future sunsirs.py) share the same
# tables as US feeds, tagging rows like "CN_EXPORTS" / "DE_IMPORTS" / "CN_SPOT".
# The resolvers below fall back from the exact region to related regions when a
# month is missing -- but that fallback must NEVER cross a country boundary:
# a US floor month silently priced from a Chinese export value would corrupt
# the premium the model exists to measure.
#
# Rule: a region belongs to a foreign family iff it contains an underscore AND
# its prefix is a known foreign country code ("CN_SPOT" -> CN). Everything else
# (bare "US", "US_IMPORTS_ALL_ORIGINS", EIA state codes like "CA"/"DE") is the
# legacy US family -- bare two-letter codes are US states, not countries, so
# "DE" stays Delaware and only "DE_IMPORTS" is Germany.
FOREIGN_REGION_PREFIXES = frozenset({
    "CN", "DE", "JP", "KR", "IN", "BE", "NL", "FR", "IT", "GB", "ES", "BR",
})


def region_family(region: str) -> str:
    """Family key for fallback compatibility: a foreign ISO prefix, or 'US'."""
    if "_" in region:
        prefix = region.split("_", 1)[0]
        if prefix in FOREIGN_REGION_PREFIXES:
            return prefix
    return "US"


def _family_clause(region: str) -> tuple[str, tuple]:
    """SQL fragment confining a query to `region`'s family.

    Foreign family X: rows whose underscore-prefix is X.
    US family: rows that are NOT foreign-prefixed (bare regions included).
    """
    family = region_family(region)
    if family == "US":
        return (
            "(position('_' in region) = 0 "
            "OR split_part(region, '_', 1) <> ALL(%s))",
            (list(FOREIGN_REGION_PREFIXES),),
        )
    return (
        "(position('_' in region) > 0 AND split_part(region, '_', 1) = %s)",
        (family,),
    )


def _fetchone(conn: Any, sql: str, params: tuple):
    """Run a single-row query through a psycopg2 cursor; return the row or None."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def gas_price_usd_per_mmbtu(conn: Any, period: str,
                            region: str = "US") -> float:
    """Henry Hub natural gas spot, $/MMBtu, for `period` (else latest <= period)."""
    for clause, args in (
        ("utility = %s AND period = %s", (HENRY_HUB_UTILITY, period)),
        ("utility = %s AND period <= %s", (HENRY_HUB_UTILITY, period)),
    ):
        row = _fetchone(
            conn,
            f"SELECT price_usd_per_unit FROM utility_observations WHERE {clause} "
            f"AND price_usd_per_unit IS NOT NULL "
            f"ORDER BY period DESC, fetched_at DESC LIMIT 1",
            args,
        )
        if row is not None:
            return float(row[0])
    raise PriceUnavailable(
        f"{HENRY_HUB_UTILITY} not in store for {period} (run pipeline/eia.py)"
    )


def electricity_price_usd_per_kwh(conn: Any, period: str,
                                  region: str = "US") -> float:
    """Industrial electricity, $/kWh, for `period`/`region` (else latest <=).

    Electricity is a per-state series, so we try the exact region first, then
    any region in the same family, then the most recent at/<= period in the
    same family. State codes ("CA", "TX") are US-family; only underscore-tagged
    foreign regions ("CN_SPOT") form foreign families -- so a Texas request can
    still fall back to another state, but never to a Chinese tariff row.
    """
    fam_sql, fam_params = _family_clause(region)
    for clause, args in (
        ("utility = %s AND period = %s AND region = %s",
         (ELECTRICITY_UTILITY, period, region)),
        (f"utility = %s AND period = %s AND {fam_sql}",
         (ELECTRICITY_UTILITY, period, *fam_params)),
        (f"utility = %s AND period <= %s AND {fam_sql}",
         (ELECTRICITY_UTILITY, period, *fam_params)),
    ):
        row = _fetchone(
            conn,
            f"SELECT price_usd_per_unit FROM utility_observations WHERE {clause} "
            f"AND price_usd_per_unit IS NOT NULL "
            f"ORDER BY period DESC, fetched_at DESC LIMIT 1",
            args,
        )
        if row is not None:
            return float(row[0])
    raise PriceUnavailable(
        f"{ELECTRICITY_UTILITY} not in store for {period} (run pipeline/eia.py)"
    )


def steam_price_usd_per_gj(conn: Any, period: str,
                           region: str = "US",
                           boiler_efficiency: float = BOILER_EFFICIENCY) -> float:
    """Steam, $/GJ, DERIVED from natural gas via a gas-boiler energy balance."""
    gas = gas_price_usd_per_mmbtu(conn, period, region)
    return gas * MMBTU_PER_GJ / boiler_efficiency


def resolve_utility_price_usd_per_unit(utility_kind: str, period: str, region: str,
                                       conn: Any) -> float:
    """Price of a metered utility in its config unit (the 3.2 injection point).

    Returns $/kWh for electricity and $/GJ for steam, matching the config's
    `*_kwh_per_ton_output` and `*_gj_per_ton_output` quantities. Mirrors the
    chemical resolver: direct read for electricity, derivation for steam.
    """
    if utility_kind == "electricity":
        return electricity_price_usd_per_kwh(conn, period, region)
    if utility_kind == "steam":
        return steam_price_usd_per_gj(conn, period, region)
    if utility_kind == "natural_gas":
        return gas_price_usd_per_mmbtu(conn, period, region)
    raise PriceUnavailable(f"unknown utility kind: {utility_kind!r}")


def _price_observation_usd_per_kg(conn: Any, chemical_id: str,
                                  period: str, region: str) -> float | None:
    """Latest non-NULL market price ($/kg) for chemical_id at/<= period.

    Tries region-specific, then any region IN THE SAME FAMILY, then the most
    recent at/<= period IN THE SAME FAMILY. The family restriction
    (see region_family) keeps fallbacks inside one country's market: with only
    US data in the store this is identical to the old behavior, but once
    comtrade/sunsirs rows exist a US request can never silently resolve to a
    CN_/DE_ price (and vice versa).
    NULL unit values (USITC keeps them for incomplete months) are excluded.
    """
    fam_sql, fam_params = _family_clause(region)
    for clause, args in (
        ("chemical_id = %s AND period = %s AND region = %s",
         (chemical_id, period, region)),
        (f"chemical_id = %s AND period = %s AND {fam_sql}",
         (chemical_id, period, *fam_params)),
        (f"chemical_id = %s AND period <= %s AND {fam_sql}",
         (chemical_id, period, *fam_params)),
    ):
        row = _fetchone(
            conn,
            f"SELECT price_usd_per_kg FROM price_observations WHERE {clause} "
            f"AND price_usd_per_kg IS NOT NULL "
            f"ORDER BY period DESC, fetched_at DESC LIMIT 1",
            args,
        )
        if row is not None:
            return float(row[0])
    return None


# Chemicals with no usable price path yet -> name the gap instead of guessing.
# (nitric_acid graduated to a derivation below; this stays as the seam for any
# future feed whose source isn't wired up.)
_PENDING_FEEDS: dict[str, str] = {}


def resolve_price_usd_per_ton(chemical_id: str, period: str, region: str,
                              conn: Any) -> float:
    """Per-ton price for `chemical_id`. Raises PriceUnavailable if no source yet."""
    if chemical_id == "hydrogen":
        # Basis follows the region family: CN hydrogen is coal-derived (two-branch,
        # see header), everything else keeps the gas-SMR derivation. Never let a
        # CN floor silently price hydrogen off US Henry Hub gas, or vice versa.
        if region_family(region) == "CN":
            return hydrogen_price_usd_per_ton_cn(conn, period, region)
        return gas_price_usd_per_mmbtu(conn, period, region) * NG_MMBTU_PER_TON_H2

    if chemical_id == "nitric_acid":
        # Feedstock floor: ammonia $/ton x stoichiometric NH3 consumed per ton HNO3.
        # Needs ammonia in the store (run pipeline/usgs_minerals.py --write).
        try:
            ammonia_per_ton = resolve_price_usd_per_ton("ammonia", period, region, conn)
        except PriceUnavailable as exc:
            raise PriceUnavailable(
                f"nitric_acid derives from ammonia, which is unavailable: {exc}"
            ) from exc
        return ammonia_per_ton * T_NH3_PER_T_HNO3

    if chemical_id in _PENDING_FEEDS:
        raise PriceUnavailable(f"{chemical_id}: {_PENDING_FEEDS[chemical_id]}")

    price_kg = _price_observation_usd_per_kg(conn, chemical_id, period, region)
    if price_kg is not None:
        return price_kg * 1000.0  # $/kg -> $/ton

    raise PriceUnavailable(
        f"{chemical_id}: no non-NULL price_observations row at/<= {period}"
    )
