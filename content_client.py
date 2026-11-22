#!/usr/bin/env python3
"""
Content client (TCP) - runs on h1 / h2.

Always connects to the VIRTUAL IP. The client never knows which real server
answers; the SDN controller decides that. The reply header carries the name of
the server that actually served the request, so we can see the load balancing.

Examples (inside Mininet):
    h1 python3 content_client.py --list
    h1 python3 content_client.py --file large.bin
    h1 python3 content_client.py --file small.txt --count 9 --interval 0.2
    h2 python3 content_client.py --file medium.bin --count 5 --parallel 3 --csv results.csv
"""
import argparse
import socket
import sys
import threading
import time

from sdn_config import SERVICE_PORT, VIP

print_lock = threading.Lock()


def request(vip, port, command, timeout):
    """One TCP connection, one request. Returns dict with the results."""
    t0 = time.time()
    with socket.create_connection((vip, port), timeout=timeout) as s:
        src = "%s:%d" % s.getsockname()
        s.sendall((command + "\n").encode())
        f = s.makefile("rb")
        header = f.readline().decode().strip()
        if not header:
            raise ConnectionError("empty response")
        parts = header.split()
        if parts[0] != "OK":
            raise RuntimeError("server said: " + header)
        size = int(parts[1])
        server = parts[2] if len(parts) > 2 else "?"
        body = b""
        remaining = size
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                raise ConnectionError("connection closed early (%d/%d bytes)"
                                      % (size - remaining, size))
            body += chunk
            remaining -= len(chunk)
        s.sendall(b"QUIT\n")
    dt = time.time() - t0
    return {"src": src, "server": server, "bytes": size, "time": dt,
            "mbps": (size * 8 / 1e6) / dt if dt > 0 else 0.0, "body": body}


def worker(args, results):
    for i in range(args.count):
        cmd = "LIST" if args.list else "GET " + args.file
        try:
            r = request(args.vip, args.port, cmd, args.timeout)
            with print_lock:
                if args.list:
                    print("FILES on %s:\n%s" % (r["server"], r["body"].decode().rstrip()))
                print("RESULT src=%s file=%s bytes=%d server=%s time_s=%.4f throughput_mbps=%.2f"
                      % (r["src"], "LIST" if args.list else args.file, r["bytes"],
                         r["server"], r["time"], r["mbps"]), flush=True)
                if args.csv:
                    with open(args.csv, "a") as f:
                        f.write("%.3f,%s,%s,%d,%s,%.4f,%.2f\n" % (
                            time.time(), r["src"], args.file, r["bytes"],
                            r["server"], r["time"], r["mbps"]))
            results.append(True)
        except Exception as e:                      # noqa: BLE001
            with print_lock:
                print("FAIL file=%s reason=%s" % (args.file, e), flush=True)
            results.append(False)
        if i < args.count - 1:
            time.sleep(args.interval)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TCP content client")
    ap.add_argument("--vip", default=VIP)
    ap.add_argument("--port", type=int, default=SERVICE_PORT)
    ap.add_argument("--file", default="small.txt")
    ap.add_argument("--list", action="store_true", help="list available files")
    ap.add_argument("--count", type=int, default=1, help="requests per thread")
    ap.add_argument("--parallel", type=int, default=1, help="concurrent threads")
    ap.add_argument("--interval", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--csv", help="append results to this CSV file")
    a = ap.parse_args()

    res = []
    threads = [threading.Thread(target=worker, args=(a, res)) for _ in range(a.parallel)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sys.exit(0 if res and all(res) else 1)
