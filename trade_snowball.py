#!/usr/bin/env python3
"""Seed-supplier to buyer-lead snowball pipeline for the CrossBorder Cube API."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import requests


class Provider(Protocol):
    def call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        ...


class CrossBorderCubeProvider:
    def __init__(self, api_key: str, base_url: str, timeout: int = 60) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.trace: list[dict[str, Any]] = []

    def call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url}{endpoint}",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        self.trace.append(
            {
                "endpoint": endpoint,
                "request": redact_request(payload),
                "response_meta": {
                    "code": body.get("code"),
                    "msg": body.get("msg"),
                    "result_count": len((body.get("data") or {}).get("list") or []),
                    "has_cursor": bool((body.get("data") or {}).get("cursor")),
                },
            }
        )
        if body.get("code") != 0:
            raise RuntimeError(f"{endpoint}: {body.get('msg', 'business error')}")
        return body


class ReplayProvider:
    def __init__(self, trace: list[dict[str, Any]]) -> None:
        self.events = list(trace)
        self.index = 0
        self.trace: list[dict[str, Any]] = []

    def call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.index >= len(self.events):
            raise RuntimeError(f"Replay trace exhausted before {endpoint}")
        event = self.events[self.index]
        self.index += 1
        if event["endpoint"] != endpoint:
            raise RuntimeError(
                f"Replay mismatch: expected {event['endpoint']}, received {endpoint}"
            )
        response = event["response"]
        self.trace.append(
            {
                "endpoint": endpoint,
                "request": redact_request(payload),
                "response_meta": {
                    "code": response.get("code"),
                    "msg": response.get("msg"),
                    "result_count": len(
                        (response.get("data") or {}).get("list") or []
                    ),
                    "has_cursor": bool(
                        (response.get("data") or {}).get("cursor")
                    ),
                },
            }
        )
        return response


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def redact_request(payload: dict[str, Any]) -> dict[str, Any]:
    safe_fields = {
        "companyNames",
        "countryCodes",
        "companyStatusIds",
        "sourceNames",
        "companyType",
        "seller",
        "sellerCountryCodes",
        "buyerCountryCodes",
        "sortingField",
        "sortingDirection",
        "cursor",
    }
    return {key: value for key, value in payload.items() if key in safe_fields}


def normalize_name(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", value)
    suffixes = r"\b(inc|llc|ltd|limited|gmbh|co|company|corp|corporation|pty)\b"
    value = re.sub(suffixes, " ", value)
    return " ".join(value.split())


def text_contains(text: str, keyword: str) -> bool:
    text = text.casefold()
    keyword = keyword.casefold().strip()
    if not keyword:
        return False
    if re.search(r"[\u3400-\u9fff]", keyword):
        return keyword in text
    pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
    return bool(re.search(pattern, text))


def product_match(text: str, filters: dict[str, Any]) -> dict[str, Any]:
    include = [
        keyword
        for keyword in filters.get("include_keywords", [])
        if text_contains(text, keyword)
    ]
    exclude = [
        keyword
        for keyword in filters.get("exclude_keywords", [])
        if text_contains(text, keyword)
    ]
    if exclude:
        return {
            "matched": False,
            "reason": "excluded_keyword",
            "include_hits": include,
            "exclude_hits": exclude,
        }
    if include:
        return {
            "matched": True,
            "reason": "included_keyword",
            "include_hits": include,
            "exclude_hits": [],
        }
    return {
        "matched": False,
        "reason": "no_product_evidence",
        "include_hits": [],
        "exclude_hits": [],
    }


def resolve_seed(
    provider: Provider,
    seed: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    response = provider.call(
        "/search/company/list",
        {
            "companyNames": seed["company_names"],
            "countryCodes": seed.get("country_codes", ["CN"]),
            "companyStatusIds": [1],
            "sort": 0,
            "isExact": False,
            "sourceNames": ["customs", "depth_company", "linkedin"],
        },
    )
    candidates = (response.get("data") or {}).get("list") or []
    if not candidates:
        return None

    target_names = {normalize_name(name) for name in seed["company_names"]}

    def candidate_score(candidate: dict[str, Any]) -> tuple[int, int, float]:
        name_match = int(normalize_name(candidate.get("company_name", "")) in target_names)
        product_text = " ".join(candidate.get("products") or [])
        product = product_match(product_text, config["filters"])
        active = int(candidate.get("status") == 1)
        return name_match, int(product["matched"]) + active, float(candidate.get("es_score") or 0)

    chosen = max(candidates, key=candidate_score)
    product_text = " ".join(chosen.get("products") or [])
    if not product_match(product_text, config["filters"])["matched"]:
        return None
    return {
        "label": seed["label"],
        "company_name": chosen.get("company_name", ""),
        "pid": chosen.get("pid", ""),
        "products": chosen.get("products") or [],
    }


def timestamp_to_date(value: Any) -> str:
    if not value:
        return ""
    try:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def normalize_buyer(record: dict[str, Any], seed_label: str) -> dict[str, Any]:
    country_info = record.get("countryInfo") or {}
    return {
        "company_id": str(
            record.get("companyId")
            or record.get("buyerCompanyId")
            or record.get("pid")
            or ""
        ),
        "company_name": (
            record.get("name")
            or record.get("company_name")
            or record.get("buyer")
            or ""
        ).strip(),
        "country": country_info.get("code_iso2")
        or record.get("country_code")
        or record.get("buyerCountryCode")
        or "",
        "scope": (record.get("scope") or "").strip(),
        "product_desc": (record.get("productDesc") or "").strip(),
        "trade_count": int(
            record.get("tradeMatchTotal")
            or record.get("trade_count")
            or record.get("tradeTotal")
            or 0
        ),
        "total_trade_count": int(record.get("tradeTotal") or 0),
        "latest_trade_date": timestamp_to_date(record.get("latestTradeDate")),
        "email_count": int(
            record.get("emailNum")
            or record.get("email_num")
            or record.get("buyerEmailNum")
            or 0
        ),
        "website_count": int(
            record.get("websiteNum")
            or record.get("website_num")
            or record.get("buyerWebsiteNum")
            or 0
        ),
        "source_seeds": [seed_label],
    }


def expand_buyers(
    provider: Provider,
    seed: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    pipeline = config.get("pipeline", {})
    max_pages = int(pipeline.get("max_pages_per_seed", 2))
    sleep_seconds = float(pipeline.get("sleep_seconds", 0.5))
    cursor: str | None = None
    leads: list[dict[str, Any]] = []

    for _ in range(max_pages):
        payload: dict[str, Any] = {
            "companyType": 2,
            "seller": seed["company_name"],
            "sellerCountryCodes": ["CN"],
            "buyerCountryCodes": config["filters"].get("buyer_countries", []),
            "isExact": False,
            "sortingField": "tradeCount",
            "sortingDirection": "desc",
            "existEmail": 0,
        }
        if cursor:
            payload["cursor"] = cursor
        response = provider.call("/customs/company/list", payload)
        data = response.get("data") or {}
        records = data.get("list") or []
        leads.extend(normalize_buyer(record, seed["label"]) for record in records)
        cursor = data.get("cursor")
        if not cursor or not records:
            break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return leads


def merge_leads(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for lead in leads:
        key = lead.get("company_id") or normalize_name(lead.get("company_name", ""))
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(lead)
            merged[key]["source_seeds"] = sorted(set(lead.get("source_seeds", [])))
            continue

        current = merged[key]
        current["source_seeds"] = sorted(
            set(current.get("source_seeds", [])) | set(lead.get("source_seeds", []))
        )
        for field in ("trade_count", "total_trade_count", "email_count", "website_count"):
            current[field] = max(int(current.get(field, 0)), int(lead.get(field, 0)))
        for field in ("scope", "product_desc", "latest_trade_date", "country"):
            if len(str(lead.get(field, ""))) > len(str(current.get(field, ""))):
                current[field] = lead[field]
    return list(merged.values())


def score_lead(lead: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    filters = config["filters"]
    pipeline = config["pipeline"]
    evidence_text = f"{lead.get('scope', '')} {lead.get('product_desc', '')}"
    product = product_match(evidence_text, filters)
    score = 0
    reasons: list[str] = []

    if product["matched"]:
        score += 45
        reasons.append("product_match")
    elif product["reason"] == "excluded_keyword":
        score -= 60
        reasons.append("cross_industry_collision")
    else:
        reasons.append("missing_product_evidence")

    trade_count = int(lead.get("trade_count", 0))
    minimum = int(filters.get("min_trade_count", 1))
    if trade_count >= minimum:
        score += min(20, 8 + trade_count)
        reasons.append("trade_evidence")

    if lead.get("latest_trade_date"):
        score += 8
        reasons.append("dated_trade_evidence")
    if int(lead.get("website_count", 0)) > 0:
        score += 8
        reasons.append("website_signal")
    if int(lead.get("email_count", 0)) > 0:
        score += 7
        reasons.append("email_signal")
    if len(lead.get("source_seeds", [])) > 1:
        score += 12
        reasons.append("cross_seed_validation")

    if score >= int(pipeline["qualified_threshold"]):
        status = "qualified"
    elif score >= int(pipeline["review_threshold"]):
        status = "review"
    else:
        status = "rejected"

    result = dict(lead)
    result.update(
        {
            "score": score,
            "status": status,
            "reason_codes": reasons,
            "include_hits": product["include_hits"],
            "exclude_hits": product["exclude_hits"],
        }
    )
    return result


def run_pipeline(
    provider: Provider,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved_seeds = []
    raw_leads = []
    for seed in config["seeds"]:
        resolved = resolve_seed(provider, seed, config)
        if not resolved:
            print(f"[skip] Could not resolve seed: {seed['label']}")
            continue
        resolved_seeds.append(resolved)
        print(f"[seed] {seed['label']} -> {resolved['company_name']}")
        expanded = expand_buyers(provider, resolved, config)
        print(f"[expand] {seed['label']} -> {len(expanded)} buyer records")
        raw_leads.extend(expanded)

    scored = [
        score_lead(lead, config)
        for lead in merge_leads(raw_leads)
    ]
    scored.sort(key=lambda item: (-item["score"], item["company_name"].casefold()))
    return resolved_seeds, scored


def csv_value(value: Any) -> Any:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return value


def export_outputs(
    output_dir: Path,
    config: dict[str, Any],
    provider: Any,
    resolved_seeds: list[dict[str, Any]],
    leads: list[dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    columns = [
        "company_name",
        "country",
        "status",
        "score",
        "trade_count",
        "latest_trade_date",
        "source_seeds",
        "scope",
        "product_desc",
        "email_count",
        "website_count",
        "reason_codes",
        "include_hits",
        "exclude_hits",
    ]
    with (output_dir / "leads.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for lead in leads:
            writer.writerow({column: csv_value(lead.get(column, "")) for column in columns})

    counts = {
        status: sum(lead["status"] == status for lead in leads)
        for status in ("qualified", "review", "rejected")
    }
    summary = {
        "project": config["project"]["name"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "resolved_seed_count": len(resolved_seeds),
        "unique_lead_count": len(leads),
        "status_counts": counts,
    }
    write_json(output_dir / "summary.json", summary)
    write_json(output_dir / "api_trace.json", provider.trace)

    report = [
        f"# {config['project']['name']}",
        "",
        f"- Resolved seeds: {len(resolved_seeds)}",
        f"- Unique leads: {len(leads)}",
        f"- Qualified: {counts['qualified']}",
        f"- Review: {counts['review']}",
        f"- Rejected: {counts['rejected']}",
        "",
        "## Top leads",
        "",
        "| Company | Country | Score | Status | Sources |",
        "|---|---|---:|---|---|",
    ]
    for lead in leads[:10]:
        report.append(
            f"| {lead['company_name']} | {lead['country']} | {lead['score']} | "
            f"{lead['status']} | {', '.join(lead['source_seeds'])} |"
        )
    report.extend(
        [
            "",
            "Scores rank evidence. They do not estimate conversion probability. "
            "Qualified leads still require human verification.",
            "",
        ]
    )
    (output_dir / "report.md").write_text("\n".join(report), encoding="utf-8")

    print(f"\nSaved outputs to {output_dir}")
    print(
        f"qualified={counts['qualified']} review={counts['review']} "
        f"rejected={counts['rejected']}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Call the live API.")
    run_parser.add_argument("--config", type=Path, required=True)
    run_parser.add_argument("--output-dir", type=Path, required=True)

    replay_parser = subparsers.add_parser(
        "replay", help="Run against a synthetic recorded trace."
    )
    replay_parser.add_argument("--config", type=Path, required=True)
    replay_parser.add_argument("--trace", type=Path, required=True)
    replay_parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = read_json(args.config)

    if args.command == "run":
        api_key = os.environ.get("TRADE_API_KEY")
        if not api_key:
            print("TRADE_API_KEY is not set.", file=sys.stderr)
            return 2
        base_url = os.environ.get(
            "TRADE_API_BASE", config["provider"]["base_url"]
        )
        provider: Any = CrossBorderCubeProvider(api_key, base_url)
    else:
        provider = ReplayProvider(read_json(args.trace))

    resolved_seeds, leads = run_pipeline(provider, config)
    export_outputs(args.output_dir, config, provider, resolved_seeds, leads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

