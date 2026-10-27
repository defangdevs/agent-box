"""Offline distrust regression, run under the deployed TLS environment."""
import os
from pathlib import Path
import ssl
import subprocess

blocked = {"AAA Certificate Services", "emSign ECC Root CA - G3",
           "emSign Root CA - C1", "emSign ECC Root CA - C3"}
certs = ssl.create_default_context().get_ca_certs()
assert len(certs) > 50, "An empty/broken trust store is not a mitigation"
subjects = {value for cert in certs for rdn in cert["subject"]
            for key, value in rdn if key == "commonName"}
assert not subjects & blocked, subjects & blocked
chains = sorted(Path(__file__).with_name("tls-chains").glob("*.pem"))
assert len(chains) == 12, f"Expected 12 chain fixtures, found {len(chains)}"
for chain in chains:
    result = subprocess.run([
        "openssl", "verify", "-attime", "1792530000", "-purpose", "sslserver",
        "-CAfile", os.environ["SSL_CERT_FILE"], "-untrusted", str(chain), str(chain),
    ], capture_output=True, text=True)
    assert result.returncode != 0, f"Compromised chain accepted: {chain.name}"
    assert "error 20" in result.stderr or "error 28" in result.stderr, result.stderr
    print("REJECT", chain.name)
print("TLS_TRUST_OK: nonempty store, blocked roots absent, all 12 chains rejected")
