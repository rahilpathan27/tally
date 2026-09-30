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


def main() -> int:
    root = Path("libs/money")
    problems = [problem for path in root.glob("*.py") for problem in violations(path)]
    print("\n".join(problems) if problems else "Money float-ban check passed.")
    return bool(problems)


if __name__ == "__main__":
    sys.exit(main())
