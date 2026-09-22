import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import detect_issues
import fetch_balance_sheet
import fetch_gl_bank_activity
import fetch_rent_roll


def main():
    print("Fetching Balance Sheet...", file=sys.stderr)
    bs = fetch_balance_sheet.fetch()
    print("Fetching Rent Roll...", file=sys.stderr)
    rr = fetch_rent_roll.fetch()
    print("Fetching GL (Operating & Security Deposit)...", file=sys.stderr)
    gl = fetch_gl_bank_activity.fetch()
    print("Detecting issues...", file=sys.stderr)
    issues = detect_issues.run(bs=bs, rr=rr, gl=gl)

    result = {"balance_sheet": bs, "rent_roll": rr, "gl": gl, "issues": issues}
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
