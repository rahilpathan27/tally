"""Export or verify the ledger service's generated OpenAPI contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from services.ledger.api import app

CONTRACT_PATH = Path("contracts/openapi/ledger-v1.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not CONTRACT_PATH.exists() or CONTRACT_PATH.read_text(encoding="utf-8") != rendered:
            print("OpenAPI contract is out of date; run make openapi.", file=sys.stderr)
            return 1
        print("OpenAPI contract is up to date.")
        return 0
    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(rendered, encoding="utf-8")
    print(f"Wrote {CONTRACT_PATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
