# Smart Content Delivery System — Project 19 (D1 + D2)

**SDN + Python TCP/UDP sockets + Mininet + OpenFlow 1.3**

## 1. Problem and objectives

Many replicated content servers provide the same files. A client should access one
virtual IP without knowing which backend is chosen. The SDN controller decides
which server receives **each new TCP connection** and installs OpenFlow rewriting
rules. Each content server reports its load and health via UDP heartbeats.

**D1 objective:** prove end-to-end file transfer through the VIP and demonstrate
app-to-controller heartbeat integration.

**D2 objective:** react to backend or link failures, stop using failed servers,
clean stale data-plane rules, recover when servers/links return, and compare the
adaptive controller with a deliberately fixed/static baseline using measurements.

## 2. Network and communication flow

```text
 h1 10.0.0.1 ───┐                    ┌── srv1 10.0.0.11 (100 Mbps,  2 ms)
                ├── s1 (OVS/OF1.3) ──┼── srv2 10.0.0.12 ( 50 Mbps,  5 ms)
 h2 10.0.0.2 ───┘         │          └── srv3 10.0.0.13 ( 20 Mbps, 10 ms)
                          │
                 SDN controller on host
                    TCP port 6653

 Clients connect only to VIP = 10.0.0.100:5000 (virtual MAC 00:00:00:00:01:00)
```

1. Client ARPs for `10.0.0.100`; the controller replies with virtual MAC.
2. Client opens a TCP connection to VIP:5000; the first SYN reaches the controller.
3. Controller selects a backend based on policy, health and reported load.
4. Two priority-10 OpenFlow rules rewrite destination IP/MAC on the forward path
   and source IP/MAC on the return path. The client continues seeing VIP.
5. Files travel over ordinary TCP data-plane flows; UDP server heartbeats are
   handled by the controller and used to select servers.

The application protocol is:

```text
Client: LIST\n     -> Server: OK <nbytes> <server-name>\n<file listing>
Client: GET name\n -> Server: OK <nbytes> <server-name>\n<file bytes>
Client: QUIT\n
Errors: ERR <code> <message>\n
```

## 3. What's inside

| File | Purpose |
|---|---|
| `sdn_config.py` | VIP, switch ports, 2 clients, 3 server IPs, heartbeat settings |
| `content_server.py` | Concurrent TCP file server + UDP heartbeat of `active` and `served` |
| `content_client.py` | TCP client; requests via VIP; records response time / throughput |
| `sdn_content_controller.py` | OS-Ken/Ryu OpenFlow controller; VIP, flow creation, failure detection & stale-rule deletion |
| `topology.py` | Starts Mininet + servers, D1 tests, **D2 failure simulations** |
| `selection_policy.py` | Isolated, tested static and adaptive selection algorithms |
| `sdn_admin.py` | Changes baseline/adaptive mode; reports controller health/status |
| `d2_evaluation.py` | Runs honest A/B experiments, server kill, link-down, recovery, CSV and Markdown reports |
| `tests/test_d2.py` | Unit and real local TCP tests that don't require Mininet |

`runtime/`, `logs/`, `content/` and `results/` are generated when running.

## 4. D2 functionality: how it ACTUALLY works

**Adaptive (SDN) mode:** A UDP heartbeat arrives from every server approximately
once per 2 seconds. The controller considers a server down if there is no
heartbeat for **6 seconds**, or earlier if an OpenFlow port-down event arrives.
It selects only alive servers, minimizing `active + pending` connections and
breaking equal-load ties round-robin. This selection results in real `set_field`
OpenFlow actions in switch `s1`.

**On failure:** The controller identifies bindings that targeted the failed
backend and sends **two `OFPFC_DELETE_STRICT` FlowMods per connection** (forward
and reverse). New client connections are consequently redirected to healthy
backends. If all three servers are down, adaptive mode makes no backend selection
instead of routing to a known-dead server.

**On recovery:** Once heartbeats resume (e.g. after a server restart or a Mininet
link is brought up), the server becomes eligible again for new requests.

**Static baseline:** Pins all new TCP connections to `srv1`, even when it dies.
This intentionally represents a traditional fixed-backend approach with
*no* fault-aware failover, enabling a useful comparison with the adaptive mode.
The benchmark never pretends static can recover from a dead `srv1`.

**Important limit:** TCP connections already attached to a server cannot be
transferred live to another server after that server disappears. New TCP
connections (or application retries) can be sent to another server. This is
failover, **not** transparent migration of an in-progress TCP download.

## 5. Set up (Ubuntu VM / two terminals)

The example assumes this project folder is `~/Downloads/sdn-content-delivery`.
Adjust `cd` to match where you extract the ZIP. Mininet, Open vSwitch and
Python 3 must already be installed. For missing system dependencies:

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch iputils-ping python3
```

Run OS-Ken in your existing environment if installed, e.g. the
`~/osken-venv` virtual environment. The source also supports `ryu-manager`
when Ryu is installed, but **OS-Ken is the recommended choice** here.

### Terminal 1 — controller (start FIRST)

```bash
cd ~/Downloads/sdn-content-delivery
~/osken-venv/bin/osken-manager --ofp-tcp-listen-port 6653 sdn_content_controller.py
```

Keep this controller running. Look for `[SDN]`, `[HEALTH]`, `[LB]`,
`[FAILOVER]` and `[POLICY]` output.

### Terminal 2 — automatic D1 + D2 test and failure demo

```bash
cd ~/Downloads/sdn-content-delivery
sudo mn -c
sudo python3 topology.py --test --d2-test
```

This runs D1 checks followed by all D2 experiments. **No third terminal is
needed.** After the tests the Mininet CLI stays open (unless you add `--no-cli`).
The benchmark creates files in `results/`. Use `exit` at `mininet>` to stop.

To run only D2 and exit automatically:

```bash
sudo python3 topology.py --d2-test --no-cli
```

Do **not** start another copy of Mininet while a CLI is already running.
The D2 test deliberately kills only the selected backend PID, not every
content-server process. Its `finally` section restores failed links and the
adaptive policy, even if a test encounters an error.

## 6. D2 test plan (mapped to the evaluation criteria)

| Case | Real action | What the assessor should observe |
|---|---|---|
| D2-1 | Check all 3 server heartbeats | `srv1`, `srv2`, `srv3` UP in controller state |
| D2-2 | **Static** mode: 8 concurrent large-file requests | All requests go to `srv1`; baseline latency/throughput collected |
| D2-3 | **Adaptive** mode: same 8 requests | New requests are distributed; latency and throughput collected |
| D2-4/5/6 | First install a VIP rule to `srv1`; then kill **srv1 process** | Confirm rule existed; controller detects DOWN; `purged_flows` increases and the stale rule disappears |
| D2-7 | Keep failed `srv1` with **static** policy | Request failures demonstrate the fixed-backend limitation |
| D2-8/9 | Same failed `srv1` with **adaptive** policy | New requests complete using `srv2`/`srv3`; switch has alternate VIP rewrite flow |
| D2-10/11 | Restart `srv1` | Heartbeats return; new connections again reach `srv1` |
| D2-12/13 | `net.configLinkStatus("s1", "srv2", "down")` | Detects link failure and avoids `srv2` |
| D2-14/15 | Bring `srv2` link back up | `srv2` becomes eligible again |

The script checks each event and prints `[PASS]` or `[FAIL]`, then produces:

- `results/d2_<timestamp>.csv`: one row per actual request, including failures.
- `results/d2_<timestamp>.md`: per-scenario success rate, mean response time,
  nearest-rank P95 response time, mean goodput (Mbps), backend distribution,
  assertions and measurement caveats.

**Do not fill these in with invented numbers:** run the tests in Mininet and
include the generated report and screenshots in the submitted GitHub repository.

## 7. D1 demo and manual commands

From `mininet>` (after running `sudo python3 topology.py`):

```text
h1 python3 content_client.py --list
h1 python3 content_client.py --file medium.bin
h1 python3 content_client.py --file large.bin --count 6 --parallel 2
h2 python3 content_client.py --file small.txt --count 3
sh ovs-ofctl -O OpenFlow13 dump-flows s1
sh python3 sdn_admin.py status
sh python3 sdn_admin.py static
sh python3 sdn_admin.py adaptive
```

To simulate a link failure manually, from `mininet>`:

```text
link s1 srv2 down
sh python3 sdn_admin.py status
h1 python3 content_client.py --file medium.bin --count 6
link s1 srv2 up
```

Allow up to ~6–7 seconds after a link failure for heartbeat timeout if the
OpenFlow port-down event doesn't arrive first. For a complete **server process
failure and restart**, use `--d2-test`: it safely manages the PIDs automatically.

If `h1` cannot locate scripts, first ensure you started `topology.py` from the
project directory (as in Terminal 2 commands). If `--no-shaping` is needed
because `tc`/netem is unavailable, the network will not retain the configured
bandwidth/delay variations, so disclose that in the performance report.

## 8. Rubric mapping (D1 15 + D2 25 = 40 marks)

### D1 (15)

| Component | Marks | Show |
|---|---:|---|
| Problem understanding and architecture | 2 | Section 2 topology/VIP packet journey |
| TCP/UDP socket implementation | 4 | `content_client.py` & `content_server.py`: TCP file transfer, threading, UDP heartbeat |
| Mininet + SDN implementation | 4 | `topology.py`, OpenFlow 1.3 switch and `set_field` flows |
| Socket–SDN integration | 3 | UDP heartbeat changes server eligibility; SYN Packet-In installs VIP flow |
| Initial testing and demo | 2 | `--test` six D1 test results |

### D2 (25)

| Component | Marks | Show |
|---|---:|---|
| Complete application functionality | 4 | 2 clients, 3 replicated servers, file LIST/GET protocol, concurrent transfers, errors |
| Dynamic SDN functionality | 6 | Heartbeat-aware load sharing, port-down event, failure-driven OpenFlow rule deletion, recovery |
| Challenging scenario / robustness | 4 | Real `srv1` process death **and** `srv2` physical Mininet link-down, both with recovery |
| Performance evaluation & comparison | 5 | Static vs adaptive, healthy vs failed scenarios, generated CSV/report with response time, goodput, success rates |
| Documentation, final demo & viva | 6 | This README, tests, reproducible two-terminal commands, GitHub repo, screenshots |

**Suggested evidence screenshots:**

1. `--test` D1 output and a successful `GET` on the VIP.
2. Controller log with `[LB]` distributing connections and server `[HEALTH] UP`.
3. Controller log `[HEALTH] srv1 DOWN` and `[FAILOVER] ... removed ... rules`.
4. D2 console with the static failure and adaptive success immediately after.
5. Mininet OpenFlow dump showing `set_field` actions to surviving servers.
6. D2 CSV/Markdown summary tables and successful server/link restoration.

## 9. Local code checks

These work on any machine with Python 3, including without Mininet:

```bash
python3 -m compileall -q .
python3 -m unittest discover -s tests -v
```

They test policy and local TCP socket transfers. **They do not replace the
real OpenFlow/Mininet end-to-end test.**

## 10. Troubleshooting and limitations

- Controller does not acknowledge adaptive/static mode: start OS-Ken before
  Mininet; check write permissions on `runtime/` (the Mininet launcher attempts
  to preserve ownership for the original `sudo` user).
- No `HEALTH UP`: check `logs/srv1.log`, `logs/srv2.log` and `logs/srv3.log`.
- Missing VIP rules: check controller is connected on TCP 6653 and inspect
  `ovs-ofctl -O OpenFlow13 dump-flows s1`.
- Some requests fail while a server is **just** being stopped: the detection
  window is up to 6 seconds. Run requests after the controller confirms DOWN.
- A current TCP download will not migrate to a new backend; new connections
  must be opened for recovery. This is expected and documented.
- `runtime/` stores local administrative state on the **same Ubuntu VM** as the
  controller. It is a local control interface, not an exposed remote API.
- The D2 benchmark is a classroom experiment on a VM, not a claim that one
  algorithm always outperforms the other. Keep the actual results and note
  scheduling/virtualization noise.
