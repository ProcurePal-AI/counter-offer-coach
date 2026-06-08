"""
prices.py -- price resolver for the should-cost engine (Cat 3 seam).

Turns the raw market signals in the SQLite store into a per-ton price for any
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

import sqlite3

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


def gas_price_usd_per_mmbtu(conn: sqlite3.Connection, period: str,
                            region: str = "US") -> float:
    """Henry Hub natural gas spot, $/MMBtu, for `period` (else latest <= period)."""
    for clause, args in (
        ("utility = ? AND period = ?", (HENRY_HUB_UTILITY, period)),
        ("utility = ? AND period <= ?", (HENRY_HUB_UTILITY, period)),
    ):
        row = conn.execute(
            f"SELECT price_usd_per_unit FROM utility_observations WHERE {clause} "
            f"AND price_usd_per_unit IS NOT NULL "
            f"ORDER BY period DESC, fetched_at DESC LIMIT 1",
            args,
        ).fetchone()
        if row is not None:
            return float(row[0])
    raise PriceUnavailable(
        f"{HENRY_HUB_UTILITY} not in store for {period} (run pipeline/eia.py)"
    )


def electricity_price_usd_per_kwh(conn: sqlite3.Connection, period: str,
                                  region: str = "US") -> float:
    """Industrial electricity, $/kWh, for `period`/`region` (else latest <=).

    Electricity is a per-state series, so we try the exact region first, then any
    region, then the most recent at/<= period.
    """
    for clause, args in (
        ("utility = ? AND period = ? AND region = ?", (ELECTRICITY_UTILITY, period, region)),
        ("utility = ? AND period = ?", (ELECTRICITY_UTILITY, period)),
        ("utility = ? AND period <= ?", (ELECTRICITY_UTILITY, period)),
    ):
        row = conn.execute(
            f"SELECT price_usd_per_unit FROM utility_observations WHERE {clause} "
            f"AND price_usd_per_unit IS NOT NULL "
            f"ORDER BY period DESC, fetched_at DESC LIMIT 1",
            args,
        ).fetchone()
        if row is not None:
            return float(row[0])
    raise PriceUnavailable(
        f"{ELECTRICITY_UTILITY} not in store for {period} (run pipeline/eia.py)"
    )


def steam_price_usd_per_gj(conn: sqlite3.Connection, period: str,
                           region: str = "US",
                           boiler_efficiency: float = BOILER_EFFICIENCY) -> float:
    """Steam, $/GJ, DERIVED from natural gas via a gas-boiler energy balance."""
    gas = gas_price_usd_per_mmbtu(conn, period, region)
    return gas * MMBTU_PER_GJ / boiler_efficiency


def resolve_utility_price_usd_per_unit(utility_kind: str, period: str, region: str,
                                       conn: sqlite3.Connection) -> float:
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


def _price_observation_usd_per_kg(conn: sqlite3.Connection, chemical_id: str,
                                  period: str, region: str) -> float | None:
    """Latest non-NULL market price ($/kg) for chemical_id at/<= period.

    Tries region-specific, then any region, then most recent at/<= period.
    NULL unit values (USITC keeps them for incomplete months) are excluded.
    """
    for clause, args in (
        ("chemical_id = ? AND period = ? AND region = ?", (chemical_id, period, region)),
        ("chemical_id = ? AND period = ?", (chemical_id, period)),
        ("chemical_id = ? AND period <= ?", (chemical_id, period)),
    ):
        row = conn.execute(
            f"SELECT price_usd_per_kg FROM price_observations WHERE {clause} "
            f"AND price_usd_per_kg IS NOT NULL "
            f"ORDER BY period DESC, fetched_at DESC LIMIT 1",
            args,
        ).fetchone()
        if row is not None:
            return float(row[0])
    return None


# Chemicals with no usable price path yet -> name the gap instead of guessing.
# (nitric_acid graduated to a derivation below; this stays as the seam for any
# future feed whose source isn't wired up.)
_PENDING_FEEDS: dict[str, str] = {}


def resolve_price_usd_per_ton(chemical_id: str, period: str, region: str,
                              conn: sqlite3.Connection) -> float:
    """Per-ton price for `chemical_id`. Raises PriceUnavailable if no source yet."""
    if chemical_id == "hydrogen":
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
