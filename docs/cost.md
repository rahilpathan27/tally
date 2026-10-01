# Cost estimate (AWS, Mumbai)

**What this is:** an order-of-magnitude monthly estimate for running the Terraform in
`infra/terraform/envs` as sized there, at on-demand list prices for `ap-south-1`. The prices are
approximate, taken from general knowledge of AWS pricing rather than the AWS Pricing API, and
were not checked against a bill: nothing has been deployed (the AWS deployment was dropped on
2026-10-01). Re-price with the AWS Pricing Calculator before relying on any figure. Excludes tax,
support plans and data transfer beyond a small allowance.

## Monthly estimate (USD)

| Component | dev | staging | prod | Notes |
| --- | ---: | ---: | ---: | --- |
| EKS control plane | 73 | 73 | 73 | $0.10/h per cluster |
| EKS nodes | 60 | 465 | 880 | dev 2 × m7i.large Spot; staging 3 × m7i.xlarge; prod 4 × m7i.xlarge + 2 × c7i.xlarge (risk) |
| NAT gateways + processing | 50 | 145 | 180 | one per AZ outside dev |
| RDS PostgreSQL (general, ledger, vault) | 180 | 1,000 | 2,150 | dev single-AZ t4g; staging/prod Multi-AZ r7g; prod adds cross-region backup copies |
| ElastiCache (Valkey) | 50 | 480 | 480 | 2 × t4g.small / 3 × r7g.large |
| MSK (Kafka) | 170 | 515 | 515 | 3 brokers + 200 GB each |
| VPC interface endpoints | 215 | 215 | 215 | 9 endpoints × 3 AZs ($0.011/h each) |
| ALB, WAF (regional + edge), CloudFront, Route 53 | 55 | 70 | 120 | |
| CloudWatch Logs, flow logs, Managed Prometheus | 50 | 100 | 250 | dominated by log volume |
| Security baseline (CloudTrail data events, GuardDuty, Config, Security Hub) | 60 | 100 | 200 | scales with API and data events |
| KMS (11 keys), Secrets Manager | 20 | 25 | 30 | |
| **Total (approx.)** | **~1,000** | **~3,200** | **~5,100** | Terraform budget alarms: 900 / 3,500 / 9,000 |

The largest costs are the databases (three separate instances, Multi-AZ, by design: the ledger and
vault do not share a blast radius with the general database), then compute, then the always-on
managed streaming and cache. VPC interface endpoints are a surprisingly large fixed cost; they
keep secrets, keys and objects off the public internet.

## Unit cost

At prod sizing, infrastructure is about $5,100/month (~₹4.3 lakh at ₹85/USD). At
10 million payments a month that is about $0.0005 (₹0.04) per payment; at 1 million, $0.005
(₹0.43). Capacity, not cost, sets the floor: see the [load-test report](load-test-report.md) for
measured throughput per CPU.

## Levers

| Lever | Saving | Trade-off |
| --- | --- | --- |
| Compute Savings Plan / RDS reserved instances (1 year) | ~30–40% on nodes and databases | commitment |
| Stop dev outside working hours (nodes to 0, RDS stopped) | ~50% of dev | slower mornings |
| dev: one RDS instance with three databases, Redpanda on EKS instead of MSK, NAT instead of interface endpoints | ~$450/month in dev | dev isolation no longer matches prod |
| Graviton (m7g/c7g) nodes | ~15–20% on nodes | multi-arch images (the Dockerfiles already build on arm64) |
| Shorter log retention for non-audit logs (365 → 90 days) | log storage | audit, CloudTrail and WAF logs keep their retention |
| MSK Serverless at low volume | depends on throughput | per-partition and throughput pricing |
