#!/usr/bin/env python3
import os
import random
import re
import csv
import argparse
import subprocess
from datetime import datetime, timezone

import time as tm
from typing import Any, Dict, List, Optional, Tuple
import requests


# -----------------------------
# Defaults (override via env or CLI)
# -----------------------------
DEFAULT_BASE_URL = os.getenv("JIRA_BASE_URL", "")
DEFAULT_EMAIL = os.getenv("JIRA_EMAIL", "")
DEFAULT_TOKEN = os.getenv("JIRA_API_TOKEN", "")

API_ROOT = "/rest/api/latest"


# -----------------------------
# Utilities
# -----------------------------
def parse_jira_dt(dt_str: str) -> Optional[datetime]:
    if not dt_str:
        return None

    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(dt_str, fmt)
        except Exception:
            continue
    return None


def iso_from_jira_dt(dt_str: str) -> str:
    dt = parse_jira_dt(dt_str)
    return dt.isoformat() if dt else dt_str


def format_duration(seconds: float, unit: str) -> str:
    if unit == "seconds":
        return str(int(seconds))
    if unit == "minutes":
        return f"{seconds / 60:.2f}"
    if unit == "hours":
        return f"{seconds / 3600:.2f}"
    if unit == "days":
        return f"{seconds / 86400:.2f}"
    raise ValueError("unit must be one of: seconds, minutes, hours, days")


def ensure_issuetype_in_jql(jql: str, issue_type: str) -> str:
    if re.search(r"\b(issuetype|type)\b", jql, flags=re.IGNORECASE):
        return jql

    m = re.search(r"\border\s+by\b", jql, flags=re.IGNORECASE)
    if m:
        left = jql[:m.start()].strip()
        right = jql[m.start():].strip()
        return f'({left}) AND issuetype = "{issue_type}" {right}'
    return f'({jql.strip()}) AND issuetype = "{issue_type}"'


def ensure_since_in_jql(jql: str, since_days: int, since_field: str) -> str:
    if since_days <= 0:
        return jql

    if re.search(rf"\b{since_field}\b\s*(>=|>|=|<=|<)", jql, flags=re.IGNORECASE):
        return jql

    clause = f"{since_field} >= -{since_days}d"

    m = re.search(r"\border\s+by\b", jql, flags=re.IGNORECASE)
    if m:
        left = jql[:m.start()].strip()
        right = jql[m.start():].strip()
        return f"({left}) AND {clause} {right}"

    return f"({jql.strip()}) AND {clause}"


def parse_statuses_arg(statuses_arg: Optional[List[str]]) -> Optional[List[str]]:
    """
    Accept either:
      --statuses "To Do,In Progress,Done"
    or
      --statuses "To Do" "In Progress" "Done"

    Returns a cleaned ordered list, or None if not provided.
    """
    if not statuses_arg:
        return None

    parsed: List[str] = []
    for item in statuses_arg:
        if item is None:
            continue
        parts = [p.strip() for p in item.split(",")]
        parsed.extend([p for p in parts if p])

    seen = set()
    unique: List[str] = []
    for status in parsed:
        key = status.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(status)

    return unique if unique else None


def parse_extra_fields_arg(extra_fields_arg: Optional[List[str]]) -> List[Tuple[str, str]]:
    """
    Accepts:
      --extra-fields "Story Points=customfield_10016" "Priority=priority"
    or
      --extra-fields "Story Points=customfield_10016,Priority=priority"

    Returns:
      [("Story Points", "customfield_10016"), ("Priority", "priority")]
    """
    if not extra_fields_arg:
        return []

    parsed_items: List[str] = []
    for item in extra_fields_arg:
        if item is None:
            continue
        parts = [p.strip() for p in item.split(",")]
        parsed_items.extend([p for p in parts if p])

    results: List[Tuple[str, str]] = []
    seen = set()

    for item in parsed_items:
        if "=" in item:
            label, field_name = item.split("=", 1)
            label = label.strip()
            field_name = field_name.strip()
        else:
            label = item.strip()
            field_name = item.strip()

        if not label or not field_name:
            continue

        key = field_name.casefold()
        if key not in seen:
            seen.add(key)
            results.append((label, field_name))

    return results


def extract_field_value(value: Any) -> str:
    """
    Best-effort formatting for common Jira field value shapes.
    """
    if value is None:
        return ""

    if isinstance(value, (str, int, float, bool)):
        return str(value)

    if isinstance(value, list):
        parts = [extract_field_value(v) for v in value]
        return ", ".join([p for p in parts if p])

    if isinstance(value, dict):
        for key in ("displayName", "name", "value", "key", "emailAddress"):
            v = value.get(key)
            if v is not None:
                return str(v)
        return str(value)

    return str(value)


# -----------------------------
# Keychain
# -----------------------------
def keychain_get(service: str, account: str = None) -> str:
    if account is None:
        account = os.getenv("USER", "")

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or e.stdout or "").strip()
        raise RuntimeError(f"Could not read '{service}' from Keychain. {msg}")


# -----------------------------
# Jira HTTP
# -----------------------------
def create_session(email: str, token: str) -> requests.Session:
    if not email or not token:
        raise RuntimeError("Missing auth. Set JIRA_EMAIL and JIRA_API_TOKEN (or pass via CLI).")

    s = requests.Session()
    s.auth = (email, token)
    s.headers.update({"Accept": "application/json"})
    return s


def jira_get(
    session: requests.Session,
    base_url: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    max_retries: int = 8,
    base_sleep: float = 1.0,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    params = params or {}

    for attempt in range(max_retries + 1):
        r = session.get(url, params=params, timeout=45)

        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                sleep_s = float(retry_after)
            else:
                sleep_s = base_sleep * (2 ** attempt) + random.uniform(0, 0.5)

            if attempt >= max_retries:
                raise RuntimeError(
                    f"Jira request rate-limited too many times (429).\n"
                    f"URL: {r.url}\n"
                    f"Last retry wait would be: {sleep_s:.2f}s\n"
                    f"Body: {r.text[:1000]!r}\n"
                )

            print(f"[429] Rate limited. Sleeping {sleep_s:.2f}s then retrying... (attempt {attempt+1}/{max_retries})")
            tm.sleep(sleep_s)
            continue

        if r.status_code >= 400:
            raise RuntimeError(
                f"Jira request failed\n"
                f"URL: {r.url}\n"
                f"Status: {r.status_code}\n"
                f"Body (first 1000 chars): {r.text[:1000]!r}\n"
            )

        return r.json()

    raise RuntimeError("Unexpected retry loop exit")


# -----------------------------
# Search
# -----------------------------
def search_issues(
    session: requests.Session,
    base_url: str,
    jql: str,
    fields: List[str],
    max_results: int = 100,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    next_token: Optional[str] = None

    while True:
        params: Dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
            "fields": ",".join(fields),
        }
        if next_token:
            params["nextPageToken"] = next_token

        data = jira_get(session, base_url, f"{API_ROOT}/search/jql", params=params)

        batch = data.get("issues", []) or []
        issues.extend(batch)

        if limit is not None and len(issues) >= limit:
            return issues[:limit]

        if data.get("isLast", False) is True:
            break

        next_token = data.get("nextPageToken")
        if not next_token:
            raise RuntimeError("Search paging expected nextPageToken but none was returned (isLast=false).")

    return issues


# -----------------------------
# Changelog
# -----------------------------
def fetch_full_changelog(
    session: requests.Session,
    base_url: str,
    issue_key: str,
    page_size: int = 100
) -> List[Dict[str, Any]]:
    all_values: List[Dict[str, Any]] = []
    start_at = 0

    while True:
        data = jira_get(
            session,
            base_url,
            f"{API_ROOT}/issue/{issue_key}/changelog",
            params={"startAt": start_at, "maxResults": page_size},
        )

        values = data.get("values", []) or []
        all_values.extend(values)

        total = data.get("total")
        is_last = data.get("isLast")

        if is_last is True:
            break
        if total is not None and (start_at + len(values)) >= int(total):
            break
        if not values:
            break

        start_at += int(data.get("maxResults", page_size) or page_size)

    return all_values


def extract_status_transitions(changelog_values: List[Dict[str, Any]]) -> List[Tuple[str, str, datetime]]:
    transitions: List[Tuple[str, str, datetime]] = []

    sorted_hist = sorted(changelog_values, key=lambda h: h.get("created") or "")

    for h in sorted_hist:
        created_raw = h.get("created", "")
        created_dt = parse_jira_dt(created_raw)
        if not created_dt:
            continue

        for it in (h.get("items") or []):
            if it.get("field") == "status":
                from_status = (it.get("fromString") or "").strip()
                to_status = (it.get("toString") or "").strip()
                transitions.append((from_status, to_status, created_dt))

    return transitions


def calculate_time_in_status(
    issue_created: str,
    current_status: str,
    changelog_values: List[Dict[str, Any]],
    now_dt: datetime,
) -> Dict[str, float]:

    durations: Dict[str, float] = {}

    created_dt = parse_jira_dt(issue_created)
    if not created_dt:
        return durations

    transitions = extract_status_transitions(changelog_values)

    if not transitions:
        seconds = max((now_dt - created_dt).total_seconds(), 0.0)
        durations[current_status] = durations.get(current_status, 0.0) + seconds
        return durations

    first_from = transitions[0][0].strip() if transitions[0][0] else current_status
    prev_status = first_from
    prev_time = created_dt

    for from_status, to_status, changed_at in transitions:
        seconds = max((changed_at - prev_time).total_seconds(), 0.0)
        durations[prev_status] = durations.get(prev_status, 0.0) + seconds

        prev_status = to_status or prev_status
        prev_time = changed_at

    seconds = max((now_dt - prev_time).total_seconds(), 0.0)
    durations[prev_status] = durations.get(prev_status, 0.0) + seconds

    return durations


# -----------------------------
# Build matrix
# -----------------------------
def build_matrix(
    issues: List[Dict[str, Any]],
    per_issue_durations: Dict[str, Dict[str, float]],
    unit: str,
    selected_statuses: Optional[List[str]] = None,
    extra_fields: Optional[List[Tuple[str, str]]] = None,
) -> Tuple[List[str], List[List[str]]]:
    statuses_set = set()

    for duration_map in per_issue_durations.values():
        statuses_set.update(duration_map.keys())

    def natural_key(s: str):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]

    discovered_statuses = sorted(statuses_set, key=natural_key)

    if selected_statuses:
        discovered_lookup = {s.casefold(): s for s in discovered_statuses}
        all_statuses: List[str] = []

        for requested in selected_statuses:
            matched = discovered_lookup.get(requested.casefold())
            all_statuses.append(matched if matched else requested)
    else:
        all_statuses = discovered_statuses

    extra_fields = extra_fields or []
    extra_labels = [label for label, _ in extra_fields]

    headers = ["issue_key", "summary", "assignee"] + extra_labels + all_statuses
    rows: List[List[str]] = []

    for issue in issues:
        key = issue.get("key", "")
        fields = issue.get("fields") or {}
        summary = (fields.get("summary") or "").replace("\n", " ").strip()

        assignee_obj = fields.get("assignee")
        assignee = ""
        if isinstance(assignee_obj, dict):
            assignee = (assignee_obj.get("displayName") or assignee_obj.get("emailAddress") or "").strip()

        extra_values = []
        for _, field_name in extra_fields:
            extra_values.append(extract_field_value(fields.get(field_name)))

        duration_map = per_issue_durations.get(key, {})
        duration_lookup = {k.casefold(): v for k, v in duration_map.items()}

        row = [key, summary, assignee] + extra_values + [
            format_duration(duration_lookup.get(status.casefold(), 0.0), unit) for status in all_statuses
        ]
        rows.append(row)

    return headers, rows


# -----------------------------
# CLI
# -----------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="Build a CSV matrix of total time spent in each Jira status per issue."
    )

    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--email", default=DEFAULT_EMAIL)
    p.add_argument("--token", default=DEFAULT_TOKEN)

    p.add_argument("--project", default="PARCHKB")
    p.add_argument("--issue-type", default="Feature")
    p.add_argument("--jql", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--since-days", type=int, default=365)
    p.add_argument("--since-field", choices=["updated", "created"], default="updated")
    p.add_argument("--sleep-ms", type=int, default=150)

    p.add_argument("--out", default="status_time_matrix.csv")
    p.add_argument("--search-page-size", type=int, default=100)
    p.add_argument("--changelog-page-size", type=int, default=100)
    p.add_argument("--time-unit", choices=["seconds", "minutes", "hours", "days"], default="hours")

    p.add_argument(
        "--statuses",
        nargs="*",
        default=None,
        help=(
            "Optional list of statuses to include and order in the output. "
            'Examples: --statuses "To Do,In Progress,Done" '
            'or --statuses "To Do" "In Progress" "Done". '
            "If omitted, all discovered statuses are included."
        ),
    )

    p.add_argument(
        "--extra-fields",
        nargs="*",
        default=None,
        help=(
            "Optional Jira fields to include before status columns. "
            'Use label=fieldName, e.g. --extra-fields "Story Points=customfield_10016" '
            '"Priority=priority". '
            "If label is omitted, the field name is used as the column header."
        ),
    )

    args = p.parse_args()

    selected_statuses = parse_statuses_arg(args.statuses)
    extra_fields = parse_extra_fields_arg(args.extra_fields)

    if not args.base_url:
        args.base_url = keychain_get("jira_base_url")
    if not args.email:
        args.email = keychain_get("jira_email")
    if not args.token:
        args.token = keychain_get("jira_api_token")

    session = create_session(args.email, args.token)

    base_jql = args.jql.strip() or f'project = "{args.project}" ORDER BY updated DESC'
    base_jql = ensure_since_in_jql(base_jql, args.since_days, args.since_field)
    full_jql = ensure_issuetype_in_jql(base_jql, args.issue_type)

    if not re.search(r"\border\s+by\b", full_jql, flags=re.IGNORECASE):
        full_jql = full_jql.strip() + " ORDER BY updated DESC"

    limit = args.limit if args.limit and args.limit > 0 else None

    search_fields = ["summary", "assignee", "created", "status"]
    for _, field_name in extra_fields:
        if field_name not in search_fields:
            search_fields.append(field_name)

    issues = search_issues(
        session=session,
        base_url=args.base_url,
        jql=full_jql,
        fields=search_fields,
        max_results=args.search_page_size,
        limit=limit,
    )

    if not issues:
        print("No issues found for JQL:", full_jql)
        return 0

    now_dt = datetime.now(timezone.utc)
    per_issue_durations: Dict[str, Dict[str, float]] = {}

    for i, issue in enumerate(issues, start=1):
        key = issue.get("key")
        if not key:
            continue

        if args.sleep_ms > 0:
            tm.sleep(args.sleep_ms / 1000.0)

        changelog_values = fetch_full_changelog(
            session=session,
            base_url=args.base_url,
            issue_key=key,
            page_size=args.changelog_page_size,
        )

        fields = issue.get("fields") or {}
        issue_created = fields.get("created", "")
        status_obj = fields.get("status") or {}
        current_status = (status_obj.get("name") or "").strip()

        per_issue_durations[key] = calculate_time_in_status(
            issue_created=issue_created,
            current_status=current_status,
            changelog_values=changelog_values,
            now_dt=now_dt,
        )

        if i % 25 == 0:
            print(f"Processed {i}/{len(issues)} issues...")

    headers, rows = build_matrix(
        issues=issues,
        per_issue_durations=per_issue_durations,
        unit=args.time_unit,
        selected_statuses=selected_statuses,
        extra_fields=extra_fields,
    )

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)

    print(f"Done. Wrote {len(rows)} rows to {args.out}")
    print(f"JQL used: {full_jql}")
    print(f"Time unit: {args.time_unit}")
    if selected_statuses:
        print(f"Statuses selected: {selected_statuses}")
    else:
        print("Statuses selected: all discovered statuses")

    if extra_fields:
        print(f"Extra fields selected: {[label for label, _ in extra_fields]}")
    else:
        print("Extra fields selected: none")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())