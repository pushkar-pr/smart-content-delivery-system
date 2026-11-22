"""Pure decision logic; imported by the OpenFlow controller and unit tests."""


def select_server(servers, state, mode, rr, now, timeout):
    """Select the target for a NEW TCP connection, or None if none are alive.

    static: deliberately pins all connections to srv1, even if it fails.
            This is the no-failover baseline for an honest D2 comparison.
    adaptive: only living servers; fewest active+pending connections, with
              round-robin tie-breaking so equally loaded servers share work.
    """
    if not servers:
        return None
    if mode == "static":
        return servers[0]
    living = [s for s in servers
              if now - state[s["name"]]["last_seen"] <= timeout
              and state[s["name"]]["last_seen"] > 0]
    if not living:
        return None
    return min(living, key=lambda s: (
        state[s["name"]]["active"] + state[s["name"]]["pending"],
        (servers.index(s) - rr) % len(servers)))
