"""Automated D2 demonstration against a real Mininet network (Ubuntu only).

Collects real observations. No benchmark results are hard-coded.
"""
import csv
from datetime import datetime
import os
import re
import statistics
import time

import sdn_config as cfg
from sdn_admin import set_mode, wait_for_mode, status

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(ROOT, "results")
RESULT = re.compile(r"RESULT src=\S+ file=\S+ bytes=(\d+) server=(\S+) "
                    r"time_s=([0-9.]+) throughput_mbps=([0-9.]+)")


def wait_health(expected, timeout=16):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = status()
        if (state and time.time() - state.get("timestamp", 0) < 4 and
                all(state.get("servers", {}).get(name, {}).get("alive") == alive
                    for name, alive in expected.items())):
            return True
        time.sleep(0.25)
    return False


def switch_mode(mode):
    revision = set_mode(mode)
    if not wait_for_mode(mode, revision, timeout=10):
        raise RuntimeError("Controller didn't acknowledge mode %s."
                           " Check the OS-Ken terminal and runtime permissions." % mode)
    # Allow queued FlowMod DELETE_STRICT messages to reach the switch.
    time.sleep(0.5)


def run_requests(host, client_name, scenario, mode, args, records):
    command = "cd %s && python3 content_client.py %s" % (ROOT, args)
    output = host.cmd(command)
    print("\n--- %s (%s) ---\n%s" % (scenario, client_name, output.strip()),
          flush=True)
    parsed = 0
    for line in output.splitlines():
        match = RESULT.search(line)
        if match:
            records.append({"scenario": scenario, "mode": mode,
                            "client": client_name, "success": True,
                            "server": match.group(2), "bytes": int(match.group(1)),
                            "time_s": float(match.group(3)),
                            "mbps": float(match.group(4)), "message": ""})
            parsed += 1
        elif line.startswith("FAIL "):
            records.append({"scenario": scenario, "mode": mode,
                            "client": client_name, "success": False,
                            "server": "", "bytes": 0, "time_s": "", "mbps": "",
                            "message": line[5:]})
            parsed += 1
    if parsed == 0:
        records.append({"scenario": scenario, "mode": mode,
                        "client": client_name, "success": False,
                        "server": "", "bytes": 0, "time_s": "", "mbps": "",
                        "message": "No client RESULT/FAIL lines: " + output[-350:]})
    return [r for r in records if r["scenario"] == scenario]


def batch(net, scenario, mode, records, file="medium.bin", count=6,
          parallel=1, timeout=12):
    args = "--file %s --count %d --parallel %d --timeout %d" % (
        file, count, parallel, timeout)
    return run_requests(net.get("h1"), "h1", scenario, mode, args, records)


def stats(items):
    successes = [r for r in items if r["success"]]
    lat = [r["time_s"] for r in successes]
    mbps = [r["mbps"] for r in successes]
    order = sorted(lat)
    p95 = order[max(0, (95 * len(order) + 99) // 100 - 1)] if order else None
    distribution = {name: sum(r["server"] == name for r in successes)
                    for name in (s["name"] for s in cfg.SERVERS)}
    return {"passed": len(successes), "total": len(items),
            "success_percent": round(100 * len(successes) / len(items), 1)
            if items else 0.0,
            "mean_latency": statistics.mean(lat) if lat else None,
            "p95_latency": p95,
            "mean_mbps": statistics.mean(mbps) if mbps else None,
            "distribution": distribution}


def write_results(records, checks, basename):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    csv_path = os.path.join(RESULTS_DIR, basename + ".csv")
    md_path = os.path.join(RESULTS_DIR, basename + ".md")
    columns = ["scenario", "mode", "client", "success", "server",
               "bytes", "time_s", "mbps", "message"]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)

    scenarios = list(dict.fromkeys(r["scenario"] for r in records))
    with open(md_path, "w") as f:
        f.write("# D2 live evaluation results\n\n")
        f.write("Generated: %s\n\n" % datetime.now().isoformat(timespec="seconds"))
        f.write("Environment: Mininet/OVS/OS-Ken on the executing machine. "
                "All performance figures below come from real client responses.\n\n")
        f.write("| Scenario | Policy | Success | Mean response time (s) | "
                "P95 (s) | Mean goodput (Mbps) | Server distribution |\n")
        f.write("|---|---|---:|---:|---:|---:|---|\n")
        for phase in scenarios:
            rows = [r for r in records if r["scenario"] == phase]
            s = stats(rows)
            fm = lambda x: "N/A" if x is None else "%.4f" % x
            f.write("| %s | %s | %d/%d (%.1f%%) | %s | %s | %s | %s |\n" % (
                phase, rows[0]["mode"], s["passed"], s["total"],
                s["success_percent"], fm(s["mean_latency"]),
                fm(s["p95_latency"]), fm(s["mean_mbps"]),
                ", ".join("%s=%d" % kv for kv in s["distribution"].items())))
        f.write("\n## Assertions\n\n")
        for label, passed, detail in checks:
            f.write("- %s **%s**: %s\n" % (
                "PASS" if passed else "FAIL", label, detail))
        f.write("\n## Interpretation\n\n")
        f.write("- `static` always targets srv1. It is deliberately a no-failover "
                "baseline; a failed srv1 should make these requests fail.\n")
        f.write("- `adaptive` uses only servers with recent UDP heartbeats and "
                "balances new connections by reported active load.\n")
        f.write("- The failure and link-down scenarios measure recovery of "
                "**new connections**, not migration of an existing TCP stream.\n")
        f.write("- Mean goodput measures client-received file bytes divided by "
                "transfer time. P95 uses nearest-rank (small-sample) estimation.\n")
        f.write("- Healthy static and adaptive tests are run sequentially in "
                "the same topology. Small samples and VM scheduling can affect "
                "comparisons; interpret numeric differences cautiously.\n")
        f.write("- For full evidence, include the controller logs for `[HEALTH]`, "
                "`[FAILOVER]`, `[LB]` and OpenFlow flow dumps.\n")
    return csv_path, md_path


def run_d2(net, stop_server, start_server):
    print("\n================ D2 LIVE TESTS ================", flush=True)
    records = []
    checks = []
    basename = "d2_%s" % datetime.now().strftime("%Y%m%d_%H%M%S")
    link_srv2_down = False
    srv1_stopped = False

    def check(label, passed, detail=""):
        checks.append((label, bool(passed), detail))
        print("[%s] %s %s" % ("PASS" if passed else "FAIL", label, detail),
              flush=True)

    def flows_to(server_ip):
        flow_text = net.get("s1").cmd("ovs-ofctl -O OpenFlow13 dump-flows s1")
        return ("set_field:%s->ip_dst" % server_ip) in flow_text

    try:
        if not wait_health({name["name"]: True for name in cfg.SERVERS}, 16):
            raise RuntimeError("Not all servers are UP. Check controller heartbeats/logs.")
        check("D2-1 all 3 servers online", True)

        # Same healthy, shaped Mininet links; identical file and request count.
        switch_mode("static")
        b = batch(net, "healthy_static", "static", records, "large.bin", 4, 2)
        check("D2-2 baseline static requests", len(b) == 8 and
              all(r["success"] and r["server"] == "srv1" for r in b),
              str(stats(b)))
        switch_mode("adaptive")
        b = batch(net, "healthy_adaptive", "adaptive", records,
                  "large.bin", 4, 2)
        check("D2-3 adaptive healthy transfers", len(b) == 8 and
              all(r["success"] for r in b) and
              len({r["server"] for r in b}) >= 2, str(stats(b)))

        # Seed a real, recent switch flow toward srv1 before injecting failure.
        # Merely checking that *no* old flow remains could otherwise pass even
        # if all flows had naturally expired before the failure.
        switch_mode("static")
        seed = batch(net, "pre_failure_srv1_flow", "static", records,
                     "small.txt", 1, 1)
        check("D2-4 verified active VIP rule targeting srv1 before failure",
              len(seed) == 1 and seed[0]["success"] and
              seed[0]["server"] == "srv1" and
              flows_to(cfg.SERVERS[0]["ip"]))
        before = status() or {}
        purged_before = before.get("purged_flows", 0)

        # Meaningful failure #1: server PROCESS dies. Static fails, adaptive recovers.
        print("\n[INJECT FAILURE] Kill srv1 content server process", flush=True)
        stop_server("srv1")
        srv1_stopped = True
        check("D2-5 controller detects srv1 death", wait_health({"srv1": False}))
        time.sleep(0.7)
        observed = status() or {}
        check("D2-6 failed srv1 flow deleted by OpenFlow controller",
              not flows_to(cfg.SERVERS[0]["ip"]) and
              observed.get("purged_flows", 0) >= purged_before + 2,
              "purged flow rules %s -> %s" % (
                  purged_before, observed.get("purged_flows", 0)))
        switch_mode("static")
        b = batch(net, "srv1_failed_static", "static", records,
                  "small.txt", 2, 1, 2)
        check("D2-7 static baseline has no failover", len(b) == 2 and
              all(not r["success"] for r in b), str(stats(b)))
        switch_mode("adaptive")
        b = batch(net, "srv1_failed_adaptive", "adaptive", records,
                  "medium.bin", 8, 1)
        check("D2-8 adaptive routes around failed srv1", len(b) == 8 and
              all(r["success"] and r["server"] in ("srv2", "srv3") for r in b),
              str(stats(b)))
        check("D2-9 OpenFlow switch now forwards to alternative server",
              flows_to(cfg.SERVERS[1]["ip"]) or flows_to(cfg.SERVERS[2]["ip"]))

        # Recovery: server restarted, heartbeat UP, can be chosen again.
        print("\n[RECOVERY] Restart srv1", flush=True)
        start_server("srv1")
        srv1_stopped = False
        check("D2-10 controller detects srv1 restored", wait_health({"srv1": True}))
        b = batch(net, "srv1_restored", "adaptive", records,
                  "small.txt", 9, 1)
        check("D2-11 restored srv1 re-enters rotation", len(b) == 9 and
              all(r["success"] for r in b) and
              any(r["server"] == "srv1" for r in b), str(stats(b)))

        # Meaningful failure #2: Mininet link goes down, independent of process.
        print("\n[INJECT FAILURE] Mininet s1 <-> srv2 link DOWN", flush=True)
        net.configLinkStatus("s1", "srv2", "down")
        link_srv2_down = True
        check("D2-12 controller detects disconnected srv2",
              wait_health({"srv2": False}, 16))
        b = batch(net, "srv2_link_down", "adaptive", records,
                  "medium.bin", 8, 1)
        check("D2-13 no requests sent to broken srv2 link", len(b) == 8 and
              all(r["success"] and r["server"] != "srv2" for r in b),
              str(stats(b)))
        print("\n[RECOVERY] Mininet s1 <-> srv2 link UP", flush=True)
        net.configLinkStatus("s1", "srv2", "up")
        link_srv2_down = False
        check("D2-14 controller detects restored srv2 link",
              wait_health({"srv2": True}, 16))
        b = batch(net, "srv2_link_restored", "adaptive", records,
                  "small.txt", 9, 1)
        check("D2-15 restored srv2 receives requests", len(b) == 9 and
              all(r["success"] for r in b) and
              any(r["server"] == "srv2" for r in b), str(stats(b)))
    except Exception as exc:
        check("D2 execution completed", False, repr(exc))
    finally:
        if link_srv2_down:
            net.configLinkStatus("s1", "srv2", "up")
        if srv1_stopped:
            start_server("srv1")
        try:
            switch_mode("adaptive")
        except Exception as exc:
            check("restore adaptive policy", False, repr(exc))
        csv_path, md_path = write_results(records, checks, basename)
        print("\nD2 CSV: %s\nD2 report: %s" % (csv_path, md_path), flush=True)
        print("D2 RESULT: %d/%d checks passed" % (
            sum(ok for _, ok, _ in checks), len(checks)), flush=True)
    return bool(checks) and all(ok for _, ok, _ in checks)
