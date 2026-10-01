# Staging: production topology (Multi-AZ, NAT per AZ) at reduced instance sizes.
module "platform" {
  source = "../../modules/platform"
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  environment = "staging"
  zone_name   = "staging.tally.example"
  node_groups = {
    general = { instance_types = ["m7i.xlarge"], min_size = 3, max_size = 6, desired_size = 3 }
  }
  db_instance_classes = { general = "db.r7g.large", ledger = "db.r7g.large", vault = "db.t4g.medium" }
  cache_node_type     = "cache.r7g.large"
  kafka_instance_type = "kafka.m7g.large"
  object_lock_mode    = "GOVERNANCE"
  monthly_budget_usd  = 3500
}

output "platform" {
  value = module.platform
}
