# Security policy

Tally is a synthetic payments simulation. It must never process real card data, real money or
real personal data, and it is not certified against PCI DSS or any regulation.

## Reporting

Report vulnerabilities privately through GitHub's "Report a vulnerability" (security advisories)
on this repository rather than in public issues. Include reproduction steps and the affected
commit. Expect an acknowledgement within a week; this is a personal project with no SLA.

## Scope notes

- Local credentials in `docker-compose.yml` and the Makefile are development-only placeholders.
- Only the published test card numbers allow-listed in the vault are accepted.
- See [docs/security.md](docs/security.md) and [docs/threat-model.md](docs/threat-model.md).
