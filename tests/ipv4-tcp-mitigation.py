"""Exercise the installed mitigation with real packets in a disposable VM."""
import json
import socket
import subprocess
import threading
from pathlib import Path

from scapy.all import IP, TCP, IPOption, sr1

UNIT = "agent-box-cve-2026-98432.service"
RULES = Path("/etc/agent-box/cve-2026-98432.nft")


def nft(*args):
    return subprocess.check_output(["nft", *args], text=True)


def rules():
    return json.loads(nft("-j", "list", "chain", "ip", "cve_2026_98432",
                          "prerouting"))["nftables"]


def counter():
    return next(expr["counter"]["packets"] for value in rules()
                if "rule" in value for expr in value["rule"]["expr"]
                if "counter" in expr)


nft("add", "table", "ip", "unrelated_check")
nft("add", "chain", "ip", "unrelated_check", "sentinel")
nft("add", "rule", "ip", "unrelated_check", "sentinel", "counter", "accept")
for _ in range(2):
    subprocess.run(["systemctl", "reload", UNIT], check=True)
assert sum("rule" in value for value in rules()) == 1
assert "accept" in nft("list", "chain", "ip", "unrelated_check", "sentinel")
print("PASS: reload is idempotent and preserves unrelated firewall rules")

before = rules()
bad = Path("/tmp/invalid-mitigation.nft")
bad.write_text(RULES.read_text().replace("counter drop", "counter invalid_drop"))
invalid = subprocess.run(["nft", "--file", str(bad)], capture_output=True)
assert invalid.returncode != 0
assert rules() == before
print("PASS: malformed reload leaves the active mitigation intact")

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", 18080))
server.listen(5)


def serve():
    while True:
        client, _ = server.accept()
        client.sendall(b"normal-tcp-ok")
        client.close()


threading.Thread(target=serve, daemon=True).start()
with socket.create_connection(("127.0.0.1", 18080), timeout=3) as client:
    assert client.recv(64) == b"normal-tcp-ok"
assert counter() == 0
print("PASS: ordinary IPv4 TCP reaches the listener")
packet = IP(dst="127.0.0.1", options=[IPOption(b"\x01\x01\x01\x00")]) / TCP(
    sport=40001, dport=18080, flags="S", seq=100)
answer = sr1(packet, timeout=1, verbose=False)
assert answer is None, repr(answer)
assert counter() > 0
print("PASS: IPv4 TCP with IP options is dropped and counted")
