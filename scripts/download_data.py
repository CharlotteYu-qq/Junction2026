"""Download the Valio Aimo challenge data files into data/valio/ using AIMO_API_KEY from .env.

    python scripts/download_data.py            # into data/valio/
    python scripts/download_data.py --out DIR  # somewhere else
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "/api/products/download": "valio_aimo_junction26_product_data.json",
    "/api/delivery-reliability/download": "valio_aimo_junction26_delivery_reliability.csv",
    "/api/carbon-footprint/download": "valio_aimo_junction26_carbon_footprint.csv",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "valio")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.getenv("AIMO_API_KEY")
    base = os.getenv("AIMO_API_BASE_URL") or "https://junction26.valioaimo.fi"
    if not key:
        sys.exit("AIMO_API_KEY is missing: copy .env.example to .env and fill it in (the key is in the team chat).")

    args.out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=base, headers={"X-API-Key": key}, follow_redirects=True, timeout=180) as client:
        for path, name in FILES.items():
            target = args.out / name
            with client.stream("GET", path) as r:
                r.raise_for_status()
                with open(target, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
            print(f"{name}: {target.stat().st_size / 1e6:.1f} MB")

    slim = args.out / "catalog_slim.json"
    if slim.exists():
        slim.unlink()  # rebuilt from the fresh files on the next server start
    print(f"Done. Files are in {args.out}")


if __name__ == "__main__":
    main()
