"""WHO CAN DO WHAT -- the one file to edit when someone needs access.

People sign in to the Claude connector with Google, so the server knows their
email. Change a line here, merge to main, and Render redeploys it.

  READ:  anyone whose email is in READ_DOMAINS or READ_EMAILS can sign in and
         use the read tools (reports, buildium_get).
  WRITE: only emails in WRITE_PERMISSIONS can use buildium_write, and only with
         the methods listed. Everyone else is read-only.

Removing someone takes effect at their next request (tokens are re-checked).

Integrations that log in with an x-api-key (MCP_API_KEYS on Render) are known
by their key name instead of an email; they can read, and can write only if
their key name is listed in WRITE_PERMISSIONS.
"""

READ_DOMAINS = {
    "patriotrei.net",
    "villagecapitalpartners.com",
}

READ_EMAILS = {
    "traversecitytownhomes@gmail.com",  # Ashley, Holiday Village Townhomes PM
}

WRITE_PERMISSIONS: dict[str, set[str]] = {
    "julie@patriotrei.net": {"POST", "PUT", "PATCH"},  # add "DELETE" if she needs deletes
}


def can_read(email: str | None) -> bool:
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    return email in READ_EMAILS or email.rsplit("@", 1)[1] in READ_DOMAINS


def write_methods(identity: str | None) -> set[str]:
    return WRITE_PERMISSIONS.get((identity or "").strip().lower(), set())
