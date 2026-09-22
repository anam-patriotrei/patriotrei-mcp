import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()

ACCOUNTING_BASIS = os.environ.get("BUILDIUM_ACCOUNTING_BASIS", "Accrual")
# This portfolio has one Operating and one Trust/Security-Deposit bank account
# PER property/LLC (e.g. "Patriot REI (OPS) - Walnut Village", "Patriot REI
# (TRUST) - FF&R"), not a single global pair -- so these match every account
# tagged that way, and every match is pulled.
OPERATING_ACCOUNT_MATCH = os.environ.get("BUILDIUM_OPERATING_ACCOUNT_NAME", "(ops)")
SD_ACCOUNT_MATCH = os.environ.get("BUILDIUM_SD_ACCOUNT_NAME", "(trust),security deposit")
EXCLUDE_ACCOUNT_MATCH = os.environ.get("BUILDIUM_EXCLUDE_ACCOUNT_NAME", "dnu")


def report_period():
    """Month-to-date: first of the current month through today."""
    today = date.today()
    start = today.replace(day=1)
    return start, today
