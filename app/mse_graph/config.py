import os
from dataclasses import dataclass, field


def _neo4j_uri(raw: str) -> str:
    """NEO4J_URI with a scheme. A bare Aura host ("xxxx.databases.neo4j.io") gets
    neo4j+s://, which Aura requires; any other bare host is an error."""
    uri = raw.strip()
    if not uri:
        return "bolt://localhost:7687"
    if "://" in uri:
        return uri
    if uri.split("/")[0].split(":")[0].endswith(".neo4j.io"):
        return "neo4j+s://" + uri
    raise ValueError(f"NEO4J_URI={uri!r} has no scheme; use e.g. bolt://{uri} or neo4j+s://{uri}")


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str = field(default_factory=lambda: _neo4j_uri(os.getenv("NEO4J_URI", "")))
    # NEO4J_USERNAME is what Neo4j Aura's downloaded credentials file uses
    neo4j_user: str = field(default_factory=lambda: os.getenv("NEO4J_USER") or os.getenv("NEO4J_USERNAME") or "neo4j")
    neo4j_password: str = field(default_factory=lambda: os.getenv("NEO4J_PASSWORD", "msegraph123"))
    # Empty = the server's default (home) database. Don't assume "neo4j": newer Aura
    # instances name their database after the instance id.
    neo4j_database: str = field(default_factory=lambda: os.getenv("NEO4J_DATABASE", "").strip())

    mse_base_url: str = field(default_factory=lambda: os.getenv("MSE_BASE_URL", "https://mse.mn"))
    # Language segment used in MSE URLs ("mn" or "en").
    mse_lang: str = field(default_factory=lambda: os.getenv("MSE_LANG", "mn"))
    # Seconds between HTTP requests, to be polite to mse.mn.
    request_delay: float = field(default_factory=lambda: float(os.getenv("MSE_REQUEST_DELAY", "1.0")))

    # Optional allow-listed fetch proxy (proxy/app.py, deployed on Render).
    proxy_url: str = field(default_factory=lambda: os.getenv("MSE_PROXY_URL", ""))
    proxy_token: str = field(default_factory=lambda: os.getenv("MSE_PROXY_TOKEN", ""))

    # Open Data Lab Mongolia. The bare domain often times out; the www host answers.
    odl_base_url: str = field(default_factory=lambda: os.getenv("ODL_BASE_URL", "https://www.opendatalab.mn"))
    # Egress for opendatalab.mn, which blocks the hosted server's address (issue #4).
    # See mse_graph/egress.py: proxy / gateway routes, gateway token, extra CA, tries per route.
    odl_proxy_url: str = field(default_factory=lambda: os.getenv("ODL_PROXY_URL", ""))
    odl_proxy_token: str = field(default_factory=lambda: os.getenv("ODL_PROXY_TOKEN", ""))
    odl_proxy_ca_bundle: str = field(default_factory=lambda: os.getenv("ODL_PROXY_CA_BUNDLE", ""))
    odl_proxy_attempts: int = field(default_factory=lambda: int(os.getenv("ODL_PROXY_ATTEMPTS") or "2"))


settings = Settings()


LOCAL_NEO4J_HOSTS = {"localhost", "127.0.0.1", "::1", "neo4j"}   # "neo4j" = docker compose service


def neo4j_is_local(uri: str | None = None) -> bool:
    from urllib.parse import urlsplit
    return (urlsplit(uri or settings.neo4j_uri).hostname or "localhost") in LOCAL_NEO4J_HOSTS
