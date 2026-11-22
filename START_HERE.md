# Start here — D1 + D2 (two Ubuntu terminals)

**Terminal 1** (controller, must run first):

```bash
cd ~/Downloads/sdn-content-delivery
~/osken-venv/bin/osken-manager --ofp-tcp-listen-port 6653 sdn_content_controller.py
```

**Terminal 2** (tests, actual server-kill and link-down simulations):

```bash
cd ~/Downloads/sdn-content-delivery
sudo mn -c
sudo python3 topology.py --test --d2-test
```

Look for `[PASS]` / `[FAIL]` and the `results/d2_<timestamp>.md` and `.csv`
files. The tests restore servers, links and adaptive policy afterward. When the
Mininet CLI opens, type `exit` to end the network.

The first time, read `README.md` for environment setup, screenshots, limitations,
all D1/D2 rubric mappings and troubleshooting. No test results are prefilled:
**you must run the real Mininet demo in Ubuntu**.
