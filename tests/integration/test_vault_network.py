from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def test_vault_database_is_on_an_internal_network_and_loopback_only() -> None:
    if not os.environ.get("VAULT_DATABASE_URL"):
        pytest.skip("set VAULT_DATABASE_URL to run vault network checks")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    )
    compose = json.loads(result.stdout)
    assert compose["networks"]["vault-isolated"]["internal"] is True
    vault = compose["services"]["postgres-vault"]
    assert set(vault["networks"]) == {"vault-isolated", "vault-host"}
    # No other container may share a network with the vault database.
    for name, service in compose["services"].items():
        if name != "postgres-vault":
            assert not set(service.get("networks", {})) & {"vault-isolated", "vault-host"}, name
    assert all(port.get("host_ip") == "127.0.0.1" for port in vault["ports"])
