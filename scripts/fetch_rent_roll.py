"""Build a Rent Roll from Rental Units + active Leases.

Buildium's API has no dedicated Rent Roll endpoint; this reconstructs the
standard rent-roll fields (unit, occupancy, tenant, rent, lease dates) from
GET /v1/rentals, GET /v1/rentals/units, GET /v1/rentals/owners and
GET /v1/leases.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from buildium_client import BuildiumClient
from fields import pick


def fetch():
    client = BuildiumClient()

    properties = client.get_all("/rentals")
    prop_by_id = {pick(p, "Id", "id"): p for p in properties}

    owners = client.get_all("/rentals/owners")
    owner_by_property = {}
    for o in owners:
        name = pick(o, "CompanyName", "company_name") or " ".join(
            filter(None, [pick(o, "FirstName", "first_name"), pick(o, "LastName", "last_name")])
        )
        for pid in pick(o, "PropertyIds", "property_ids", default=[]) or []:
            owner_by_property[pid] = name

    units = client.get_all("/rentals/units")
    leases = client.get_all("/leases", params={"leasestatuses": ["Active"]})
    lease_by_unit = {pick(l, "UnitId", "unit_id"): l for l in leases}

    rows = []
    for u in units:
        uid = pick(u, "Id", "id")
        pid = pick(u, "PropertyId", "property_id")
        prop = prop_by_id.get(pid, {})
        lease = lease_by_unit.get(uid)

        tenants = []
        rent = deposit = lease_from = lease_to = None
        status = "Vacant"

        if lease:
            status = "Occupied"
            acct = pick(lease, "AccountDetails", "account_details", default={}) or {}
            rent = pick(acct, "Rent", "rent")
            deposit = pick(acct, "SecurityDeposit", "security_deposit")
            lease_from = pick(lease, "LeaseFromDate", "lease_from_date")
            lease_to = pick(lease, "LeaseToDate", "lease_to_date")
            for t in pick(lease, "CurrentTenants", "current_tenants", default=[]) or []:
                fn = pick(t, "FirstName", "first_name") or ""
                ln = pick(t, "LastName", "last_name") or ""
                full = f"{fn} {ln}".strip()
                if full:
                    tenants.append(full)

        rows.append(
            {
                "property_id": pid,
                "property_name": pick(prop, "Name", "name"),
                "rental_owner": owner_by_property.get(pid),
                "unit_id": uid,
                "unit_number": pick(u, "UnitNumber", "unit_number"),
                "status": status,
                "tenants": "; ".join(tenants),
                "rent": rent,
                "market_rent": pick(u, "MarketRent", "market_rent"),
                "security_deposit": deposit,
                "lease_from": lease_from,
                "lease_to": lease_to,
            }
        )

    return rows


if __name__ == "__main__":
    fetch()
