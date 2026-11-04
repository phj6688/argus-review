"""Host canonicalization shared by forge configuration."""


def canonical_host(host: str, port: int | None = None, default_port: int = 443) -> str:
    """Normalize a host without validation, always bracketing IPv6 literals."""
    host = host.strip().lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None and port != default_port else host
