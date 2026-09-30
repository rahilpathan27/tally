# ADR-0001: Phase 1 local foundation

## Context

Tally needs a reproducible local base with distinct data stores for general state, ledger state, and token-vault state, plus cache, event streaming, and S3-compatible object storage. The first delivery should not imply that application services already exist.

## Options

1. Add only empty service directories and defer local infrastructure.
2. Add a Compose development environment and shared Python quality configuration before implementing services.

## Decision

Choose option 2. Use independent PostgreSQL containers for general, ledger, and vault data; Redis 7, Redpanda, and a local S3-compatible object store complete the dependency set. Keep the application service directories as boundaries for later phases.

## Consequences

The foundation can support locally isolated data stores and common quality checks. Compose credentials are explicitly simulation-only. The three PostgreSQL containers increase local resource use. No ledger, vault, or application guarantees exist until their implementations and verification are delivered. The object-store implementation is recorded separately in ADR-003.
