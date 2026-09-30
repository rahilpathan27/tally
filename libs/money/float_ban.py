"""AST guard against float annotations, literals, and constructors in money code."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and type(node.value).__name__ == "float":
            problems.append(f"{path}:{node.lineno}: floating-point literal")
        if isinstance(node, ast.Name) and node.id == "fl" + "oat":
            problems.append(f"{path}:{node.lineno}: float name")
        if isinstance(node, ast.Attribute) and node.attr == "fl" + "oat":
            problems.append(f"{path}:{node.lineno}: float attribute")
    return problems


# Modules that compute or carry money amounts. Keep this list in sync with new money code.
MONEY_MODULES: tuple[str, ...] = (
    "libs/money/*.py",
    "services/ledger/model.py",
    "services/ledger/invariants.py",
    "services/core/fees.py",
    "services/core/settlement.py",
    "services/core/refunds.py",
    "services/core/disputes.py",
    "services/recon/*.py",
)


def main() -> int:
    paths = sorted({path for pattern in MONEY_MODULES for path in Path(".").glob(pattern)})
    problems = [problem for path in paths for problem in violations(path)]
    print("\n".join(problems) if problems else "Money float-ban check passed.")
    return bool(problems)


if __name__ == "__main__":
    sys.exit(main())
