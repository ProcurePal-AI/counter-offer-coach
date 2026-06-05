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
  * Nitric acid: source data (USGS annual ammonia, 2.4) exists in design but the
    ammonia->nitric-acid cost conversion is not yet sourced, so we raise rather
    than fabricate a factor.

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

HENRY_HUB_UTILITY = "natural_gas_henry_hub"  # utility_observations.utility, $/MMBtu


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
_PENDING_FEEDS = {
    "nitric_acid": (
        "USGS provides annual ammonia (pipe 2.4, USD/short ton, table "
        "ammonia_price_usgs), but (a) usgs_minerals.py currently has a syntax "
        "error and (b) the ammonia->nitric-acid cost conversion (Ostwald, incl. "
        "processing) is not yet sourced -- not derived (won't guess a factor)"
    ),
}


def resolve_price_usd_per_ton(chemical_id: str, period: str, region: str,
                              conn: sqlite3.Connection) -> float:
    """Per-ton price for `chemical_id`. Raises PriceUnavailable if no source yet."""
    if chemical_id == "hydrogen":
        return gas_price_usd_per_mmbtu(conn, period, region) * NG_MMBTU_PER_TON_H2

    if chemical_id in _PENDING_FEEDS:
        raise PriceUnavailable(f"{chemical_id}: {_PENDING_FEEDS[chemical_id]}")

    price_kg = _price_observation_usd_per_kg(conn, chemical_id, period, region)
    if price_kg is not None:
        return price_kg * 1000.0  # $/kg -> $/ton

    raise PriceUnavailable(
        f"{chemical_id}: no non-NULL price_observations row at/<= {period}"
    )
