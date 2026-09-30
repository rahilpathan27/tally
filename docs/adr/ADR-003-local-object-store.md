# ADR-003: SeaweedFS for the local S3-compatible object store

## Context

The Phase 1 Compose stack initially pinned a MinIO container image, but Docker Hub denied access when the image pull was attempted. Local development still needs an S3-compatible endpoint for later reconciliation and audit-file work.

## Options

1. Keep the unavailable MinIO image reference.
2. Use a locally hosted S3-compatible implementation with a published container image.
3. Remove object storage from the local stack.

## Decision

Choose option 2 and use SeaweedFS's documented single-node S3 server mode, pinned to image tag `3.93` for this scaffold. The endpoint is local-only and does not model S3 Object Lock or cloud durability.

## Consequences

Local development retains an S3 API endpoint without assuming MinIO image availability. SeaweedFS is not MinIO and feature equivalence is not claimed. The current single-node server starts without an S3 identity configuration, so its local S3 API is unauthenticated; Compose binds the endpoint to loopback only. Use synthetic local data and never expose this endpoint to a network. Production cloud storage remains a separate deployment concern.
