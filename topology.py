#!/usr/bin/env python3
r"""
Mininet topology for the Smart Content Delivery System.

              h1 (10.0.0.1) ---\                    /--- srv1 (10.0.0.11)  100 Mbps, 2 ms
                                 s1 (OpenFlow 1.3) ---- srv2 (10.0.0.12)   50 Mbps, 5 ms
              h2 (10.0.0.2) ---/        |           \--- srv3 (10.0.0.13)   20 Mbps, 10 ms
                                        |
                          remote OS-Ken/Ryu controller (127.0.0.1:6653)
                          virtual service IP: 10.0.0.100:5000

Usage (start the controller FIRST, in another terminal):
    sudo python3 topology.py            # build network, start servers, open CLI
    sudo python3 topology.py --test     # also run the automatic D1 test cases
    sudo python3 topology.py --test --no-cli
    sudo python3 topology.py --test --d2-test --no-cli
"""
import argparse
import os
import re
import sys
import time

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController
from mininet.topo import Topo

import sdn_config as cfg

BASE = os.path.dirname(os.path.abspath(__file__))


class ContentTopo(Topo):
    def build(self, shaping=True):
        s1 = self.addSwitch("s1", protocols="OpenFlow13")
        for c in cfg.CLIENTS:
            h = self.addHost(c["name"], ip=c["ip"] + "/24", mac=c["mac"])
            self.addLink(h, s1, port2=c["port"], bw=100)
        for s in cfg.SERVERS:
            h = self.addHost(s["name"], ip=s["ip"] + "/24", mac=s["mac"])
            opts = {"bw": s["bw"], "delay": s["delay"]} if shaping else {}
            self.addLink(h, s1, port2=s["port"], **opts)


def start_server(net, name, pids):
    """Start exactly one server; keep its PID for safe D2 failure injection."""
    server = next(s for s in cfg.SERVERS if s["name"] == name)
    host = net.get(name)
    # Let the actual Python server report its PID: shell-background PIDs can
    # otherwise refer to a wrapper shell rather than the backend itself.
    host.cmd("rm -f %s/logs/%s.pid" % (BASE, name))
    command = ("cd %s && sh -c 'exec python3 -u content_server.py "
               "--name %s --bind %s --content-dir content "
               "--pid-file logs/%s.pid' "
               "> logs/%s.log 2>&1 &" %
               (BASE, name, server["ip"], name, name))
    host.cmd(command)
    pid = None
    for _ in range(30):
        output = host.cmd("cat %s/logs/%s.pid 2>/dev/null" % (BASE, name)).strip()
        if output.isdigit():
            pid = int(output)
            break
        time.sleep(0.1)
    if pid is None:
        raise RuntimeError("Cannot start %s; inspect logs/%s.log" % (name, name))
    pids[name] = pid
    info("*** started %s on %s:%d (pid %s)\n" % (
        name, server["ip"], cfg.SERVICE_PORT, pid))


def stop_server(net, name, pids):
    pid = pids.pop(name, None)
    if pid is not None:
        net.get(name).cmd("kill -TERM %d 2>/dev/null || true" % pid)
        net.get(name).cmd("rm -f %s/logs/%s.pid" % (BASE, name))
        info("*** stopped %s (pid %s)\n" % (name, pid))


def start_servers(net, pids):
    os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
    cfg.ensure_content(os.path.join(BASE, "content"))
    for server in cfg.SERVERS:
        start_server(net, server["name"], pids)


def allow_controller_to_write_runtime():
    """Mininet is sudo/root; OS-Ken is usually run by the desktop user."""
    runtime = os.path.join(BASE, "runtime")
    os.makedirs(runtime, exist_ok=True)
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        os.chown(runtime, int(os.environ["SUDO_UID"]),
                 int(os.environ.get("SUDO_GID", 0)))


def client_cmd(args):
    return "cd %s && python3 content_client.py %s" % (BASE, args)


def run_tests(net):
    h1, h2, s1 = net.get("h1", "h2", "s1")
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))

    print("\n================ D1 TEST CASES ================")

    # T1 - basic L2 connectivity through the SDN switch (ARP + learning switch)
    out = h1.cmd("ping -c 3 -W 2 %s" % cfg.SERVERS[0]["ip"])
    check("T1 host-to-host connectivity (h1 -> srv1 ping)", " 0% packet loss" in out)

    # T2 - application protocol through the VIP: LIST command
    out = h1.cmd(client_cmd("--list"))
    check("T2 TCP request to VIP works (LIST)",
          "small.txt" in out and "large.bin" in out)

    # T3 - file transfer integrity through the rewritten flows
    out = h1.cmd(client_cmd("--file medium.bin"))
    m = re.search(r"bytes=(\d+) server=(\S+)", out)
    check("T3 file download via VIP (medium.bin = 262144 bytes)",
          bool(m) and int(m.group(1)) == 262144 and "FAIL" not in out,
          "(served by %s)" % (m.group(2) if m else "?"))

    # T4 - controller spreads new connections over servers
    out = h1.cmd(client_cmd("--file small.txt --count 9 --interval 0.3"))
    servers = re.findall(r"server=(\S+)", out)
    dist = {s: servers.count(s) for s in sorted(set(servers))}
    check("T4 controller distributes connections over servers",
          len(servers) == 9 and len(dist) >= 2 and "FAIL" not in out, str(dist))

    # T5 - two clients at the same time
    cmd = client_cmd("--file large.bin --count 2")
    h1.sendCmd(cmd)
    h2.sendCmd(cmd)
    o1, o2 = h1.waitOutput(), h2.waitOutput()
    ok = o1.count("RESULT") == 2 and o2.count("RESULT") == 2 and "FAIL" not in o1 + o2
    check("T5 two concurrent clients (h1, h2) download large.bin", ok)

    # T6 - the SDN flow table really contains the VIP rewrite rules
    flows = s1.cmd("ovs-ofctl -O OpenFlow13 dump-flows s1")
    check("T6 switch has VIP rewrite flows installed by the controller",
          "ip_dst" in flows and cfg.VIP in flows)
    print("\n--- sample of s1 flow table (take a screenshot of this) ---")
    for line in [l for l in flows.splitlines() if "set_field" in l][:4]:
        print(" ", line.strip())

    print("\n  RESULT: %d/%d tests passed" % (sum(results), len(results)))
    return all(results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="run D1 test cases")
    ap.add_argument("--d2-test", action="store_true",
                    help="run D2 benchmark + failure/recovery simulations")
    ap.add_argument("--no-cli", action="store_true", help="exit after tests")
    ap.add_argument("--no-shaping", action="store_true",
                    help="do not set bw/delay on links (if tc/netem is missing)")
    a = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run with sudo:  sudo python3 topology.py")

    setLogLevel("info")
    allow_controller_to_write_runtime()
    net = Mininet(topo=ContentTopo(shaping=not a.no_shaping),
                  switch=OVSKernelSwitch, link=TCLink,
                  controller=None, autoSetMacs=False)
    net.addController("c0", controller=RemoteController,
                      ip=cfg.CONTROLLER_IP, port=cfg.CONTROLLER_PORT)
    net.start()
    from sdn_admin import set_mode, wait_for_mode
    revision = set_mode("adaptive")
    if not wait_for_mode("adaptive", revision, timeout=8):
        net.stop()
        sys.exit("Controller did not acknowledge adaptive policy. "
                 "Start the OS-Ken controller first and check runtime/ permissions.")
    pids = {}
    ok = True
    try:
        try:
            net.waitConnected()
        except Exception:                      # older Mininet: no waitConnected
            time.sleep(3)
        start_servers(net, pids)
        wait = cfg.HEARTBEAT_INTERVAL * 2 + 1
        info("*** waiting %ds for server heartbeats to reach the controller\n" % wait)
        time.sleep(wait)
        if a.test:
            ok = run_tests(net) and ok
        if a.d2_test:
            from d2_evaluation import run_d2
            ok = run_d2(
                net,
                stop_server=lambda name: stop_server(net, name, pids),
                start_server=lambda name: start_server(net, name, pids)) and ok
        if not a.no_cli:
            info("*** D1: h1 python3 content_client.py --file large.bin --count 5\n")
            info("*** D2: sh python3 sdn_admin.py status\n")
            CLI(net)
    finally:
        for name in list(pids):
            stop_server(net, name, pids)
        net.stop()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
