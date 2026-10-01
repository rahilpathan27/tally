# Dev: single NAT, single-AZ databases, small nodes, GOVERNANCE object lock, no DR copy.
module "platform" {
  source = "../../modules/platform"
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  environment        = "dev"
  zone_name          = "dev.tally.example"
  single_nat_gateway = true
  db_multi_az        = false
  node_groups = {
    general = { instance_types = ["m7i.large"], min_size = 2, max_size = 4, desired_size = 2, capacity_type = "SPOT" }
  }
  db_instance_classes = { general = "db.t4g.medium", ledger = "db.t4g.medium", vault = "db.t4g.small" }
  cache_node_type     = "cache.t4g.small"
  cache_nodes         = 2
  kafka_instance_type = "kafka.t3.small"
  kafka_brokers       = 3
  object_lock_mode    = "GOVERNANCE"
  deletion_protection = false
  build_images        = true # dev's account builds and signs images; others pull by digest
  monthly_budget_usd  = 900
}

output "platform" {
  value = module.platform
}
