"""Isolate auth integration tests before application modules construct their database."""

from app.config import generate_rsa_keypair, settings

# Never connect to the database or reuse signing keys from a developer's .env.
settings.mongodb_uri = ""
settings.jwt_private_key_pem, settings.jwt_public_key_pem = generate_rsa_keypair()
settings.admin_email = "admin@example.com"
