# D2 viva and three-member responsibility split

Use this alongside `README.md` and your *actual* `results/d2_*.md` after running the demo.

## Roles for a team of three

| Member | Owns | Must demonstrate/explain |
|---|---|---|
| 1 — Socket/application | `content_server.py`, `content_client.py`, replicated data | TCP `LIST`/`GET`, file integrity/bytes, concurrent clients, UDP heartbeat generation and the `active` load field |
| 2 — Mininet/SDN | `topology.py`, `sdn_content_controller.py`, `selection_policy.py` | VIP/ARP and forward/reverse OpenFlow rewrite, failure detection, stale-rule deletion, adaptive selection, host/link topology |
| 3 — D2 evaluation/integration | `d2_evaluation.py`, `sdn_admin.py`, test suite, README/results | Static baseline comparison, process kill + link down, recovery, CSV/latency/throughput/success rates, reproducible GitHub demo |

**Everyone should know:** the packet journey, why D1 and D2 are different,
what failure means for a new vs existing TCP connection, and the limitations.

## Ten viva questions and short answers

1. **Why an SDN controller?** It makes routing/selection decisions separately
   from the data plane and programs OVS with OpenFlow rules.
2. **Why the virtual IP?** Clients keep one destination even when the actual
   selected server changes; OpenFlow translates addresses transparently.
3. **Why TWO service flows?** Forward packets need the destination rewritten
   to a real server; reverse packets need source rewritten to VIP so the TCP
   client sees a consistent remote endpoint.
4. **Why UDP for heartbeats?** Periodic small, one-way status messages are
   lightweight; a missed packet is tolerated until the timeout.
5. **Why TCP for content?** Reliable ordered bytes are needed for file transfer.
6. **How does failure detection work?** No heartbeat for six seconds means
   DOWN; a switch port-down event can mark a disconnected link immediately.
7. **What does the controller do when a server dies?** It removes both
   directions of the old OpenFlow connections and excludes that server from
   future adaptive selections.
8. **Does an active TCP file download survive server death?** No. An existing
   connection drops; a NEW or retried connection can choose another server.
9. **What is the comparison baseline?** Static pins the service to srv1 even
   during failure; adaptive chooses a recent-heartbeat backend with lower
   reported active load.
10. **How did you measure D2?** The client records successful transfers,
    elapsed time, delivered bytes, and goodput. The evaluation reports success
    rate, mean and P95 time, mean goodput and backend distribution for healthy
    and failure scenarios. Results must be measured on the real VM.

## Recommended live demo order

1. Show source code and `README.md` architecture.
2. Start OS-Ken in **Terminal 1**.
3. Run `sudo python3 topology.py --test --d2-test` in **Terminal 2**.
4. Show D1 successes and the load-sharing responses.
5. Point out `srv1 DOWN`, stale-flow deletion and static failure.
6. Point out adaptive recovery to `srv2`/`srv3`.
7. Show process restoration, `srv2` link down/up and the restored service.
8. Open real `results/d2_*.csv` / `results/d2_*.md` and explain the comparisons.
9. Show `ovs-ofctl -O OpenFlow13 dump-flows s1` and point to `set_field`.
