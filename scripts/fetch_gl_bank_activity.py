"""Pull GL transactions for the Operating and Security Deposit/Trust bank accounts.

This portfolio has one Operating and one Trust bank account per property/LLC
(tagged "(OPS)" / "(TRUST)" in the account name), not a single global pair, so
every matching active account is included. Override matching via
BUILDIUM_OPERATING_ACCOUNT_NAME / BUILDIUM_SD_ACCOUNT_NAME / BUILDIUM_EXCLUDE_ACCOUNT_NAME
env vars (comma-separated, case-insensitive substrings). Accounts whose name
matches the exclude pattern (default "dnu") are dropped even if they'd
otherwise match. Each Buildium bank account is tied to a GL account
(BankAccount.GLAccount.Id); GL transactions are pulled for those GL account
ids over the report period via GET /v1/generalledger/transactions.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
from buildium_client import BuildiumClient
from fields import pick


def match_accounts(accounts, patterns):
    pats = [p.strip().lower() for p in patterns.split(",") if p.strip()]
    return [a for a in accounts if any(p in (pick(a, "Name", "name") or "").lower() for p in pats)]


def fetch():
    client = BuildiumClient()
    start, end = config.report_period()

    accounts = client.get_all("/bankaccounts", params={"bankaccountstatus": "Active"})

    excluded = match_accounts(accounts, config.EXCLUDE_ACCOUNT_MATCH)
    excluded_ids = {pick(a, "Id", "id") for a in excluded}
    candidates = [a for a in accounts if pick(a, "Id", "id") not in excluded_ids]

    ops_matches = match_accounts(candidates, config.OPERATING_ACCOUNT_MATCH)
    sd_matches = match_accounts(candidates, config.SD_ACCOUNT_MATCH)

    warnings = []
    if len(ops_matches) == 0:
        warnings.append(
            "No operating bank accounts matched. Set BUILDIUM_OPERATING_ACCOUNT_NAME to fix."
        )
    if len(sd_matches) == 0:
        warnings.append(
            "No security deposit/trust bank accounts matched. Set BUILDIUM_SD_ACCOUNT_NAME to fix."
        )
    if excluded:
        warnings.append(
            f"Excluded {len(excluded)} account(s) matching '{config.EXCLUDE_ACCOUNT_MATCH}': "
            + ", ".join(pick(a, "Name", "name") for a in excluded)
        )

    tracked = {"operating": ops_matches, "security_deposit": sd_matches}
    all_gl_ids = []
    account_summaries = {}
    for label, matches in tracked.items():
        account_summaries[label] = []
        for a in matches:
            gl = pick(a, "GLAccount", "gl_account", default={}) or {}
            gl_id = pick(gl, "Id", "id")
            if gl_id:
                all_gl_ids.append(gl_id)
            account_summaries[label].append(
                {
                    "bank_account_id": pick(a, "Id", "id"),
                    "name": pick(a, "Name", "name"),
                    "gl_account_id": gl_id,
                    "balance": pick(a, "Balance", "balance"),
                    "is_active": pick(a, "IsActive", "is_active"),
                }
            )

    transactions = []
    if all_gl_ids:
        transactions = client.get_all(
            "/generalledger/transactions",
            params={
                "startdate": start.isoformat(),
                "enddate": end.isoformat(),
                "glaccountids": all_gl_ids,
            },
        )

    report = {
        "report": "General Ledger - Operating & Security Deposit bank accounts",
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "accounts": account_summaries,
        "warnings": warnings,
        "transaction_count": len(transactions),
        "transactions": transactions,
    }

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    return report


if __name__ == "__main__":
    fetch()
