import json
import os
import time
from pathlib import Path

import ngrok
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

NGROK_AUTHTOKEN = os.environ["NGROK_AUTHTOKEN"]
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
# Optional reserved ngrok domain (e.g. "swivel-aide-overprice.ngrok-free.dev").
# When unset, ngrok hands out an ephemeral URL that changes every run.
NGROK_DOMAIN = os.getenv("NGROK_DOMAIN")
CA_PATH = Path(
    os.getenv("OPENAI_MTLS_CA_PATH", "openai-connectors-mtls-ca.pem")
).expanduser()
if not CA_PATH.is_absolute():
    CA_PATH = (ROOT / CA_PATH).resolve(strict=True)
else:
    CA_PATH = CA_PATH.resolve(strict=True)

openai_connector_ca = CA_PATH.read_text(encoding="utf-8").strip()
if "-----BEGIN CERTIFICATE-----" not in openai_connector_ca:
    raise RuntimeError(f"Invalid CA certificate: {CA_PATH}")

traffic_policy = {
    "on_tcp_connect": [
        {
            "actions": [
                {
                    "type": "terminate-tls",
                    "config": {
                        "mutual_tls_certificate_authorities": [openai_connector_ca]
                    },
                }
            ]
        }
    ]
}

forward_kwargs = {
    "authtoken": NGROK_AUTHTOKEN,
    "traffic_policy": json.dumps(traffic_policy),
}
if NGROK_DOMAIN:
    forward_kwargs["domain"] = NGROK_DOMAIN

listener = ngrok.forward(MCP_PORT, **forward_kwargs)
public_url = listener.url().rstrip("/")

print()
print("Protected ngrok endpoint established")
if NGROK_DOMAIN:
    print(f"Reserved domain: {NGROK_DOMAIN}")
print(f"MCP connector URL: {public_url}/mcp")
print("Press Ctrl+C to close the endpoint.")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nClosing ngrok endpoint...")
    listener.close()

