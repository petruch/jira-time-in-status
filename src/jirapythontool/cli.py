#!/usr/bin/env python3
from dataclasses import fields
import os
import random
import re
import csv
import argparse
from datetime import datetime

import time as tm
from time import time as now

from typing import Any, Dict, List, Optional, Tuple
import requests


# -----------------------------
# Defaults (override via env or CLI)
# -----------------------------
DEFAULT_BASE_URL = os.getenv("JIRA_BASE_URL", "")
DEFAULT_EMAIL = os.getenv("JIRA_EMAIL", "")  # Jira Cloud: account email
DEFAULT_TOKEN = os.getenv("JIRA_API_TOKEN", "")  # <-- keep token out of code

API_ROOT = "/rest/api/latest"  # IMPORTANT for your org


# -----------------------------
# Utilities
# -----------------------------
def iso_from_jira_dt(dt_str: str) -> str:
    """
    Jira often returns: 2025-12-30T12:34:56.789-0500
    Convert to ISO; if parsing fails, return raw.
    """
    if not dt_str:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(dt_str, fmt).isoformat()
        except Exception:
            continue
    return dt_str


def ensure_issuetype_in_jql(jql: str, issue_type: str) -> str:
    """
    If JQL already contains issuetype/type, leave it.
    Otherwise, inject: AND issuetype = "<issue_type>" (preserving ORDER BY).
    """
    if re.search(r"\b(issuetype|type)\b", jql, flags=re.IGNORECASE):
        return jql

    m = re.search(r"\border\s+by\b", jql, flags=re.IGNORECASE)
    if m:
        left = jql[:m.start()].strip()
        right = jql[m.start():].strip()
        return f'({left}) AND issuetype = "{issue_type}" {right}'
    return f'({jql.strip()}) AND issuetype = "{issue_type}"'


def ensure_since_in_jql(jql: str, since_days: int, since_field: str) -> str:
    """
    Adds a time window if not already present.
    Example injection: AND updated >= -365d
    """
    if since_days <= 0:
        return jql

    # If user already included updated/created constraints, don't add another.
    if re.search(r"\b(updated|created)\b\s*(>=|>|=|<=|<)", jql, flags=re.IGNORECASE):
        return jql

    clause = f'{since_field} >= -{since_days}d'

    m = re.search(r"\border\s+by\b", jql, flags=re.IGNORECASE)
    if m:
        left = jql[:m.start()].strip()
        right = jql[m.start():].strip()
        return f'({left}) AND {clause} {right}'

    return f'({jql.strip()}) AND {clause}'


# -----------------------------
# Jira HTTP
# -----------------------------
def create_session(email: str, token: str) -> requests.Session:
    if not email or not token:
        raise RuntimeError("Missing auth. Set JIRA_EMAIL and JIRA_API_TOKEN (or pass via CLI).")
    s = requests.Session()
    s.auth = (email, token)  # Jira Cloud: email + API token (Basic)
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

        # Handle rate limiting
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                sleep_s = float(retry_after)
            else:
                # exponential backoff with jitter
                sleep_s = base_sleep * (2 ** attempt) + random.uniform(0, 0.5)

            if attempt >= max_retries:
                raise RuntimeError(
                    f"Jira request rate-limited too many times (429).\n"
                    f"URL: {r.url}\n"
                    f"Last retry wait would be: {sleep_s:.2f}s\n"
                    f"Body: {r.text[:1000]!r}\n"
                )

            print(f"[429] Rate limited. Sleeping {sleep_s:.2f}s then retrying... (attempt {attempt+1}/{max_retries})")
            tm.sleep(sleep_s)  # <-- FIX #1
            continue

        # Other errors
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
# Search: GET /search/jql (nextPageToken)
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
# Changelog: GET /issue/{key}/changelog (startAt paging)
# -----------------------------
def fetch_full_changelog(session: requests.Session, base_url: str, issue_key: str, page_size: int = 100) -> List[Dict[str, Any]]:
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


def extract_phase_events(
    issue_key: str,
    changelog_values: List[Dict[str, Any]],
    field_name: str,
    field_id: str = "",
) -> List[Tuple[str, str]]:
    events: List[Tuple[str, str]] = []

    def matches(item: Dict[str, Any]) -> bool:
        if field_id:
            return item.get("fieldId") == field_id
        return item.get("field") == field_name

    sorted_hist = sorted(changelog_values, key=lambda h: h.get("created") or "")

    for h in sorted_hist:
        created = h.get("created", "")
        created_iso = iso_from_jira_dt(created)
        for it in (h.get("items") or []):
            if matches(it):
                to_val = (it.get("toString") or "").strip() or "EMPTY"
                events.append((to_val, created_iso))

    return events


# -----------------------------
# Build matrix
# -----------------------------
def build_matrix(
    issues: List[Dict[str, Any]],
    per_issue_events: Dict[str, List[Tuple[str, str]]],
    mode: str,
) -> Tuple[List[str], List[List[str]]]:
    values_set = set()
    for events in per_issue_events.values():
        for val, _ts in events:
            values_set.add(val)

    def natural_key(s: str):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]
    all_values_order = sorted(values_set, key=natural_key)

    headers = ["issue_key", "summary", "assignee"] + all_values_order
    rows: List[List[str]] = []

    for issue in issues:
        key = issue.get("key", "")
        fields = issue.get("fields") or {}
        summary = (fields.get("summary") or "").replace("\n", " ").strip()

        assignee_obj = fields.get("assignee")
        assignee = ""

        if isinstance(assignee_obj, dict):
            assignee = (assignee_obj.get("displayName") or assignee_obj.get("emailAddress") or "").strip()


        cell_map: Dict[str, str] = {}

        for val, ts in per_issue_events.get(key, []):
            if mode == "first":
                cell_map.setdefault(val, ts)
            elif mode == "last":
                cell_map[val] = ts
            else:
                raise ValueError("mode must be 'first' or 'last'")

        row = [key, summary, assignee] + [cell_map.get(v, "") for v in all_values_order]

        rows.append(row)

    return headers, rows


# -----------------------------
# CLI
# -----------------------------
def main() -> int:
    p = argparse.ArgumentParser(
            description="Build a CSV matrix of change timestamps for a Jira field per issue using /search/jql + /issue/{key}/changelog."
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

    p.add_argument("--field-name", required=False, default=None,
               help='Display name of the field to track (e.g. "PI - PxTA Phase")')
    p.add_argument("--field-id", required=False, default=None,
               help='Field id to track (e.g. "customfield_12345") - preferred if known')


    p.add_argument("--out", default="phase_matrix.csv")
    p.add_argument("--search-page-size", type=int, default=100)
    p.add_argument("--changelog-page-size", type=int, default=100)
    p.add_argument("--mode", choices=["first", "last"], default="first")

    args = p.parse_args()
    if not args.field_name and not args.field_id:
        raise SystemExit('ERROR: You must provide either --field-name or --field-id')
    # Pull creds from Keychain if not passed via CLI/env
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

    issues = search_issues(
        session=session,
        base_url=args.base_url,
        jql=full_jql,
        fields=["summary", "assignee"],
        max_results=args.search_page_size,
        limit=limit,
    )

    if not issues:
        print("No issues found for JQL:", full_jql)
        return 0

    per_issue_events: Dict[str, List[Tuple[str, str]]] = {}
    for i, issue in enumerate(issues, start=1):
        key = issue.get("key")
        if not key:
            continue

        if args.sleep_ms > 0:
            tm.sleep(args.sleep_ms / 1000.0)  # <-- FIX #1

        changelog_values = fetch_full_changelog(
            session=session,
            base_url=args.base_url,
            issue_key=key,
            page_size=args.changelog_page_size,
        )

        events = extract_phase_events(
            issue_key=key,
            changelog_values=changelog_values,
            field_name=args.field_name or "",
            field_id=args.field_id or "",
        )
        per_issue_events[key] = events

        if i % 25 == 0:
            print(f"Processed {i}/{len(issues)} issues...")

    headers, rows = build_matrix(issues, per_issue_events, mode=args.mode)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)

    print(f"Done. Wrote {len(rows)} rows to {args.out}")
    print(f"JQL used: {full_jql}")
    print(f"Field: {args.field_id or args.field_name} | mode={args.mode}")
    return 0
import subprocess

def keychain_get(service: str, account: str = None) -> str:
    """
    Reads a generic password from macOS Keychain.
    service: the -s value you stored (e.g., jira_api_token)
    account: optional -a value (defaults to current user via $USER)
    """
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



if __name__ == "__main__":
    raise SystemExit(main())
