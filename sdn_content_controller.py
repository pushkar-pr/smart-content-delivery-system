"""Smart Content Delivery SDN controller (OpenFlow 1.3), D1 + D2.

D1: VIP ARP, per-TCP-connection destination/source rewriting, UDP heartbeat
    monitoring, load-aware selection and ordinary L2 forwarding.
D2: failure-aware selection, deletion of both directions of failed-server
    flows, fast handling of OpenFlow port-down events, return-to-service,
    runtime adaptive/static policy, and observable controller state.

Existing TCP connections cannot migrate after a server dies; only NEW (or
client-retried) connections can fail over. The D2 demo tests that behavior.

Run: ~/osken-venv/bin/osken-manager --ofp-tcp-listen-port 6653 sdn_content_controller.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from os_ken.base import app_manager
    from os_ken.controller import ofp_event
    from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
    from os_ken.lib import hub
    from os_ken.lib.packet import arp, ether_types, ethernet, ipv4, packet, tcp, udp
    from os_ken.ofproto import ofproto_v1_3
except ImportError:  # preserve compatibility with an existing Ryu installation
    from ryu.base import app_manager
    from ryu.controller import ofp_event
    from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
    from ryu.lib import hub
    from ryu.lib.packet import arp, ether_types, ethernet, ipv4, packet, tcp, udp
    from ryu.ofproto import ofproto_v1_3

import sdn_config as cfg
from sdn_admin import MODE_FILE, STATUS_FILE, atomic_json
from selection_policy import select_server

FLOW_PRIORITY_SERVICE = 10
FLOW_PRIORITY_L2 = 1
FLOW_IDLE_TIMEOUT = 25


class ContentDeliveryController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(ContentDeliveryController, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        # (dpid, client_ip, source_tcp_port) -> {server, client_switch_port}
        self.flow_map = {}
        self.rr = 0
        self.mode = "adaptive"
        self.mode_revision = None
        self.purged_flows = 0
        self.cookie_sequence = 0
        self.state = {
            s["name"]: {"active": 0, "pending": 0, "last_seen": 0.0,
                        "alive": False, "assigned": 0}
            for s in cfg.SERVERS
        }
        hub.spawn(self._monitor)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        self._add_flow(dp, 0, dp.ofproto_parser.OFPMatch(), [
            dp.ofproto_parser.OFPActionOutput(
                dp.ofproto.OFPP_CONTROLLER, dp.ofproto.OFPCML_NO_BUFFER)])
        self.logger.info("[SDN] switch %s connected; VIP=%s:%d", dp.id,
                         cfg.VIP, cfg.SERVICE_PORT)

    def _add_flow(self, dp, priority, match, actions, idle=0, flags=0, cookie=0):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority,
                                      match=match, instructions=inst,
                                      idle_timeout=idle, flags=flags, cookie=cookie))

    def _delete_exact(self, dp, match):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, command=ofp.OFPFC_DELETE_STRICT,
            priority=FLOW_PRIORITY_SERVICE, match=match,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY))

    def _packet_out(self, dp, in_port, actions, data):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=in_port, actions=actions, data=data))

    def _is_alive(self, name):
        st = self.state[name]
        return (st["last_seen"] > 0 and
                time.time() - st["last_seen"] <= cfg.HEARTBEAT_TIMEOUT)

    def _match_forward(self, dp, client_ip, client_tcp_port, in_port):
        return dp.ofproto_parser.OFPMatch(
            in_port=in_port, eth_type=ether_types.ETH_TYPE_IP, ip_proto=6,
            ipv4_src=client_ip, ipv4_dst=cfg.VIP,
            tcp_src=client_tcp_port, tcp_dst=cfg.SERVICE_PORT)

    def _match_reverse(self, dp, client_ip, client_tcp_port, server):
        return dp.ofproto_parser.OFPMatch(
            in_port=server["port"], eth_type=ether_types.ETH_TYPE_IP, ip_proto=6,
            ipv4_src=server["ip"], ipv4_dst=client_ip,
            tcp_src=cfg.SERVICE_PORT, tcp_dst=client_tcp_port)

    def _purge(self, reason, server_name=None):
        """Remove *both* OpenFlow rules for dead/old destinations, not just Python state."""
        removed = 0
        for key, binding in list(self.flow_map.items()):
            if server_name and binding["server"]["name"] != server_name:
                continue
            dpid, client_ip, client_tcp_port = key
            dp = self.datapaths.get(dpid)
            if dp:
                self._delete_exact(dp, self._match_forward(
                    dp, client_ip, client_tcp_port, binding["in_port"]))
                self._delete_exact(dp, self._match_reverse(
                    dp, client_ip, client_tcp_port, binding["server"]))
                removed += 2
            self.flow_map.pop(key, None)
        if removed:
            self.purged_flows += removed
            self.logger.warning("[FAILOVER] %s: removed %d stale OpenFlow rules",
                                reason, removed)
        return removed

    def _load_mode(self):
        try:
            with open(MODE_FILE) as f:
                data = json.load(f)
            mode, revision = data["mode"], data["revision"]
            if mode not in ("static", "adaptive"):
                return
        except (OSError, ValueError, KeyError, TypeError):
            return
        if revision != self.mode_revision:
            previous = self.mode
            self.mode, self.mode_revision = mode, revision
            if previous != mode:
                self._purge("policy switched from %s to %s" % (previous, mode))
            self.logger.info("[POLICY] %s (revision=%s)", mode, revision)

    def _status(self):
        return {
            "timestamp": time.time(), "mode": self.mode,
            "revision": self.mode_revision,
            "vip": cfg.VIP, "switches": list(self.datapaths),
            "purged_flows": self.purged_flows,
            "connections_tracked": len(self.flow_map),
            "servers": {
                name: {"alive": self._is_alive(name),
                       "active": st["active"], "pending": st["pending"],
                       "assigned": st["assigned"]}
                for name, st in self.state.items()
            }}

    def _monitor(self):
        while True:
            self._load_mode()
            for name, st in self.state.items():
                alive = self._is_alive(name)
                if st["alive"] and not alive:
                    self.logger.warning("[HEALTH] %s DOWN (heartbeat timeout)", name)
                    self._purge("server %s DOWN" % name, name)
                st["alive"] = alive
            try:
                atomic_json(STATUS_FILE, self._status())
            except OSError as exc:
                self.logger.warning("Cannot write controller status: %s", exc)
            hub.sleep(0.5)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        """Track real flow lifetimes, including long-running TCP transfers.

        Only the forward flow requests removal notifications. Cookies avoid
        an old removal event discarding a newer connection that reused a port.
        """
        msg = ev.msg
        if msg.priority != FLOW_PRIORITY_SERVICE:
            return
        try:
            if msg.match["ipv4_dst"] != cfg.VIP:
                return
            key = (msg.datapath.id, msg.match["ipv4_src"], msg.match["tcp_src"])
        except KeyError:
            return
        binding = self.flow_map.get(key)
        if not binding or binding["cookie"] != msg.cookie:
            return
        self.flow_map.pop(key, None)
        # Reverse may still be installed; remove it when the forward expires.
        self._delete_exact(msg.datapath, self._match_reverse(
            msg.datapath, key[1], key[2], binding["server"]))

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        down = (msg.reason == msg.datapath.ofproto.OFPPR_DELETE or
                bool(msg.desc.state & msg.datapath.ofproto.OFPPS_LINK_DOWN))
        if not down:
            return
        for s in cfg.SERVERS:
            if msg.desc.port_no == s["port"]:
                self.state[s["name"]]["last_seen"] = 0
                self.state[s["name"]]["alive"] = False
                self._purge("OpenFlow port %d DOWN" % s["port"], s["name"])
                self.logger.warning("[HEALTH] %s DOWN (OpenFlow port %d)",
                                    s["name"], s["port"])

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        self.mac_to_port.setdefault(dp.id, {})[eth.src] = in_port

        req = pkt.get_protocol(arp.arp)
        if req is not None:
            if req.opcode == arp.ARP_REQUEST and req.dst_ip == cfg.VIP:
                self._reply_arp_for_vip(dp, in_port, req)
            else:
                self._l2_forward(dp, msg, in_port, eth)
            return

        ip = pkt.get_protocol(ipv4.ipv4)
        if ip is not None:
            u = pkt.get_protocol(udp.udp)
            if u is not None and u.dst_port == cfg.HEARTBEAT_PORT:
                self._handle_heartbeat(ip, u, msg.data)
                return
            t = pkt.get_protocol(tcp.tcp)
            if t is not None and ip.dst == cfg.VIP and t.dst_port == cfg.SERVICE_PORT:
                self._handle_service_packet(dp, msg, in_port, ip, t)
                return
        self._l2_forward(dp, msg, in_port, eth)

    def _reply_arp_for_vip(self, dp, port, req):
        parser = dp.ofproto_parser
        p = packet.Packet()
        p.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                         dst=req.src_mac, src=cfg.VMAC))
        p.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=cfg.VMAC,
                               src_ip=cfg.VIP, dst_mac=req.src_mac,
                               dst_ip=req.src_ip))
        p.serialize()
        self._packet_out(dp, dp.ofproto.OFPP_CONTROLLER,
                         [parser.OFPActionOutput(port)], p.data)

    def _handle_heartbeat(self, ip, udp_msg, data):
        offset = 14 + ip.header_length * 4 + 8
        payload_length = udp_msg.total_length - 8
        if payload_length < 1 or len(data) < offset + payload_length:
            return
        try:
            info = json.loads(data[offset: offset + payload_length].decode())
            name = info["name"]
        except (ValueError, KeyError, UnicodeDecodeError, TypeError):
            return
        server = next((s for s in cfg.SERVERS if s["name"] == name), None)
        if not server or server["ip"] != ip.src:
            return
        st = self.state[name]
        if not st["alive"]:
            self.logger.info("[HEALTH] %s UP (%s)", name, ip.src)
        st["alive"] = True
        st["last_seen"] = time.time()
        try:
            st["active"] = max(0, int(info.get("active", 0)))
        except (ValueError, TypeError):
            st["active"] = 0
        st["pending"] = 0

    def _select_server(self):
        self.rr += 1
        chosen = select_server(cfg.SERVERS, self.state, self.mode, self.rr,
                               time.time(), cfg.HEARTBEAT_TIMEOUT)
        if chosen is None:
            self.logger.warning("[SELECT] no live server; refuse new request")
        return chosen

    def _handle_service_packet(self, dp, msg, in_port, ip, t):
        key = (dp.id, ip.src, t.src_port)
        new_syn = bool(t.bits & tcp.TCP_SYN) and not bool(t.bits & tcp.TCP_ACK)
        binding = self.flow_map.get(key)
        # If a reused source port sends a new SYN, forget previous mapping.
        if new_syn and binding:
            self._delete_exact(dp, self._match_forward(
                dp, ip.src, t.src_port, binding["in_port"]))
            self._delete_exact(dp, self._match_reverse(
                dp, ip.src, t.src_port, binding["server"]))
            self.flow_map.pop(key, None)
            binding = None

        if binding is None:
            server = self._select_server()
            if server is None:
                return
            self.state[server["name"]]["pending"] += 1
            self.state[server["name"]]["assigned"] += 1
            self.cookie_sequence = (self.cookie_sequence + 1) % (2 ** 64)
            binding = {"server": server, "in_port": in_port,
                       "cookie": self.cookie_sequence}
            self.flow_map[key] = binding
            self.logger.info("[LB] mode=%s %s:%d -> VIP -> %s (%s)",
                             self.mode, ip.src, t.src_port, server["name"],
                             server["ip"])
        server = binding["server"]
        parser = dp.ofproto_parser
        match_fwd = self._match_forward(dp, ip.src, t.src_port, in_port)
        actions_fwd = [parser.OFPActionSetField(eth_dst=server["mac"]),
                       parser.OFPActionSetField(ipv4_dst=server["ip"]),
                       parser.OFPActionOutput(server["port"])]
        match_rev = self._match_reverse(dp, ip.src, t.src_port, server)
        actions_rev = [parser.OFPActionSetField(eth_src=cfg.VMAC),
                       parser.OFPActionSetField(ipv4_src=cfg.VIP),
                       parser.OFPActionOutput(in_port)]
        self._add_flow(dp, FLOW_PRIORITY_SERVICE, match_fwd,
                       actions_fwd, FLOW_IDLE_TIMEOUT,
                       flags=dp.ofproto.OFPFF_SEND_FLOW_REM,
                       cookie=binding["cookie"])
        self._add_flow(dp, FLOW_PRIORITY_SERVICE, match_rev,
                       actions_rev, FLOW_IDLE_TIMEOUT,
                       cookie=binding["cookie"])
        self._packet_out(dp, in_port, actions_fwd, msg.data)

    def _l2_forward(self, dp, msg, in_port, eth):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dest_port = self.mac_to_port[dp.id].get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(dest_port)]
        if dest_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=eth.dst, eth_src=eth.src)
            self._add_flow(dp, FLOW_PRIORITY_L2, match, actions, FLOW_IDLE_TIMEOUT)
        self._packet_out(dp, in_port, actions, msg.data)
