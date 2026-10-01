# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim AS build
WORKDIR /web
COPY web/console/package.json web/console/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web/console ./
# Inlined at build time by Next: rewrites (in-cluster the Ingress routes /auth and /bff to the
# back office directly) and the browser-visible vault origin and publishable key.
ARG TALLY_BFF_URL=http://tally-backoffice:8040
ARG NEXT_PUBLIC_VAULT_URL=https://vault.tally.example
ARG NEXT_PUBLIC_TALLY_PUBLISHABLE_KEY=pk_test_tally_demo
ARG NEXT_PUBLIC_ASSET_ORIGIN=
ENV NEXT_TELEMETRY_DISABLED=1
ENV TALLY_BFF_URL=${TALLY_BFF_URL}
ENV NEXT_PUBLIC_VAULT_URL=${NEXT_PUBLIC_VAULT_URL}
ENV NEXT_PUBLIC_TALLY_PUBLISHABLE_KEY=${NEXT_PUBLIC_TALLY_PUBLISHABLE_KEY}
ENV NEXT_PUBLIC_ASSET_ORIGIN=${NEXT_PUBLIC_ASSET_ORIGIN}
RUN npm run build

FROM gcr.io/distroless/nodejs22-debian12:nonroot AS runtime
WORKDIR /app
COPY --from=build /web/.next/standalone ./
COPY --from=build /web/.next/static ./.next/static
COPY --from=build /web/public ./public
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0 NEXT_TELEMETRY_DISABLED=1
USER nonroot
EXPOSE 3000
CMD ["server.js"]
