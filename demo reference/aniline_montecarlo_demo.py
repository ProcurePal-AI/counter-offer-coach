"""
aniline_montecarlo_demo.py
=====================================================================
A lightweight adaptation of BioSTEAM's uncertainty-analysis pattern,
fitted to our aniline should-cost engine.
 
This is a REFERENCE DEMO. The numbers are placeholders (see MARKET and
PARAMETERS below) — the point is to show that the mechanism runs
end to end, not to produce a defensible price. Real values get plugged
in once our Cat 2 live-data pipeline (EIA / USITC) is wired up.
 
Three patterns borrowed from BioSTEAM:
  1) Parameter = (distribution + setter)  -> declared with a distribution,
     not a single value
  2) Latin Hypercube Sampling (LHS)       -> covers the input space more
     evenly than plain random sampling
  3) Spearman rank-order sensitivity      -> "which input drives price
     uncertainty the most" (tornado)
 
How this differs from BioSTEAM (our philosophy vs theirs):
  - BioSTEAM: simulates the process to DERIVE yields / energy use
  - Ours:     treats yields / energy use as FIXED config constants and
              builds up cost by multiplication
  So we don't need a heavy thermodynamic engine — plain numpy is enough.
 
Dependencies: numpy, scipy  (pip install numpy scipy)
=====================================================================
"""
 
import numpy as np
from scipy import stats
 
# =====================================================================
# 1. Cost build-up model (deterministic core)
#    This is the core formula of our Cat 3 Should-Cost Engine.
#    market_prices (benzene / electricity / gas) are the "fixed inputs"
#    that will come from external data (EIA / USITC); process params
#    (yields / energy) are the "uncertain inputs" from our config.
# =====================================================================
 
def fair_price_per_ton(
    # --- Market prices (external data, single-month snapshot) ---
    benzene_usd_per_t,      # benzene price, USD/t
    elec_usd_per_kwh,       # electricity, USD/kWh
    nat_gas_usd_per_gj,     # natural gas, USD/GJ (basis for steam / hydrogen cost)
    # --- Process parameters (config, uncertain) ---
    yield_nitration,        # nitration yield (0-1)
    yield_hydrogenation,    # hydrogenation yield (0-1)
    steam_gj_per_t,         # steam consumption per ton, GJ
    elec_kwh_per_t,         # electricity consumption per ton, kWh
    # --- Markup (config, uncertain) ---
    variable_markup,        # variable conversion cost (fraction of feedstock)
    fixed_markup,           # fixed overhead (fraction of feedstock + utility)
    target_margin,          # target margin
    # --- Fixed stoichiometric constants ---
    benzene_t_per_t=0.85,   # benzene input per ton (stoichiometric)
    boiler_eff=0.82,        # boiler efficiency (for steam cost conversion)
):
    """Compute the fair price (USD/t) of one ton of aniline via cost build-up.
 
    Returns: (total_price, breakdown)  where breakdown is the per-layer dict.
    """
    overall_yield = yield_nitration * yield_hydrogenation
 
    # --- (1) feedstock cost ---
    # divide benzene input by yield to correct for actual quantity needed
    feedstock = (benzene_usd_per_t * benzene_t_per_t) / overall_yield
 
    # --- (2) utility cost ---
    # steam: convert natural gas price to a per-GJ cost via boiler efficiency
    steam_cost = steam_gj_per_t * (nat_gas_usd_per_gj / boiler_eff)
    elec_cost = elec_kwh_per_t * elec_usd_per_kwh
    utility = steam_cost + elec_cost
 
    # --- (3) markup layers ---
    variable_cost = feedstock * variable_markup
    fixed_cost = (feedstock + utility) * fixed_markup
    base = feedstock + utility + variable_cost + fixed_cost
    margin = base * target_margin
 
    total = base + margin
 
    breakdown = {
        "feedstock": feedstock,
        "utility": utility,
        "variable_markup": variable_cost,
        "fixed_markup": fixed_cost,
        "margin": margin,
    }
    return total, breakdown
 
 
# =====================================================================
# 2. Parameter definitions (BioSTEAM pattern: distribution + metadata)
#    BioSTEAM uses an @model.parameter decorator; here we keep it simple
#    with a list of dicts. The key idea is "declare a distribution".
#
#    distribution:
#      "triangular" = (low, base, high)  -> when the most-likely value is
#                                           known (most yields / energy)
#      "uniform"    = (low, high)        -> when only the range is known
# =====================================================================
 
# Values read from Ullmann's etc. base = most-likely, low/high = uncertainty range.
# NOTE: ranges are illustrative for the demo; verify against primary sources.
PARAMETERS = [
    # name,                    dist,         (low, base, high) or (low, high)
    ("yield_nitration",        "triangular", (0.97, 0.98, 0.985)),
    ("yield_hydrogenation",    "triangular", (0.98, 0.99, 0.995)),
    ("steam_gj_per_t",         "triangular", (3.0,  4.0,  5.0)),
    ("elec_kwh_per_t",         "triangular", (200., 300., 400.)),
    ("variable_markup",        "triangular", (0.08, 0.11, 0.15)),
    ("fixed_markup",           "triangular", (0.05, 0.075, 0.10)),
    ("target_margin",          "uniform",    (0.05, 0.15)),
]
 
# Market prices are "fixed inputs", so no distribution — single values
# (one-month snapshot example; PLACEHOLDER numbers).
MARKET = {
    "benzene_usd_per_t": 950.0,
    "elec_usd_per_kwh": 0.075,
    "nat_gas_usd_per_gj": 4.0,
}
 
 
# =====================================================================
# 3. Latin Hypercube Sampling (BioSTEAM's rule='L')
#    Split each dimension into N bins, draw exactly one sample per bin
#    -> covers the space evenly. Then map to real values via the
#    distribution's inverse CDF (ppf).
# =====================================================================
 
def _lhs_unit(n_samples, n_dims, seed=1234):
    """Generate LHS samples on [0,1) (shape: n_samples x n_dims)."""
    rng = np.random.default_rng(seed)
    result = np.empty((n_samples, n_dims))
    for j in range(n_dims):
        # bin centers + small jitter, then shuffle
        cut = (np.arange(n_samples) + rng.random(n_samples)) / n_samples
        rng.shuffle(cut)
        result[:, j] = cut
    return result
 
 
def _to_distribution(u_col, dist, params):
    """Map uniform [0,1) samples to the given distribution (inverse CDF)."""
    if dist == "triangular":
        low, base, high = params
        c = (base - low) / (high - low)  # shape parameter for scipy triang
        return stats.triang.ppf(u_col, c, loc=low, scale=high - low)
    elif dist == "uniform":
        low, high = params
        return stats.uniform.ppf(u_col, loc=low, scale=high - low)
    else:
        raise ValueError(f"unknown distribution: {dist}")
 
 
# =====================================================================
# 4. Run Monte Carlo -> P10/P50/P90 + sensitivity
# =====================================================================
 
def run_monte_carlo(n_samples=1000, seed=1234):
    names = [p[0] for p in PARAMETERS]
    n_dims = len(PARAMETERS)
 
    # 4-1) LHS samples on [0,1) -> map each to its parameter distribution
    u = _lhs_unit(n_samples, n_dims, seed)
    sampled = {}
    for j, (name, dist, params) in enumerate(PARAMETERS):
        sampled[name] = _to_distribution(u[:, j], dist, params)
 
    # 4-2) compute price for each sample
    prices = np.empty(n_samples)
    for i in range(n_samples):
        kwargs = {name: sampled[name][i] for name in names}
        total, _ = fair_price_per_ton(**MARKET, **kwargs)
        prices[i] = total
 
    # 4-3) percentiles (BioSTEAM also reads percentiles off the distribution)
    p10, p50, p90 = np.percentile(prices, [10, 50, 90])
 
    # 4-4) Spearman rank-order sensitivity (BioSTEAM's model.spearman_r)
    #      rank correlation between each input and the output price;
    #      the largest absolute value is the most influential input.
    sensitivity = {}
    for name in names:
        rho, _ = stats.spearmanr(sampled[name], prices)
        sensitivity[name] = rho
 
    return {
        "prices": prices,
        "p10": p10, "p50": p50, "p90": p90,
        "sensitivity": sensitivity,
    }
 
 
def _ascii_tornado(sensitivity):
    """Simple terminal tornado chart (sorted by |Spearman|)."""
    items = sorted(sensitivity.items(), key=lambda kv: abs(kv[1]), reverse=True)
    print("\nTornado (input -> price sensitivity, Spearman rho)")
    print("-" * 60)
    max_abs = max(abs(v) for _, v in items) or 1.0
    for name, rho in items:
        bar_len = int(abs(rho) / max_abs * 30)
        sign = "+" if rho >= 0 else "-"
        print(f"  {name:22s} {sign} |{'#' * bar_len:30s}| {rho:+.3f}")
    print("-" * 60)
 
 
# =====================================================================
# 5. Demo run
# =====================================================================
 
if __name__ == "__main__":
    print("=" * 60)
    print("Aniline Should-Cost Monte Carlo (lightweight BioSTEAM pattern)")
    print("=" * 60)
 
    # Baseline: compute once with every parameter at its most-likely value
    base_kwargs = {}
    for name, dist, params in PARAMETERS:
        base_kwargs[name] = params[1] if dist == "triangular" else np.mean(params)
    base_price, breakdown = fair_price_per_ton(**MARKET, **base_kwargs)
 
    print(f"\n[Baseline] fair price = {base_price:,.1f} USD/t")
    print("  Cost breakdown by layer:")
    for k, v in breakdown.items():
        print(f"    {k:18s} {v:8,.1f} USD/t  ({v/base_price*100:5.1f}%)")
 
    # Monte Carlo
    res = run_monte_carlo(n_samples=1000)
    print(f"\n[Monte Carlo, 1000 samples (LHS)]")
    print(f"  P10 = {res['p10']:,.1f} USD/t")
    print(f"  P50 = {res['p50']:,.1f} USD/t  (median)")
    print(f"  P90 = {res['p90']:,.1f} USD/t")
    print(f"  band width = {res['p90'] - res['p10']:,.1f} USD/t "
          f"(+/-{(res['p90']-res['p10'])/2/res['p50']*100:.1f}% of P50)")
 
    _ascii_tornado(res["sensitivity"])
 
    print("\nReading: the larger the |Spearman|, the more that input drives")
    print("         price uncertainty -> prioritize verifying that input")
    print("         (securing a primary source for it).")