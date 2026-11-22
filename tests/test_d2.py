"""Run without Mininet: python3 -m unittest discover -s tests -v"""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from selection_policy import select_server
import sdn_config as cfg
import sdn_admin
from content_server import ContentServer
from content_client import request


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.servers = cfg.SERVERS
        self.state = {s["name"]: {"last_seen": 999, "active": 0, "pending": 0}
                      for s in self.servers}

    def choose(self, mode, rr=0):
        return select_server(self.servers, self.state, mode, rr,
                             self.now, cfg.HEARTBEAT_TIMEOUT)

    def test_adaptive_balances_equal_load(self):
        selected = [self.choose("adaptive", rr=i)["name"] for i in range(9)]
        self.assertEqual(set(selected), {"srv1", "srv2", "srv3"})

    def test_no_dead_server(self):
        self.state["srv1"]["last_seen"] = 990
        selected = [self.choose("adaptive", rr=i)["name"] for i in range(9)]
        self.assertNotIn("srv1", selected)
        self.assertEqual(set(selected), {"srv2", "srv3"})

    def test_all_down_not_sent_to_dead_server(self):
        for s in self.state.values():
            s["last_seen"] = 0
        self.assertIsNone(self.choose("adaptive"))

    def test_static_baseline_fails_over_never(self):
        self.state["srv1"]["last_seen"] = 0
        self.assertEqual(self.choose("static")["name"], "srv1")

    def test_actual_active_load_overrides_rr(self):
        self.state["srv1"]["active"] = 5
        self.state["srv2"]["pending"] = 3
        self.assertEqual(self.choose("adaptive")["name"], "srv3")

    def test_recovered_server_returns(self):
        self.state["srv1"]["last_seen"] = 0
        self.assertNotIn("srv1", [self.choose("adaptive", rr=i)["name"]
                                  for i in range(6)])
        self.state["srv1"]["last_seen"] = self.now
        self.assertIn("srv1", [self.choose("adaptive", rr=i)["name"]
                                for i in range(6)])


class AdminTests(unittest.TestCase):
    def test_revisioned_policy_status(self):
        with tempfile.TemporaryDirectory() as root:
            policy = os.path.join(root, "mode.json")
            status = os.path.join(root, "status.json")
            with mock.patch.object(sdn_admin, "MODE_FILE", policy), \
                 mock.patch.object(sdn_admin, "STATUS_FILE", status):
                revision = sdn_admin.set_mode("static")
                with open(policy) as fh:
                    self.assertEqual(json.load(fh)["mode"], "static")
                sdn_admin.atomic_json(status, {"mode": "static",
                                               "revision": revision,
                                               "timestamp": time.time()})
                self.assertTrue(sdn_admin.wait_for_mode("static", revision, .2))


class SocketTests(unittest.TestCase):
    def test_real_tcp_socket_file_transfer(self):
        with tempfile.TemporaryDirectory() as root:
            cfg.ensure_content(root)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen(5)
            server = ContentServer("srv1", "127.0.0.1", port, root,
                                   "127.0.0.1", 9999)

            def accept_connections():
                for _ in range(2):
                    conn, addr = listener.accept()
                    server.handle(conn, addr)

            t = threading.Thread(target=accept_connections, daemon=True)
            t.start()
            r1 = request("127.0.0.1", port, "LIST", 2)
            r2 = request("127.0.0.1", port, "GET medium.bin", 2)
            self.assertIn(b"medium.bin", r1["body"])
            self.assertEqual(r2["bytes"], 262144)
            self.assertEqual(r2["server"], "srv1")
            self.assertEqual(r2["body"], bytes(range(256)) * 1024)
            listener.close()
            t.join(timeout=1)


if __name__ == "__main__":
    unittest.main()

# The actual OS-Ken/Mininet packages are not in this test environment. This
# narrow stub verifies our controller constructs the correct DELETE_STRICT
# requests; real integration still requires the Ubuntu Mininet demo.
class ControllerFlowTests(unittest.TestCase):
    @staticmethod
    def load_controller():
        import importlib
        import sys
        import types
        def mk(name, **members):
            module = types.ModuleType(name)
            module.__dict__.update(members)
            return module
        names = {
            "os_ken": mk("os_ken"),
            "os_ken.base": mk("os_ken.base", app_manager=types.SimpleNamespace(
                RyuApp=type("RyuApp", (), {"__init__": lambda self, *a, **k: setattr(
                    self, "logger", mock.Mock())}))),
            "os_ken.controller": mk("os_ken.controller", ofp_event=types.SimpleNamespace(
                EventOFPSwitchFeatures=object(), EventOFPPacketIn=object(),
                EventOFPPortStatus=object(), EventOFPFlowRemoved=object())),
            "os_ken.controller.handler": mk("os_ken.controller.handler",
                CONFIG_DISPATCHER="cfg", MAIN_DISPATCHER="main",
                set_ev_cls=lambda *a, **kw: lambda f: f),
            "os_ken.lib": mk("os_ken.lib", hub=types.SimpleNamespace(
                spawn=lambda *a, **kw: None, sleep=lambda t: None)),
            "os_ken.lib.packet": mk("os_ken.lib.packet",
                arp=types.SimpleNamespace(),
                ether_types=types.SimpleNamespace(ETH_TYPE_IP=0x0800),
                ethernet=types.SimpleNamespace(), ipv4=types.SimpleNamespace(),
                packet=types.SimpleNamespace(),
                tcp=types.SimpleNamespace(TCP_SYN=2, TCP_ACK=16),
                udp=types.SimpleNamespace()),
            "os_ken.ofproto": mk("os_ken.ofproto",
                ofproto_v1_3=types.SimpleNamespace(OFP_VERSION=4))
        }
        # Using an import patch here keeps controller tests dependency-free.
        with mock.patch.dict(sys.modules, names):
            sys.modules.pop("sdn_content_controller", None)
            return importlib.import_module("sdn_content_controller")

    def test_failure_deletes_both_forward_and_reverse_openflow_rules(self):
        ctl_module = self.load_controller()
        from types import SimpleNamespace
        class Parser:
            @staticmethod
            def OFPMatch(**kw):
                return kw
            @staticmethod
            def OFPFlowMod(**kw):
                return kw
        class DP:
            id = 7
            ofproto_parser = Parser()
            ofproto = SimpleNamespace(OFPFC_DELETE_STRICT=4, OFPP_ANY=4294967295,
                                      OFPG_ANY=4294967295)
            def __init__(self):
                self.messages = []
            def send_msg(self, m):
                self.messages.append(m)
        dp = DP()
        ctl = ctl_module.ContentDeliveryController()
        ctl.datapaths[7] = dp
        server = cfg.SERVERS[0]
        ctl.flow_map[(7, "10.0.0.1", 42000)] = {
            "server": server, "in_port": 1, "cookie": 1}
        ctl.flow_map[(7, "10.0.0.2", 43000)] = {
            "server": cfg.SERVERS[1], "in_port": 2, "cookie": 2}
        removed = ctl._purge("test", "srv1")
        self.assertEqual(removed, 2)
        self.assertEqual(len(dp.messages), 2)
        self.assertTrue(all(x["command"] == 4 and x["priority"] == 10
                            for x in dp.messages))
        self.assertEqual(dp.messages[0]["match"]["ipv4_dst"], cfg.VIP)
        self.assertEqual(dp.messages[1]["match"]["ipv4_src"], "10.0.0.11")
        self.assertEqual(len(ctl.flow_map), 1)

    def test_stale_flow_removed_cookie_doesnt_delete_new_binding(self):
        ctl_module = self.load_controller()
        from types import SimpleNamespace
        ctl = ctl_module.ContentDeliveryController()
        key = (1, "10.0.0.1", 42000)
        ctl.flow_map[key] = {"server": cfg.SERVERS[1], "in_port": 1,
                             "cookie": 2}
        msg = SimpleNamespace(priority=10, datapath=SimpleNamespace(id=1),
                              match={"ipv4_dst": cfg.VIP,
                                     "ipv4_src": "10.0.0.1", "tcp_src": 42000},
                              cookie=1)
        ctl.flow_removed_handler(SimpleNamespace(msg=msg))
        self.assertIn(key, ctl.flow_map)
