# ADR-007: Isolated test-card vault and tokenization

## Status

Accepted for the local simulation.

## Context

The platform needs card references while keeping primary account numbers (PANs) inside a narrow vault boundary. The simulation must not accept real payment data or retain CVV. The vault database needs a separate network and a restricted runtime database role. Detokenization is needed only by the network simulator.

## Decision

- Accept tokenization only for an explicit configured allowlist of published test PANs that also pass Luhn and expiry validation.
- Encrypt each PAN with AES-GCM under a fresh random data key, then wrap that key with a local 256-bit key-encryption key. Bind both encryption layers to the opaque token as authenticated associated data.
- Store ciphertext, BIN prefix, last four digits, and expiry in the dedicated vault database. Return only the opaque token and display metadata from tokenization.
- Reject unknown request fields, including CVV, and replace validation errors with a generic response that does not echo submitted card data.
- Restrict database application privileges and record every detokenization through a database-owned append-only SHA-256 hash chain. Only the `network-simulator` caller identity is permitted by the audit function.
- Attach the vault database only to an internal Compose network, publish its local development port on loopback, and bind the local API to loopback.
- Use a separate local shared credential for the simulator's detokenization call. This substitutes for service mTLS in the local simulation and is not equivalent to certificate-based identity.

## Consequences

This establishes a local security boundary and verifies ciphertext-at-rest, tokenization response/log scrubbing, restricted detokenization, audit immutability, and configured network isolation. The local KEK and shared credential are development substitutes. A real deployment needs managed KMS envelope-key lifecycle, service mTLS, secret rotation, production network policy, and independent audit anchoring. The service is synthetic and must not receive real PAN or CVV.
