from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass(slots=True)
class RegistrationWindow:
    start: str
    end: str | None
    label: str
    source_text: str
    reminder_minutes: int = 10

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Promotion:
    id: str
    bank_id: str
    bank_name: str
    title: str
    merchant: str
    categories: list[str]
    start_date: str
    end_date: str | None
    summary: str
    source_url: str
    source_entry_url: str
    observed_at: str
    registration_required: bool = False
    registration_text: str = ""
    terms_sections: dict[str, str] = field(default_factory=dict)
    terms_raw: str = ""
    parent_activity_id: str = ""
    activity_periods: list[dict[str, str]] = field(default_factory=list)
    reward_tiers: list[dict[str, Any]] = field(default_factory=list)
    registration_url: str = ""
    registration_url_kind: str = "unknown"
    registration_windows: list[RegistrationWindow] = field(default_factory=list)
    registration_timing_contracts: list[str] = field(default_factory=list)
    max_reward_percent: float | None = None
    max_reward_amount_twd: int | None = None
    high_return: bool = False
    featured: bool = False
    lifecycle: str = "unknown"
    tags: list[str] = field(default_factory=list)
    official_status: str = "published"
    review_required: bool = False
    needs_review: bool = False
    review_message: str = ""
    source_fingerprint: str = ""
    last_detail_checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["registration_windows"] = [item.to_dict() for item in self.registration_windows]
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Promotion":
        payload = {
            item.name: value[item.name]
            for item in fields(cls)
            if item.name in value
        }
        payload["registration_windows"] = [
            item if isinstance(item, RegistrationWindow) else RegistrationWindow(**item)
            for item in value.get("registration_windows", [])
            if isinstance(item, (dict, RegistrationWindow))
        ]
        return cls(**payload)


@dataclass(slots=True)
class SourceHealth:
    id: str
    bank_name: str
    requested_url: str
    resolved_url: str
    status: str
    activity_count: int
    checked_at: str
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Alert:
    type: str
    bank_name: str
    message: str
    old_url: str = ""
    new_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
