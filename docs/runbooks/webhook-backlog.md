# Runbook: webhook or outbox backlog (`WebhookBacklog`, `OutboxBacklog`)

1. Outbox backlog: the relay is not publishing. Check Kafka/Redpanda health and core logs;
   events stay in `core_outbox` and are published at least once when the relay recovers.
2. Webhook backlog: Security Events dashboard `result` breakdown. `dead` deliveries to one
   endpoint usually mean the merchant's endpoint is down; they can redeliver from the dashboard.
3. `blocked_destination`: the endpoint now resolves to a private address (possible DNS rebinding
   or misconfiguration); do not bypass the SSRF check.
