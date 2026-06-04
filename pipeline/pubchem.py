"""
pubchem.py -- One-time static pull of chemical identity data from PubChem PUG-REST.

Populates config/chemical_registry.yaml for the aniline production chain:
benzene (feedstock) -> nitrobenzene (intermediate) -> aniline (product).

PubChem PUG-REST is free and requires no API key.

Run:  python pipeline/pubchem.py

Note on density: PUG-REST's property endpoint does not expose density (it lives in
the PUG-View experimental-annotation layer and is not cleanly structured). Density is
not used by the should-cost mass balance, which is molecular-weight driven, so it is
intentionally omitted here. Add a PUG-View pull later only if a downstream layer needs it.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import requests
import yaml

PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

# CIDs are PubChem identities. HTS codes and status are project decisions
# (not PubChem data), pinned here and merged into the pulled record.
CHEMICALS = {
    "benzene":      {"cid": 241,  "hts_codes": ["2902.20.00"], "status": "feedstock_only"},
    "nitrobenzene": {"cid": 7416, "hts_codes": ["2904.20.10"], "status": "feedstock_only"},
    "aniline":      {"cid": 6115, "hts_codes": ["2921.41.20"], "status": "active"},
    "nitric_acid":  {"cid": 944,  "hts_codes": ["2808.00.00"], "status": "feedstock_only"},
    "hydrogen":     {"cid": 783,  "hts_codes": ["2804.10.00"], "status": "feedstock_only"},
    "water":        {"cid": 962,  "hts_codes": [],             "status": "feedstock_only"},
}

CAS_RE = re.compile(r"^\d{2,7}-\d{2}-\d$")


def fetch_properties(cid: int) -> dict:
    url = (
        f"{PUBCHEM_BASE}/compound/cid/{cid}"
        "/property/MolecularWeight,IUPACName,MolecularFormula/JSON"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()["PropertyTable"]["Properties"][0]


def fetch_cas(cid: int) -> str | None:
    url = f"{PUBCHEM_BASE}/compound/cid/{cid}/synonyms/JSON"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    synonyms = r.json()["InformationList"]["Information"][0].get("Synonym", [])
    for s in synonyms:
        if CAS_RE.match(s):
            return s
    return None


def build_registry() -> dict:
    chemicals = {}
    for name, meta in CHEMICALS.items():
        cid = meta["cid"]
        print(f"  pulling {name} (CID {cid})...")
        props = fetch_properties(cid)
        chemicals[name] = {
            "cas": fetch_cas(cid),
            "pubchem_cid": cid,
            "iupac_name": props.get("IUPACName"),
            "molecular_formula": props.get("MolecularFormula"),
            "molecular_weight_g_per_mol": float(props["MolecularWeight"]),
            "hts_codes": meta["hts_codes"],
            "status": meta["status"],
        }
        time.sleep(0.25)  # be polite to the public API
    return {"chemicals": chemicals}


def main() -> int:
    out_path = Path(__file__).resolve().parents[1] / "config" / "chemical_registry.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print("Pulling chemical identity data from PubChem...")
    registry = build_registry()
    with out_path.open("w") as f:
        yaml.safe_dump(registry, f, sort_keys=False, default_flow_style=False)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
