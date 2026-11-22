"""
Shared configuration for Project 19 - Smart Content Delivery System.

This one file is imported by the SDN controller, the Mininet topology, the
content servers and the client, so that addresses/ports are defined ONCE.
"""
import os

# ---- Virtual service (what clients connect to) -------------------------------
VIP = "10.0.0.100"                 # virtual IP of the content service
VMAC = "00:00:00:00:01:00"         # virtual MAC the controller answers ARP with
SERVICE_PORT = 5000                # TCP port of the content service

# ---- Server -> controller heartbeat (application event -> SDN) ---------------
HEARTBEAT_ADDR = "10.0.0.255"      # UDP broadcast of the 10.0.0.0/24 subnet
HEARTBEAT_PORT = 9999
HEARTBEAT_INTERVAL = 2             # seconds between heartbeats
HEARTBEAT_TIMEOUT = 6              # no heartbeat for this long -> server DOWN

# ---- Remote SDN controller (Ryu default OpenFlow port) -----------------------
CONTROLLER_IP = "127.0.0.1"
CONTROLLER_PORT = 6653

# ---- Hosts. "port" = switch port number (fixed so controller can use it) -----
CLIENTS = [
    {"name": "h1", "ip": "10.0.0.1", "mac": "00:00:00:00:00:01", "port": 1},
    {"name": "h2", "ip": "10.0.0.2", "mac": "00:00:00:00:00:02", "port": 2},
]

# bw (Mbps) / delay differ per server so "network conditions" are not identical.
SERVERS = [
    {"name": "srv1", "ip": "10.0.0.11", "mac": "00:00:00:00:00:11", "port": 3,
     "bw": 100, "delay": "2ms"},
    {"name": "srv2", "ip": "10.0.0.12", "mac": "00:00:00:00:00:12", "port": 4,
     "bw": 50, "delay": "5ms"},
    {"name": "srv3", "ip": "10.0.0.13", "mac": "00:00:00:00:00:13", "port": 5,
     "bw": 20, "delay": "10ms"},
]


def ensure_content(content_dir):
    """Create the sample content files (identical on every server)."""
    os.makedirs(content_dir, exist_ok=True)
    samples = {
        "small.txt": b"Hello from the Smart Content Delivery System!\n" * 20,
        "medium.bin": bytes(range(256)) * 1024,    # 256 KB
        "large.bin": bytes(range(256)) * 8192,     # 2 MB
    }
    for fname, data in samples.items():
        path = os.path.join(content_dir, fname)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(data)
