# ADR-014: PostgreSQL + artifact model registry instead of MLflow

## Context

The master plan names MLflow for the model registry. The registry needs are narrow: immutable
versions with metrics and artifact checksums, champion/challenger stages, an evaluation gate,
and dual control on champion promotion. MLflow would add a tracking server, its own database
and artifact store, and a large dependency tree to a local simulation.

## Options

1. MLflow tracking server + model registry.
2. A registry table (`risk_model_versions`) next to the decision log, artifacts on disk or object
   storage with SHA-256 digests, promotion through the shared `maker_checker_requests` table.

## Decision

Option 2. `services/risk/registry.py` registers versions (refusing a different artifact under an
existing version), promotes challengers after the evaluation gate, and requires a second person
to approve champion promotion. Partial unique indexes guarantee one champion and one challenger.

## Consequences

- One fewer stateful service; promotion is auditable in the same database as decisions and labels.
- No experiment-tracking UI; training metrics live in each version's `metadata.json` and the
  registry row. Adopting MLflow later only changes where `ml/train.py` logs and where
  `registry.py` reads artifacts.
