#!/usr/bin/env python3
"""One-time Tesla partner setup: create the app key pair and register your domain.

  python scripts/register_partner.py keygen                 # writes keys/private-key.pem + keys/public-key.pem
  python scripts/register_partner.py register example.com   # after the app is reachable at https://example.com

Needs BH_TESLA_CLIENT_ID, BH_TESLA_CLIENT_SECRET and BH_TESLA_FLEET_BASE in the environment.
The public key must already be served at
https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem (the app does this).
"""
import os
import sys

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"


def keygen() -> None:
    os.makedirs("keys", exist_ok=True)
    if os.path.exists("keys/private-key.pem"):
        sys.exit("keys/private-key.pem already exists; refusing to overwrite")
    key = ec.generate_private_key(ec.SECP256R1())
    with open(os.open("keys/private-key.pem", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open("keys/public-key.pem", "wb") as f:
        f.write(key.public_key().public_bytes(serialization.Encoding.PEM,
                                              serialization.PublicFormat.SubjectPublicKeyInfo))
    print("wrote keys/private-key.pem (keep secret) and keys/public-key.pem")


def register(domain: str) -> None:
    base = os.environ.get("BH_TESLA_FLEET_BASE", "https://fleet-api.prd.eu.vn.cloud.tesla.com")
    r = httpx.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": os.environ["BH_TESLA_CLIENT_ID"],
        "client_secret": os.environ["BH_TESLA_CLIENT_SECRET"],
        "scope": "openid vehicle_device_data",
        "audience": base,
    })
    if r.status_code != 200:
        # Tesla's answer says what is wrong (invalid_client, bad audience, ...); it holds no secrets.
        sys.exit(f"token request failed: {r.status_code} {r.text}")
    r = httpx.post(f"{base}/api/1/partner_accounts", json={"domain": domain},
                   headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    print(r.status_code, r.text)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "keygen":
        keygen()
    elif len(sys.argv) == 3 and sys.argv[1] == "register":
        register(sys.argv[2])
    else:
        sys.exit(__doc__)
