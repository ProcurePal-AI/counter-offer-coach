# Counter Offer Coach

A should-cost and Bayesian calibration engine for specialty chemicals procurement. Takes a supplier quote, computes a bottom-up fair price from chemistry fundamentals, and produces a negotiation-ready counter-offer with a fully itemized cost breakdown.

**Current status:** Phase 1 (price layer) in progress — aniline demo chemical. Phase 2 (Bayesian calibration) begins after Phase 1 validation.

---

## How it works

The engine has five layers:

| Layer | What it does |
|---|---|
| **1. Text Parsing** | Reads a supplier quote (PDF, email, pasted text) and extracts structured fields: chemical, quantity, price, region, period, grade |
| **2. Should-Cost Engine** | Computes a bottom-up fair price from stoichiometry, live feedstock prices, utility costs, and industry markup heuristics. Outputs a P10/P50/P90 price band |
| **3. Bayesian Calibration** | Updates the should-cost prior with market evidence (USITC customs data, internal purchase history) to separate the physical cost floor from an estimated supplier premium |
| **4. Counter-Offer Reasoning** | Decomposes the gap between the quote and the fair price, constructs defensible talking points, and drafts a counter-offer with cited cost drivers |
| **5. Decision Ledger** | Every input, prediction, and calibration step is logged for auditability and reproducibility |

The core architectural insight: the physical cost floor (stoichiometry, energy, catalyst) is mathematically protected from being moved by biased market data. Observed overpayments are isolated and measured as an explicit premium — which is the leverage the buyer carries into a negotiation.

---

## Repo structure

```
counter-offer-coach/
├── config/          # Chemical registry (PubChem) and process YAML configs (aniline, etc.)
├── pipeline/        # Live data connectors — EIA, USITC, USGS, EDGAR, PubChem
├── engine/          # Should-cost engine: predict_fair_price(month, region) → {p10, p50, p90, breakdown}
├── dashboard/       # Streamlit validation dashboard (predicted vs. observed, error decomposition)
├── docs/
│   └── schema/      # Master schema — chemical registry, process config, market observations, predictions
├── tests/           # pytest test suite
└── data/            # Shared PostgreSQL DB — git-ignored; holds live price pulls
```

---

## Data sources

| Source | What it provides | Update frequency |
|---|---|---|
| PubChem PUG-REST | MW, CAS, IUPAC for benzene / nitrobenzene / aniline | One-time static pull |
| EIA Open Data API | Industrial electricity by state, Henry Hub natural gas | Monthly |
| USITC DataWeb | Import unit values — HTS 2902.20.00 (benzene), HTS 2921.41.20 (aniline) | Monthly |
| USGS Minerals Yearbook | Ammonia price → nitric acid cost proxy | Annual |
| SEC EDGAR | 10-K/10-Q filings for BASF, Covestro, Huntsman, Tosoh | Quarterly |

Note: ICIS is deferred until Phase 2. The prototype proves the mechanism on free public data; ICIS adds precision on specialty grade premiums and is a one-line pipe swap when available.

---

## Setup

```bash
git clone https://github.com/[org]/counter-offer-coach
cd counter-offer-coach
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install
pytest
```

A green pytest run confirms the environment is wired up. CI runs on every push via GitHub Actions.

---

## Demo chemical: aniline

Aniline (CAS 62-53-3) is produced via a two-step fossil route:

1. **Nitration** — benzene + HNO₃ → nitrobenzene (~98% yield)
2. **Hydrogenation** — nitrobenzene + H₂ → aniline (~99% yield)

Key cost drivers: benzene spot price (USITC), hydrogen cost (derived from Henry Hub via SMR factor), industrial electricity (EIA). Overall stoichiometric yield ~97%; ~0.85 t benzene per ton aniline output.

---

## What's next

- **Phase 1 (Weeks 1–5):** Complete the should-cost engine and validate against USITC historical data. Deliverable: Streamlit dashboard + validation memo identifying systematic biases.
- **Phase 2 (Weeks 6–10):** Bayesian calibration layer — prior × market evidence → posterior price band with explicit floor/premium decomposition. Deliverable: calibrated callable + assessment memo defining allowed claims for the counter-offer layer.
- **Phase 3:** Counter-offer reasoning layer + buyer-facing UI. ICIS integration for tighter specialty grade premiums.

---

## Team

| Role | Owner |
|---|---|
| Project Lead / Architecture | Martin |
| Process Knowledge (Cat 1) | TBD |
| Data Engineering (Cat 2) | TBD |
| Modeler / Engine (Cat 3) | TBD |
| Analyst / Validation (Cat 4) | TBD |
