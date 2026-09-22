"""Pull an interim Balance Sheet from Buildium GL account balances.

Buildium's public API has no dedicated "Balance Sheet report" endpoint.
This reconstructs one from GET /v1/glaccounts (account types) and
GET /v1/glaccounts/balances (as-of balances). That balances endpoint only
takes an as-of date, not a start date, so Income/Expense totals it returns
are life-to-date (since each account's first transaction), not just the
report period. That's rolled into Equity as "Net Income (since inception)"
for the balance check below -- which is the correct treatment as long as
the books have never been formally closed to Retained Earnings, since
Buildium doesn't expose a fiscal-year-close date via API to verify that.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
from buildium_client import BuildiumClient
from fields import pick


def fetch():
    client = BuildiumClient()
    _, end = config.report_period()

    gl_accounts = client.get_all("/glaccounts")
    accounts_by_id = {pick(a, "Id", "id"): a for a in gl_accounts}

    balances = client.get_all(
        "/glaccounts/balances",
        params={"accountingbasis": config.ACCOUNTING_BASIS, "asofdate": end.isoformat()},
    )

    rows = []
    totals = {"Asset": 0.0, "Liability": 0.0, "Equity": 0.0, "Income": 0.0, "Expense": 0.0}
    for b in balances:
        gl = pick(b, "GLAccount", "gl_account", default={}) or {}
        acc_id = pick(gl, "Id", "id")
        acct = accounts_by_id.get(acc_id, gl)
        acct_type = pick(acct, "Type", "type", default="Unknown")
        total_balance = pick(b, "TotalBalance", "total_balance", default=0.0) or 0.0

        rows.append(
            {
                "gl_account_id": acc_id,
                "account_number": pick(acct, "AccountNumber", "account_number"),
                "name": pick(acct, "Name", "name"),
                "type": acct_type,
                "sub_type": pick(acct, "SubType", "sub_type"),
                "balance": total_balance,
            }
        )
        if acct_type in totals:
            totals[acct_type] += total_balance

    net_income_since_inception = totals["Income"] - totals["Expense"]
    balance_check = totals["Asset"] - (totals["Liability"] + totals["Equity"] + net_income_since_inception)

    report = {
        "report": "Balance Sheet (reconstructed from GL account balances)",
        "accounting_basis": config.ACCOUNTING_BASIS,
        "as_of_date": end.isoformat(),
        "accounts": rows,
        "totals": totals,
        "net_income_since_inception": net_income_since_inception,
        "balance_check_assets_minus_liab_equity_netincome": balance_check,
        "note": (
            "Buildium's API has no dedicated Balance Sheet endpoint; this is "
            "reconstructed from /v1/glaccounts + /v1/glaccounts/balances. "
            "Income/Expense totals are life-to-date (the balances endpoint only "
            "takes an as-of date, not a start date) and rolled into Equity as "
            "Net Income since inception for the balance check -- correct as long "
            "as the books have never been closed to Retained Earnings."
        ),
    }

    return report


if __name__ == "__main__":
    fetch()
