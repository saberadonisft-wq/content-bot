from __future__ import annotations

import os
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def generate_rsa_keypair() -> tuple[str, str]:
    """Generate a new 2048-bit RSA key pair in PEM format."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    return private_pem, public_pem


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    port: int = 8080
    host: str = "0.0.0.0"

    mongodb_uri: str = ""
    mongodb_database: str = "content_bot_auth"

    jwt_private_key_pem: str = ""
    jwt_public_key_pem: str = ""
    jwt_algorithm: str = "RS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30

    admin_email: str = "saberadonisft@gmail.com"

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "https://content-bot-auth.azurewebsites.net/auth/google/callback"

    frontend_url: str = "http://127.0.0.1:5173"
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173,https://content-bot-auth.azurewebsites.net"

    # Cloudflare R2 / Release Storage config
    r2_public_url: str = ""
    r2_bucket_name: str = "content-bot-releases"
    r2_endpoint_url: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""

    def ensure_jwt_keys(self) -> tuple[str, str]:
        """Ensure valid RSA private and public keys are present."""
        if self.jwt_private_key_pem and self.jwt_public_key_pem:
            return self.jwt_private_key_pem, self.jwt_public_key_pem

        # Try to read or persist to data directory
        data_dir = Path(__file__).resolve().parents[1] / "data"
        priv_file = data_dir / "jwt_private.pem"
        pub_file = data_dir / "jwt_public.pem"

        if priv_file.exists() and pub_file.exists():
            try:
                priv = priv_file.read_text(encoding="utf-8").strip()
                pub = pub_file.read_text(encoding="utf-8").strip()
                if priv and pub:
                    self.jwt_private_key_pem = priv
                    self.jwt_public_key_pem = pub
                    return priv, pub
            except Exception:
                pass

        priv, pub = generate_rsa_keypair()
        self.jwt_private_key_pem = priv
        self.jwt_public_key_pem = pub

        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            priv_file.write_text(priv, encoding="utf-8")
            pub_file.write_text(pub, encoding="utf-8")
        except Exception:
            # Filesystem might be read-only in some cloud environments; in-memory is fine
            pass

        return priv, pub


settings = Settings()
# Initialize keys on startup
settings.ensure_jwt_keys()
