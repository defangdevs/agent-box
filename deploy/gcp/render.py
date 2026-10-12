#!/usr/bin/env python3
"""Render bootstrap.sh.in into a GCE startup-script (PROTOTYPE).

Every value reaches the script base64-encoded, so nothing a caller passes can
break out of the shell literal it lands in (the Azure template's rule).
Plain-marker values (flake ref, installer URL, image flag) are validated here
instead, because they are substituted raw.
"""
import argparse
import base64
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def render(*, public_ip, password_hash, user="workspace",
           flake_ref="github:defangdevs/agent-box",
           nix_installer="https://install.determinate.systems/nix",
           sslip_domains=("sslip.io",), agents_md="", portal_issuer="",
           portal_user="", image_includes_runtime=False):
    if not password_hash.startswith(("$argon2id$", "$2a$", "$2b$", "$2y$")):
        raise ValueError("password_hash must be an argon2id or bcrypt hash, never a plaintext password")
    if not re.fullmatch(r"[A-Za-z0-9:/._#?=&+-]+", flake_ref):
        raise ValueError(f"unsafe flake ref {flake_ref!r}")
    if not re.fullmatch(r"https://[A-Za-z0-9./_-]+", nix_installer):
        raise ValueError(f"unsafe installer URL {nix_installer!r}")
    if bool(portal_issuer) != bool(portal_user):
        portal_issuer = portal_user = ""  # both or neither, as on Azure
    values = {
        "@@NIXINSTALLER@@": nix_installer,
        "@@FLAKEREF@@": flake_ref,
        "@@IMAGERUNTIME@@": "true" if image_includes_runtime else "false",
        "@@USERB64@@": b64(user),
        "@@PUBLICIPB64@@": b64(public_ip),
        "@@PORTALISSUERB64@@": b64(portal_issuer),
        "@@PORTALUSERIDB64@@": b64(portal_user),
        "@@SSLIPDOMAINSB64@@": b64(json.dumps(list(sslip_domains))),
        "@@AGENTSMDB64@@": b64(agents_md),
        "@@WEBPASSWORDHASHB64@@": b64(password_hash),
    }
    out = (HERE / "bootstrap.sh.in").read_text()
    for marker, value in values.items():
        if marker not in out:
            raise ValueError(f"marker {marker} missing from bootstrap.sh.in")
        out = out.replace(marker, value)
    leftover = re.findall(r"@@[A-Z0-9]+@@", out)
    if leftover:
        raise ValueError(f"unsubstituted markers: {leftover}")
    out.encode("ascii")  # keep the script ASCII, as the Azure check does
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--public-ip", required=True)
    p.add_argument("--password-hash-file", required=True)
    p.add_argument("--user", default="workspace")
    p.add_argument("--flake-ref", default="github:defangdevs/agent-box")
    p.add_argument("--sslip-domains", default='["sslip.io"]')
    p.add_argument("--agents-md-file")
    p.add_argument("--portal-issuer", default="")
    p.add_argument("--portal-user", default="")
    a = p.parse_args()
    sys.stdout.write(render(
        public_ip=a.public_ip,
        password_hash=Path(a.password_hash_file).read_text().strip(),
        user=a.user, flake_ref=a.flake_ref,
        sslip_domains=json.loads(a.sslip_domains),
        agents_md=Path(a.agents_md_file).read_text() if a.agents_md_file else "",
        portal_issuer=a.portal_issuer, portal_user=a.portal_user))


if __name__ == "__main__":
    main()
