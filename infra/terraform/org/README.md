# Organisation guardrails

`region-guardrail-scp.json` is a Service Control Policy for the AWS Organization that owns the
Tally accounts. It is applied by the organisation administrator (not by these Terraform roots,
which run inside member accounts):

- denies every regional API call outside Mumbai (`ap-south-1`) and Hyderabad (`ap-south-2`),
  except global services and the us-east-1 ACM/WAF calls CloudFront requires;
- prevents anyone in a member account from stopping CloudTrail, GuardDuty or AWS Config.

The Terraform roots add two more layers: a `region` variable restricted to the same two regions
and `allowed_account_ids` on every provider, so a plan pointed at the wrong account fails.
