#!/usr/bin/env python3
"""
Content server (TCP) - runs on srv1 / srv2 / srv3.

Application protocol (line based, over TCP):
    client -> server : "LIST\n"            | "GET <filename>\n"  | "QUIT\n"
    server -> client : "OK <size> <srv>\n" + <size> bytes of payload
                       "ERR <code> <msg>\n"

Concurrency : one thread per client connection.
SDN link    : every HEARTBEAT_INTERVAL seconds the server broadcasts a small
              JSON heartbeat (UDP) {name, active, served}. The switch has no
              flow for it, so it reaches the SDN controller as a Packet-In.
              This is how the application tells the network "I am alive and
              this is my load".
"""
import argparse
import json
import os
import socket
import threading
import time

from sdn_config import (HEARTBEAT_ADDR, HEARTBEAT_INTERVAL, HEARTBEAT_PORT,
                        SERVICE_PORT, ensure_content)

CHUNK = 64 * 1024


class ContentServer:
    def __init__(self, name, bind_ip, port, content_dir, hb_addr, hb_port, pid_file=None):
        self.name = name
        self.bind_ip = bind_ip
        self.port = port
        self.content_dir = content_dir
        self.hb_addr = hb_addr
        self.hb_port = hb_port
        self.pid_file = pid_file
        self.active = 0      # connections being served right now
        self.served = 0      # files served so far
        self.lock = threading.Lock()

    def log(self, msg):
        print("[%s] %s %s" % (self.name, time.strftime("%H:%M:%S"), msg), flush=True)

    # ------------------------------------------------------------------ heartbeat
    def heartbeat_loop(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        warned = False
        while True:
            with self.lock:
                msg = {"name": self.name, "active": self.active,
                       "served": self.served, "ts": time.time()}
            try:
                s.sendto(json.dumps(msg).encode(), (self.hb_addr, self.hb_port))
                warned = False
            except OSError as e:
                if not warned:
                    self.log("heartbeat send failed: %s" % e)
                    warned = True
            time.sleep(HEARTBEAT_INTERVAL)

    # ------------------------------------------------------------------ requests
    def send_list(self, conn):
        names = sorted(f for f in os.listdir(self.content_dir)
                       if os.path.isfile(os.path.join(self.content_dir, f)))
        body = ("\n".join(names) + "\n").encode()
        conn.sendall(("OK %d %s\n" % (len(body), self.name)).encode() + body)

    def send_file(self, conn, fname, addr):
        safe = os.path.basename(fname)             # block ../ path tricks
        path = os.path.join(self.content_dir, safe)
        if not os.path.isfile(path):
            conn.sendall(b"ERR 404 file not found\n")
            self.log("404 %s requested by %s" % (safe, addr[0]))
            return
        size = os.path.getsize(path)
        conn.sendall(("OK %d %s\n" % (size, self.name)).encode())
        with open(path, "rb") as f:
            while True:
                data = f.read(CHUNK)
                if not data:
                    break
                conn.sendall(data)
        with self.lock:
            self.served += 1
        self.log("served %s (%d bytes) to %s:%d" % (safe, size, addr[0], addr[1]))

    def handle(self, conn, addr):
        with self.lock:
            self.active += 1
        self.log("connection from %s:%d" % addr)
        try:
            conn.settimeout(30)
            f = conn.makefile("rb")
            while True:
                line = f.readline()
                if not line:
                    break
                parts = line.decode(errors="replace").strip().split(None, 1)
                if not parts:
                    continue
                cmd = parts[0].upper()
                if cmd == "LIST":
                    self.send_list(conn)
                elif cmd == "GET" and len(parts) == 2:
                    self.send_file(conn, parts[1].strip(), addr)
                elif cmd == "QUIT":
                    break
                else:
                    conn.sendall(b"ERR 400 bad request\n")
        except (socket.timeout, OSError) as e:
            self.log("connection %s:%d error: %s" % (addr[0], addr[1], e))
        finally:
            conn.close()
            with self.lock:
                self.active -= 1

    # ------------------------------------------------------------------ main loop
    def run(self):
        ensure_content(self.content_dir)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind_ip, self.port))
        srv.listen(128)
        # Used by the D2 demo to kill precisely this backend, not all servers.
        if self.pid_file:
            with open(self.pid_file, "w") as f:
                f.write(str(os.getpid()) + "\n")
        self.log("listening on %s:%d, content dir '%s'" %
                 (self.bind_ip, self.port, self.content_dir))
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self.handle, args=(conn, addr),
                             daemon=True).start()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TCP content server")
    ap.add_argument("--name", required=True, help="server name, e.g. srv1")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=SERVICE_PORT)
    ap.add_argument("--content-dir", default="content")
    ap.add_argument("--hb-addr", default=HEARTBEAT_ADDR)
    ap.add_argument("--hb-port", type=int, default=HEARTBEAT_PORT)
    ap.add_argument("--pid-file", help="optional PID file for failure simulation")
    a = ap.parse_args()
    try:
        ContentServer(a.name, a.bind, a.port, a.content_dir,
                      a.hb_addr, a.hb_port, a.pid_file).run()
    except KeyboardInterrupt:
        pass
