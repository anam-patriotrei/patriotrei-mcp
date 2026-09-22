def pick(d, *names, default=None):
    """Buildium's SDK docs show snake_case, but the raw REST JSON is PascalCase.
    Try a few casings so field access doesn't silently break either way."""
    if not isinstance(d, dict):
        return default
    for n in names:
        if n in d:
            return d[n]
    return default
