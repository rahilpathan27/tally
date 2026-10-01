# Prod-shaped environment of the simulation: Multi-AZ everything, COMPLIANCE object lock,
# RDS backups replicated to Hyderabad (ap-south-2) for regional DR.
module "platform" {
  source = "../../modules/platform"
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  environment = "prod"
  zone_name   = "tally.example"
  node_groups = {
    general = { instance_types = ["m7i.xlarge", "m6i.xlarge"], min_size = 3, max_size = 12, desired_size = 4 }
    risk    = { instance_types = ["c7i.xlarge"], min_size = 2, max_size = 8, desired_size = 2 }
  }
  db_instance_classes      = { general = "db.r7g.xlarge", ledger = "db.r7g.xlarge", vault = "db.r7g.large" }
  db_dr_backup_replication = true
  cache_node_type          = "cache.r7g.large"
  kafka_instance_type      = "kafka.m7g.large"
  object_lock_mode         = "COMPLIANCE"
  monthly_budget_usd       = 9000
}

output "platform" {
  value = module.platform
}
