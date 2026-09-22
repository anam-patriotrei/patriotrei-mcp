"""Scan a Balance Sheet, Rent Roll and GL report (as already-fetched dicts)
for common bookkeeping / data-quality issues and return the findings.
"""
import sys
from datetime import date

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def check_balance_sheet(bs, issues):
    if not bs:
        issues.append({"severity": "info", "area": "balance-sheet", "message": "No balance sheet report found."})
        return
    check = bs.get("balance_check_assets_minus_liab_equity_netincome")
    if check is not None and abs(check) > 1.0:
        issues.append(
            {
                "severity": "high",
                "area": "balance-sheet",
                "message": f"Balance sheet does not balance: Assets - (Liabilities+Equity+NetIncome) = {check:.2f}",
            }
        )
    for acct in bs.get("accounts", []):
        if acct.get("balance") is None:
            issues.append(
                {
                    "severity": "low",
                    "area": "balance-sheet",
                    "message": f"GL account '{acct.get('name')}' (#{acct.get('account_number')}) has a null balance.",
                }
            )


def check_rent_roll(rr, issues):
    if not rr:
        issues.append({"severity": "info", "area": "rent-roll", "message": "No rent roll report found."})
        return
    for row in rr:
        status = row.get("status")
        rent = row.get("rent")
        market_rent = row.get("market_rent")
        lease_to = row.get("lease_to")
        unit_label = f"{row.get('property_name')} / Unit {row.get('unit_number')}"

        if status == "Vacant" and rent:
            issues.append(
                {
                    "severity": "medium",
                    "area": "rent-roll",
                    "message": f"{unit_label} is marked Vacant but has a nonzero rent charge ({rent}).",
                }
            )
        if status == "Occupied" and not rent:
            issues.append(
                {
                    "severity": "high",
                    "area": "rent-roll",
                    "message": f"{unit_label} is Occupied but has no rent amount recorded.",
                }
            )
        if status == "Occupied" and rent and market_rent and rent < market_rent * 0.85:
            issues.append(
                {
                    "severity": "low",
                    "area": "rent-roll",
                    "message": f"{unit_label} rent ({rent}) is more than 15% below market rent ({market_rent}).",
                }
            )
        if lease_to and status == "Occupied":
            try:
                if date.fromisoformat(str(lease_to)[:10]) < date.today():
                    issues.append(
                        {
                            "severity": "medium",
                            "area": "rent-roll",
                            "message": f"{unit_label} lease end date ({lease_to}) is in the past but still shows Active/Occupied.",
                        }
                    )
            except ValueError:
                pass


def check_gl(gl, bs, issues):
    if not gl:
        issues.append({"severity": "info", "area": "general-ledger", "message": "No GL report found."})
        return
    for w in gl.get("warnings", []):
        issues.append({"severity": "medium", "area": "general-ledger", "message": w})

    for a in gl.get("accounts", {}).get("operating", []):
        if a.get("balance") is not None and a["balance"] < 0:
            issues.append(
                {
                    "severity": "high",
                    "area": "general-ledger",
                    "message": f"Operating bank account '{a.get('name')}' has a negative balance ({a['balance']}).",
                }
            )

    sd_accounts = gl.get("accounts", {}).get("security_deposit", [])
    if sd_accounts and bs:
        sd_bank_balance = sum(a.get("balance") or 0 for a in sd_accounts)
        sd_liability = next(
            (
                acct["balance"]
                for acct in bs.get("accounts", [])
                if "security deposit" in (acct.get("name") or "").lower()
            ),
            None,
        )
        if sd_liability is not None and abs(sd_bank_balance - sd_liability) > 1.0:
            diff = sd_bank_balance - sd_liability
            issues.append(
                {
                    "severity": "high",
                    "area": "trust-accounting",
                    "message": (
                        f"Security Deposit bank balance ({sd_bank_balance:.2f}) does not match "
                        f"the Security Deposit liability on the balance sheet ({sd_liability:.2f}); "
                        f"difference of {diff:.2f}."
                    ),
                }
            )


def run(bs=None, rr=None, gl=None):
    """Run all checks over already-fetched report dicts (pass None for any
    report that wasn't fetched) and return the findings directly -- nothing
    is read from or written to disk."""
    issues = []
    check_balance_sheet(bs, issues)
    check_rent_roll(rr, issues)
    check_gl(gl, bs, issues)

    issues.sort(key=lambda x: SEVERITY_ORDER.get(x["severity"], 4))
    out = {"generated": date.today().isoformat(), "issue_count": len(issues), "issues": issues}
    print(f"Found {len(issues)} issue(s).", file=sys.stderr)
    return out


if __name__ == "__main__":
    import fetch_balance_sheet
    import fetch_gl_bank_activity
    import fetch_rent_roll

    run(bs=fetch_balance_sheet.fetch(), rr=fetch_rent_roll.fetch(), gl=fetch_gl_bank_activity.fetch())
