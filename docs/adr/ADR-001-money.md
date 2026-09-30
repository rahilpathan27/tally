# ADR-001: Integer minor-unit money

## Context

Ledger entries and reconciled statements must agree to the smallest currency unit. Binary floating-point cannot represent most decimal fractions exactly.

## Options

1. Binary floating-point amounts.
2. Decimal major-unit amounts throughout application code.
3. Integer minor-unit amounts with explicit currency and a controlled Decimal conversion boundary.

## Decision

Choose option 3. `Money` is a frozen value object holding an integer and a supported currency. Currency exponents are explicit. `round_to_minor` is the only Decimal conversion function and uses half-even rounding. Splits use largest remainder and preserve the exact total.

## Consequences

Ledger arithmetic is exact and API amounts can be transported as safe JSON integers. Currency support must be extended deliberately. The current currency set is a small development subset, not a complete ISO 4217 registry.
