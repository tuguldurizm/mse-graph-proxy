import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str = field(default_factory=lambda: os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    # NEO4J_USERNAME is what Neo4j Aura's downloaded credentials file uses
    neo4j_user: str = field(default_factory=lambda: os.getenv("NEO4J_USER") or os.getenv("NEO4J_USERNAME") or "neo4j")
    neo4j_password: str = field(default_factory=lambda: os.getenv("NEO4J_PASSWORD", "msegraph123"))
    neo4j_database: str = field(default_factory=lambda: os.getenv("NEO4J_DATABASE", "neo4j"))

    mse_base_url: str = field(default_factory=lambda: os.getenv("MSE_BASE_URL", "https://mse.mn"))
    # Language segment used in MSE URLs ("mn" or "en").
    mse_lang: str = field(default_factory=lambda: os.getenv("MSE_LANG", "mn"))
    # Seconds between HTTP requests, to be polite to mse.mn.
    request_delay: float = field(default_factory=lambda: float(os.getenv("MSE_REQUEST_DELAY", "1.0")))

    # Optional allow-listed fetch proxy (proxy/app.py, deployed on Render).
    proxy_url: str = field(default_factory=lambda: os.getenv("MSE_PROXY_URL", ""))
    proxy_token: str = field(default_factory=lambda: os.getenv("MSE_PROXY_TOKEN", ""))

    # Open Data Lab Mongolia
    odl_base_url: str = field(default_factory=lambda: os.getenv("ODL_BASE_URL", "https://opendatalab.mn"))


settings = Settings()


LOCAL_NEO4J_HOSTS = {"localhost", "127.0.0.1", "::1", "neo4j"}   # "neo4j" = docker compose service


def neo4j_is_local(uri: str | None = None) -> bool:
    from urllib.parse import urlsplit
    return (urlsplit(uri or settings.neo4j_uri).hostname or "localhost") in LOCAL_NEO4J_HOSTS
