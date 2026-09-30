"""Versioned, data-driven risk rules.

A rule set is JSON: ``{"version": int, "rules": [...], "lists": {...}}``. A rule has an ID,
description, reason code, action (``allow``/``step_up``/``review``/``block``) and a condition:

* ``{"all": [cond, ...]}``, ``{"any": [cond, ...]}``, ``{"not": cond}``
* ``{"fact": "instrument_count_1h", "op": ">=", "value": 10}`` over features and raw facts
* ``{"in_list": "blocklist", "field": "device_id"}`` against the rule set's lists

Rule sets are validated on load so a malformed proposal is rejected before it can be approved.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ACTIONS = ("allow", "step_up", "review", "block")
SEVERITY = {action: rank for rank, action in enumerate(ACTIONS)}
OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}
LIST_FIELDS = ("device_id", "instrument_id", "ip_address", "payee_id", "merchant_id")


class RuleSetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RuleHit:
    rule_id: str
    action: str
    reason_code: str
    description: str


def _validate_condition(condition: Any, depth: int = 0) -> None:
    if depth > 8 or not isinstance(condition, dict) or len(condition) == 0:
        raise RuleSetError("conditions must be non-empty objects nested at most 8 deep")
    if "all" in condition or "any" in condition:
        items = condition.get("all", condition.get("any"))
        if not isinstance(items, list) or not items:
            raise RuleSetError("all/any need a non-empty list")
        for item in items:
            _validate_condition(item, depth + 1)
    elif "not" in condition:
        _validate_condition(condition["not"], depth + 1)
    elif "fact" in condition:
        if condition.get("op") not in OPS or not isinstance(
            condition.get("value"), int | float | str
        ):
            raise RuleSetError(f"invalid comparison {condition}")
    elif "in_list" in condition:
        if condition.get("field") not in LIST_FIELDS:
            raise RuleSetError(f"list field must be one of {LIST_FIELDS}")
    else:
        raise RuleSetError(f"unknown condition {sorted(condition)}")


def validate_rule_set(rule_set: Mapping[str, Any]) -> None:
    rules = rule_set.get("rules")
    if not isinstance(rules, list):
        raise RuleSetError("rules must be a list")
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise RuleSetError("each rule must be an object")
        rule_id = rule.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen:
            raise RuleSetError("rule IDs must be unique non-empty strings")
        seen.add(rule_id)
        if rule.get("action") not in ACTIONS:
            raise RuleSetError(f"{rule_id}: action must be one of {ACTIONS}")
        if not isinstance(rule.get("reason_code"), str):
            raise RuleSetError(f"{rule_id}: reason_code is required")
        _validate_condition(rule.get("condition"))
    lists = rule_set.get("lists", {})
    if not isinstance(lists, dict):
        raise RuleSetError("lists must be an object")
    for name, fields in lists.items():
        if not isinstance(fields, dict) or any(
            field not in LIST_FIELDS or not isinstance(values, list)
            for field, values in fields.items()
        ):
            raise RuleSetError(f"list {name} must map fields to arrays")


class RuleSet:
    def __init__(self, definition: Mapping[str, Any]) -> None:
        validate_rule_set(definition)
        self.version = int(definition.get("version", 0))
        self.rules = [rule for rule in definition["rules"] if rule.get("enabled", True)]
        self.lists = {
            name: {field: set(map(str, values)) for field, values in fields.items()}
            for name, fields in definition.get("lists", {}).items()
        }

    def _holds(self, condition: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
        if "all" in condition:
            return all(self._holds(item, facts) for item in condition["all"])
        if "any" in condition:
            return any(self._holds(item, facts) for item in condition["any"])
        if "not" in condition:
            return not self._holds(condition["not"], facts)
        if "in_list" in condition:
            values = self.lists.get(condition["in_list"], {}).get(condition["field"], set())
            return str(facts.get(condition["field"], "")) in values
        actual = facts.get(condition["fact"])
        if actual is None:
            return False
        expected = condition["value"]
        if isinstance(actual, float) and not math.isfinite(actual):
            return False
        return bool(OPS[condition["op"]](actual, expected))

    def evaluate(self, facts: Mapping[str, Any]) -> list[RuleHit]:
        return [
            RuleHit(
                rule["rule_id"],
                rule["action"],
                rule["reason_code"],
                rule.get("description", ""),
            )
            for rule in self.rules
            if self._holds(rule["condition"], facts)
        ]


DEFAULT_RULES: dict[str, Any] = {
    "version": 1,
    "rules": [
        {
            "rule_id": "R001",
            "description": "Blocklisted device, instrument, IP or payee",
            "reason_code": "BLOCKLISTED",
            "action": "block",
            "condition": {
                "any": [
                    {"in_list": "blocklist", "field": field}
                    for field in ("device_id", "instrument_id", "ip_address", "payee_id")
                ]
            },
        },
        {
            "rule_id": "R002",
            "description": "Card testing: one device used many cards in 24 hours",
            "reason_code": "CARD_TESTING",
            "action": "block",
            "condition": {
                "all": [
                    {"fact": "is_card", "op": "==", "value": 1},
                    {"fact": "device_distinct_instruments_24h", "op": ">=", "value": 5},
                ]
            },
        },
        {
            "rule_id": "R003",
            "description": "More than ten payments from one instrument in an hour",
            "reason_code": "VELOCITY_INSTRUMENT",
            "action": "review",
            "condition": {"fact": "instrument_count_1h", "op": ">=", "value": 10},
        },
        {
            "rule_id": "R004",
            "description": "First payment to a payee that many other payers started using today",
            "reason_code": "PAYEE_FAN_IN",
            "action": "review",
            "condition": {
                "all": [
                    {"fact": "new_payee", "op": "==", "value": 1},
                    {"fact": "payee_distinct_payers_24h", "op": ">=", "value": 8},
                    {"fact": "amount_minor", "op": ">=", "value": 500_000},
                ]
            },
        },
        {
            "rule_id": "R005",
            "description": "High value from a new device in a different country",
            "reason_code": "NEW_DEVICE_GEO_MISMATCH",
            "action": "step_up",
            "condition": {
                "all": [
                    {"fact": "new_device_for_instrument", "op": "==", "value": 1},
                    {"fact": "geo_mismatch", "op": "==", "value": 1},
                    {"fact": "amount_to_instrument_avg", "op": ">=", "value": 2},
                ]
            },
        },
        {
            "rule_id": "R006",
            "description": "Night-time high-value payment (01:00-04:59 IST)",
            "reason_code": "NIGHT_HIGH_VALUE",
            "action": "review",
            "condition": {
                "all": [
                    {"fact": "hour_ist", "op": ">=", "value": 1},
                    {"fact": "hour_ist", "op": "<=", "value": 4},
                    {"fact": "amount_minor", "op": ">=", "value": 2_000_000},
                ]
            },
        },
        {
            "rule_id": "R100",
            "description": "Allowlisted instrument",
            "reason_code": "ALLOWLISTED",
            "action": "allow",
            "condition": {"in_list": "allowlist", "field": "instrument_id"},
        },
    ],
    "lists": {"blocklist": {}, "allowlist": {}},
}
