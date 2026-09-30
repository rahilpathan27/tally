# ADR-018: One Next.js console with role-based areas

## Context

The plan allows three apps or one app with role-based areas. Merchant users, ops staff, risk
analysts and approvers share authentication, the design system, the API client and many views
(a payment detail is the same for a merchant and for ops, with different actions).

## Decision

- One Next.js 16 (App Router) app in `web/console` with areas `/merchant/*`, `/ops/*`, and the
  public `/checkout` and `/payer` pages. Navigation is filtered by the session's roles; every
  action is still authorised by the back office (the UI hides, the server enforces).
- The console calls the back office same-origin through Next rewrites (`/auth`, `/bff`), so
  session cookies stay `SameSite=Strict`, no CORS is needed, and CSRF uses the double-submit
  header. A proxy sets a per-request CSP nonce and security headers.
- Responses are validated at runtime with Zod: the BFF returns loosely typed JSON, so runtime
  validation catches contract drift (it found `numeric` sums serialised as strings). A generated
  OpenAPI client was not adopted for the BFF for this reason; the merchant and ledger APIs keep
  generated OpenAPI contracts.
- Money is formatted from integer minor units through an exact decimal string and parsed from
  input without floating point (`src/lib/money.ts`, unit-tested). Mutations reuse one idempotency
  key per user action.
- Components are a small in-house set in the shadcn style (Tailwind, native `<dialog>` for focus
  management) rather than the shadcn generator.
- Hosted checkout posts card data from the browser straight to the vault with a publishable key;
  the demo store's server (Next route handlers) holds the merchant HMAC secret and only sees tokens.

## Consequences

Role areas deploy together. The payer "phone" and the demo store are simulations that read
local dev-stack credentials. No Lighthouse budget is enforced yet.
