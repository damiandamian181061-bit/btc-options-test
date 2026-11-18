"""Golden reference values for the pricing tests, produced by Damian's btc-vol-models code.

btc-vol-models (github.com/damiandamian181061-bit/btc-vol-models) prices with Black-Scholes on a
spot S0 and a rate r. With r = 0 and S0 set to the forward F, that is exactly Black-76 on the
forward with no discounting, which is how Deribit prices BTC options (interest rate 0). So its
numbers are a fair reference for ``optlab.pricing.black76``.

This script imports btc-vol-models from a clone (never from this repo), runs its own functions
and writes ``tests/pricing/golden/btc_vol_models.json``:

- ``grid``: ``models.black_scholes.bs_price`` and ``implied_vol`` and ``greeks.compute_greeks``
  ("Black-Scholes", its finite-difference Greeks) on a grid of forwards, strikes, expiries and
  vols, with r = 0;
- ``greeks_table``: the Black-Scholes rows of its committed ``greeks_table.csv`` (the synthetic
  study's output, S0 = 60,000, r = 0), exactly as committed.

btc-vol-models' Greek conventions (its greeks.py docstring): delta and gamma by central
differences in S (1 bp bump); vega = change per 1 vol point (x 0.01) by central differences;
theta = price one calendar day later minus price now, spot fixed (a discrete one-day change,
not the instantaneous derivative).

    git clone https://github.com/damiandamian181061-bit/btc-vol-models /tmp/btc-vol-models
    uv run python research/btc_vol_models_golden.py /tmp/btc-vol-models
"""

import argparse
import csv
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

OUT = (
    Path(__file__).resolve().parent.parent / "tests" / "pricing" / "golden" / "btc_vol_models.json"
)

FORWARDS = (60_000.0, 84_000.0)
MONEYNESS = (0.7, 0.8, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2, 1.4)
DAYS = (1.0, 7.0, 30.0, 90.0)
VOLS = (0.3, 0.5, 0.8)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("clone", type=Path, help="a clone of btc-vol-models")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    clone = args.clone.resolve()
    commit = subprocess.run(  # noqa: S603 (fixed git command on a path we were given)
        ["git", "-C", str(clone), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    sys.path.insert(0, str(clone))
    bs = importlib.import_module("models.black_scholes")
    greeks = importlib.import_module("greeks")

    grid: list[dict[str, Any]] = []
    for f in FORWARDS:
        for m in MONEYNESS:
            k = round(f * m, 2)
            for days in DAYS:
                t = days / 365.0
                for sigma in VOLS:
                    for kind in ("call", "put"):
                        price = float(bs.bs_price(f, k, t, 0.0, sigma, kind))
                        g = greeks.compute_greeks(
                            "Black-Scholes", {"sigma": sigma}, 0.0, f, [k], t, kind
                        )
                        iv = float(bs.implied_vol(price, f, k, t, 0.0, kind))
                        grid.append(
                            {
                                "forward": f,
                                "strike": k,
                                "tau": t,
                                "sigma": sigma,
                                "option_type": kind,
                                "price": price,
                                "implied_vol": None if iv != iv else iv,
                                "delta": float(g["delta"][0]),
                                "gamma": float(g["gamma"][0]),
                                "vega": float(g["vega"][0]),
                                "theta_one_day": float(g["theta"][0]),
                            }
                        )

    table: list[dict[str, Any]] = []
    with (clone / "greeks_table.csv").open(newline="") as fh:
        for row in csv.DictReader(fh):
            if row["model"] != "Black-Scholes":
                continue
            table.append(
                {
                    "tau": float(row["T"]),
                    "strike": float(row["K"]),
                    "option_type": row["option_type"],
                    "price": float(row["price"]),
                    "delta": float(row["delta"]),
                    "gamma": float(row["gamma"]),
                    "vega": float(row["vega"]),
                    "theta_one_day": float(row["theta"]),
                }
            )

    doc = {
        "source": "github.com/damiandamian181061-bit/btc-vol-models",
        "commit": commit,
        "generated_by": "research/btc_vol_models_golden.py",
        "conventions": {
            "model": "Black-Scholes with r = 0 and S0 = forward (= Black-76, undiscounted)",
            "delta": "dV/dS, central difference, 1 bp bump",
            "gamma": "d2V/dS2, central difference, 1 bp bump",
            "vega": "dV/dsigma x 0.01 (per 1 vol point), central difference",
            "theta_one_day": "V(tau - 1/365) - V(tau), spot fixed (one calendar day)",
            "implied_vol": "models.black_scholes.implied_vol (brentq on [1e-6, 5]); null if none",
        },
        "greeks_table_spot": 60_000.0,
        "grid": grid,
        "greeks_table": table,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"{len(grid)} grid cases and {len(table)} greeks_table rows from {commit} -> {args.out}")


if __name__ == "__main__":
    main()
