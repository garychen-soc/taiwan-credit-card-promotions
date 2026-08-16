#!/usr/bin/env python3
"""Validate the deployable layered GitHub Pages data set."""

from __future__ import annotations

import gzip
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "docs" / "data"
INDEX_PATH = DATA_ROOT / "promotions.json"
MAX_INDEX_GZIP_BYTES = 100_000


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path}")
    return value


def validate() -> dict[str, int]:
    index = load_json(INDEX_PATH)
    catalog = index.get("catalog")
    if not isinstance(catalog, dict):
        raise ValueError("promotions.json is missing catalog metadata")

    bank_files = catalog.get("bank_files")
    if not isinstance(bank_files, dict) or not bank_files:
        raise ValueError("catalog.bank_files must contain at least one bank shard")

    index_activities = index.get("activities")
    if not isinstance(index_activities, list):
        raise ValueError("promotions.json activities must be a list")
    if any(not item.get("registration_required") for item in index_activities):
        raise ValueError("the first-load index may only contain registration activities")
    expected_index_count = int(catalog.get("registration_index_count") or 0)
    if len(index_activities) != expected_index_count:
        raise ValueError(
            f"registration index count mismatch: {len(index_activities)} != "
            f"{expected_index_count}"
        )

    activity_ids: set[str] = set()
    detail_refs: set[str] = set()
    bank_activity_count = 0
    for bank_id, reference in bank_files.items():
        if not isinstance(reference, str) or not reference.startswith("banks/"):
            raise ValueError(f"invalid bank shard reference for {bank_id}: {reference!r}")
        bank = load_json(DATA_ROOT / reference)
        if bank.get("bank_id") != bank_id:
            raise ValueError(f"bank id mismatch in {reference}")
        activities = bank.get("activities")
        if not isinstance(activities, list):
            raise ValueError(f"activities must be a list in {reference}")
        if int(bank.get("activity_count") or 0) != len(activities):
            raise ValueError(f"activity_count mismatch in {reference}")
        bank_activity_count += len(activities)
        for activity in activities:
            activity_id = str(activity.get("id") or "")
            if not activity_id:
                raise ValueError(f"activity without id in {reference}")
            if activity_id in activity_ids:
                raise ValueError(f"duplicate activity id: {activity_id}")
            activity_ids.add(activity_id)
            detail_ref = activity.get("detail_ref")
            if isinstance(detail_ref, str) and detail_ref:
                detail_refs.add(detail_ref)

    expected_activity_count = int(catalog.get("activity_count") or 0)
    if bank_activity_count != expected_activity_count:
        raise ValueError(
            f"catalog activity count mismatch: {bank_activity_count} != "
            f"{expected_activity_count}"
        )

    for reference in detail_refs:
        if not reference.startswith("activities/"):
            raise ValueError(f"invalid detail reference: {reference!r}")
        detail = load_json(DATA_ROOT / reference)
        expected_id = Path(reference).stem
        if detail.get("activity_id") != expected_id:
            raise ValueError(f"activity id mismatch in {reference}")

    compressed_size = len(gzip.compress(INDEX_PATH.read_bytes()))
    if compressed_size >= MAX_INDEX_GZIP_BYTES:
        raise ValueError(
            f"first-load index is too large: {compressed_size} gzip bytes "
            f"(limit {MAX_INDEX_GZIP_BYTES - 1})"
        )

    return {
        "index_activities": len(index_activities),
        "catalog_activities": bank_activity_count,
        "detail_files": len(detail_refs),
        "index_gzip_bytes": compressed_size,
    }


if __name__ == "__main__":
    result = validate()
    print(
        "Public artifacts valid: "
        f"index={result['index_activities']}, "
        f"catalog={result['catalog_activities']}, "
        f"details={result['detail_files']}, "
        f"index_gzip={result['index_gzip_bytes']} bytes"
    )
