# Tests for aiab.netproxy.evaluate — the pure policy-matching core of the
# filtering proxy — and for is_external_address, the check that stands between
# an allowed *name* and an address pointing back at the host. The
# socket/parking machinery around them is exercised manually; this covers the
# verdict logic.

import aiab.state as state
from aiab.netproxy import ALLOW, ASK, DENY, evaluate, is_external_address


def _policy(mode=state.MODE_RESTRICTED, allow=(), deny=()):
    return {
        "mode": mode,
        "allow": [{"domain": d, "expires": None} for d in allow],
        "deny": list(deny),
    }


def test_open_mode_allows_everything():
    assert evaluate("anything.example", [], _policy(mode=state.MODE_OPEN)) == ALLOW


def test_api_domain_allowed():
    assert evaluate("anthropic.com", ["anthropic.com"], _policy()) == ALLOW


def test_api_subdomain_allowed():
    assert evaluate("api.anthropic.com", ["anthropic.com"], _policy()) == ALLOW


def test_allowlist_domain_and_subdomain():
    policy = _policy(allow=["github.com"])
    assert evaluate("github.com", [], policy) == ALLOW
    assert evaluate("api.github.com", [], policy) == ALLOW


def test_denylist_domain_and_subdomain():
    policy = _policy(deny=["tracker.example"])
    assert evaluate("tracker.example", [], policy) == DENY
    assert evaluate("cdn.tracker.example", [], policy) == DENY


def test_unknown_host_is_ask():
    assert evaluate("unknown.example", [], _policy(allow=["github.com"])) == ASK


def test_no_suffix_confusion():
    # evilgithub.com must not match a github.com rule.
    assert evaluate("evilgithub.com", [], _policy(allow=["github.com"])) == ASK


def test_most_specific_rule_wins():
    # An allow for a subdomain pokes a hole in a broader deny, and vice versa.
    policy = _policy(allow=["api.x.com"], deny=["x.com"])
    assert evaluate("api.x.com", [], policy) == ALLOW
    assert evaluate("v2.api.x.com", [], policy) == ALLOW
    assert evaluate("www.x.com", [], policy) == DENY

    policy = _policy(allow=["x.com"], deny=["telemetry.x.com"])
    assert evaluate("telemetry.x.com", [], policy) == DENY
    assert evaluate("www.x.com", [], policy) == ALLOW


def test_api_domains_beat_deny():
    # The agent's own API endpoints cannot be denied.
    policy = _policy(deny=["anthropic.com"])
    assert evaluate("api.anthropic.com", ["anthropic.com"], policy) == ALLOW


def test_host_normalisation():
    policy = _policy(allow=["github.com"])
    assert evaluate("GitHub.COM.", [], policy) == ALLOW


def test_global_allow_supplements_local():
    # A globally-allowed domain is allowed even with an empty local allowlist.
    glob = _policy(allow=["github.com"])
    assert evaluate("api.github.com", [], _policy(), glob) == ALLOW


def test_global_deny_supplements_local():
    glob = _policy(deny=["tracker.example"])
    assert evaluate("cdn.tracker.example", [], _policy(), glob) == DENY


def test_local_allow_overrides_global_deny_on_tie():
    # Same-length match: the directory's own rule wins over the global one.
    glob = _policy(deny=["x.com"])
    assert evaluate("x.com", [], _policy(allow=["x.com"]), glob) == ALLOW


def test_local_deny_overrides_global_allow_on_tie():
    glob = _policy(allow=["x.com"])
    assert evaluate("x.com", [], _policy(deny=["x.com"]), glob) == DENY


def test_longer_global_rule_beats_shorter_local_rule():
    # Most-specific-wins still holds across scopes: a longer global allow
    # pokes a hole in a shorter local deny.
    glob = _policy(allow=["api.x.com"])
    local = _policy(deny=["x.com"])
    assert evaluate("api.x.com", [], local, glob) == ALLOW
    assert evaluate("www.x.com", [], local, glob) == DENY


def test_global_ignored_in_open_mode():
    glob = _policy(deny=["github.com"])
    assert evaluate("github.com", [], _policy(mode=state.MODE_OPEN), glob) == ALLOW


# -- ProxyServer wiring (agent axis) --

from aiab import agents  # noqa: E402
from aiab.netproxy import ProxyServer  # noqa: E402


def test_proxy_derives_api_domains_from_agent(tmp_path):
    srv = ProxyServer("\0aiab-test-derive", tmp_path, "claude")
    try:
        expected = {
            d.lower()
            for d in agents.BASELINE_DOMAINS + agents.get("claude").api_domains
        }
        assert set(srv.api_domains) == expected
    finally:
        srv.server_close()


def test_proxy_decide_honors_per_agent_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_NET_PATH", tmp_path / "network.json")
    work = tmp_path / "proj"
    work.mkdir()
    state.set_network_mode(work, state.MODE_RESTRICTED)
    state.add_network_allow(work, "mcp.example", expires=None, agent="opencode")

    opencode = ProxyServer("\0aiab-test-oc", work, "opencode")
    claude = ProxyServer("\0aiab-test-cl", work, "claude")
    try:
        # The rule applies to opencode only; claude has to ask.
        assert opencode.decide("mcp.example") == ALLOW
        assert claude.decide("mcp.example") == ASK
    finally:
        opencode.server_close()
        claude.server_close()


# is_external_address: the second gate, on the resolved address rather than
# the name. Policy allowing a name must not become a route to the host.


def test_public_addresses_are_external():
    assert is_external_address("93.184.216.34")
    assert is_external_address("2606:2800:220:1:248:1893:25c8:1946")


def test_loopback_refused():
    assert not is_external_address("127.0.0.1")
    assert not is_external_address("127.1.2.3")
    assert not is_external_address("::1")


def test_private_ranges_refused():
    assert not is_external_address("10.0.0.1")
    assert not is_external_address("172.16.5.4")
    assert not is_external_address("192.168.1.1")
    assert not is_external_address("fd00::1")  # IPv6 unique-local


def test_link_local_refused():
    # 169.254.169.254 is the cloud metadata host, the classic SSRF target.
    assert not is_external_address("169.254.169.254")
    assert not is_external_address("fe80::1")


def test_ipv4_mapped_loopback_refused():
    # ::ffff:127.0.0.1 must be judged as the v4 address it denotes, not waved
    # through as an unremarkable IPv6 address.
    assert not is_external_address("::ffff:127.0.0.1")
    assert not is_external_address("::ffff:10.0.0.1")


def test_unspecified_and_multicast_refused():
    assert not is_external_address("0.0.0.0")
    assert not is_external_address("::")
    assert not is_external_address("224.0.0.1")


def test_unparseable_address_refused():
    # Fail closed: this is the last check before a connection is made.
    assert not is_external_address("not-an-address")
    assert not is_external_address("")


# -- tunnel teardown --
#
# _relay keeps both directions of an established tunnel going. The cases below
# are the ones that used to leak: a peer that goes away *without closing*
# leaves the other direction blocked on a read that never returns, pinning two
# sockets and two threads in a proxy that outlives the session.

import socket  # noqa: E402
import threading  # noqa: E402

from aiab import netproxy  # noqa: E402
from aiab.netproxy import _relay  # noqa: E402


def _tunnel():
    """A relay thread over two socketpairs, plus the two far ends.

    Returns (thread, client_peer, upstream_peer): the relay shovels between
    the inner halves, and the test plays both outer ends.
    """
    client_peer, client = socket.socketpair()
    upstream, upstream_peer = socket.socketpair()
    t = threading.Thread(target=_relay, args=(client, upstream), daemon=True)
    t.start()
    return t, client_peer, upstream_peer


def test_relay_gives_up_when_the_client_vanishes(monkeypatch):
    # The client closes; the upstream stays open and silent (a server with a
    # long idle timeout). The upstream->client direction must not block forever.
    monkeypatch.setattr(netproxy, "_HALF_CLOSE_GRACE", 0.2)
    t, client_peer, upstream_peer = _tunnel()
    client_peer.close()
    t.join(10)
    assert not t.is_alive()
    upstream_peer.close()


def test_relay_gives_up_when_the_upstream_vanishes(monkeypatch):
    # The mirror image: the upstream closes while the client holds its end
    # open without sending anything more.
    monkeypatch.setattr(netproxy, "_HALF_CLOSE_GRACE", 0.2)
    t, client_peer, upstream_peer = _tunnel()
    upstream_peer.close()
    t.join(10)
    assert not t.is_alive()
    client_peer.close()


def test_relay_keeps_an_idle_tunnel_with_both_ends_live(monkeypatch):
    # The grace is an idle timeout for the *half-closed* state only: while
    # both ends are open, a tunnel that says nothing for a while (a long poll,
    # a streaming response between chunks) has to survive and still carry
    # traffic afterwards.
    monkeypatch.setattr(netproxy, "_HALF_CLOSE_GRACE", 0.2)
    t, client_peer, upstream_peer = _tunnel()
    try:
        t.join(0.6)  # several times the grace
        assert t.is_alive()
        client_peer.sendall(b"ping")
        assert upstream_peer.recv(16) == b"ping"
        upstream_peer.sendall(b"pong")
        assert client_peer.recv(16) == b"pong"
    finally:
        client_peer.close()
        upstream_peer.close()
    t.join(10)
    assert not t.is_alive()


# -- usage metering --
#
# The tunnel is the only place every proxied byte passes through, so it is
# where a session's traffic gets counted (see netproxy._UsageMeter). These
# cover the counting and the file it publishes for `aiab monitor`.

import json  # noqa: E402

from aiab.netproxy import _UsageMeter, usage_path  # noqa: E402


def _counted_tunnel(meter, host="example.com"):
    """A relay whose two directions report into a meter, plus the far ends."""
    client_peer, client = socket.socketpair()
    upstream, upstream_peer = socket.socketpair()
    t = threading.Thread(
        target=_relay,
        args=(
            client,
            upstream,
            lambda n: meter.count(host, up=n),
            lambda n: meter.count(host, down=n),
        ),
        daemon=True,
    )
    t.start()
    return t, client_peer, upstream_peer


def _published(path):
    return json.loads(path.read_text())["hosts"]


def test_meter_counts_both_directions_of_a_tunnel(tmp_path):
    path = tmp_path / "c.bytes"
    meter = _UsageMeter(path)
    t, client_peer, upstream_peer = _counted_tunnel(meter)
    try:
        client_peer.sendall(b"request")  # 7 bytes out
        assert upstream_peer.recv(64) == b"request"
        upstream_peer.sendall(b"a response")  # 10 bytes back
        assert client_peer.recv(64) == b"a response"
    finally:
        client_peer.close()
        upstream_peer.close()
    t.join(10)
    meter.flush()
    assert _published(path) == {"example.com": {"up": 7, "down": 10}}


def test_meter_accumulates_across_requests(tmp_path):
    path = tmp_path / "c.bytes"
    meter = _UsageMeter(path)
    meter.count("example.com", up=1, down=2)
    meter.count("example.com", up=10, down=20)
    meter.flush()
    assert _published(path)["example.com"] == {"up": 11, "down": 22}


def test_meter_writes_are_rate_limited_but_flush_is_not(tmp_path, monkeypatch):
    # A busy transfer must not rewrite the file once per 64KB chunk; the end
    # of a tunnel (and the proxy exiting) publishes regardless.
    monkeypatch.setattr(netproxy, "_USAGE_FLUSH_INTERVAL", 3600.0)
    path = tmp_path / "c.bytes"
    meter = _UsageMeter(path)
    meter.count("example.com", down=5)  # the first count is due at once
    meter.count("example.com", down=5)
    assert _published(path)["example.com"]["down"] == 5  # second not written yet
    meter.flush()
    assert _published(path)["example.com"]["down"] == 10


def test_meter_without_a_file_is_inert(tmp_path):
    # --usage-file is optional, so counting has to tolerate its absence
    # rather than every request raising.
    meter = _UsageMeter(None)
    meter.count("example.com", up=1)
    meter.flush()
    assert list(tmp_path.iterdir()) == []


def test_meter_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "c.bytes"
    meter = _UsageMeter(path)
    meter.count("example.com", up=1)
    meter.flush()
    assert [p.name for p in tmp_path.iterdir()] == ["c.bytes"]


def test_usage_path_sits_beside_the_other_proxy_files(tmp_path, monkeypatch):
    monkeypatch.setattr(netproxy, "PROXY_DIR", tmp_path)
    assert usage_path("claude-proj-abc123") == tmp_path / "claude-proj-abc123.bytes"


def test_a_request_through_the_proxy_lands_in_the_usage_file(tmp_path, monkeypatch):
    """End to end over the real handler: request in, counters on disk.

    The upstream is a socketpair rather than a real host — the point is the
    wiring between _Handler, _relay and the meter, not the network — but
    everything on this side of connect_external() is the production path,
    including the flush at the end of a tunnel.
    """
    monkeypatch.setattr(state, "_NET_PATH", tmp_path / "network.json")
    work = tmp_path / "proj"
    work.mkdir()
    state.set_network_mode(work, state.MODE_OPEN)  # allow, without a rule list
    response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"
    received: list[bytes] = []

    def fake_upstream(host, port):
        """Stand in for the host: echo back a canned response, then hang up."""
        ours, theirs = socket.socketpair()

        def serve():
            with ours:
                received.append(ours.recv(65536))
                ours.sendall(response)

        threading.Thread(target=serve, daemon=True).start()
        return theirs

    monkeypatch.setattr(netproxy, "connect_external", fake_upstream)
    usage = tmp_path / "c.bytes"
    srv = ProxyServer(
        "\0aiab-test-e2e", work, "claude", usage_file=usage
    )  # no pending dir: nothing should need parking in open mode
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(10)
        client.connect("\0aiab-test-e2e")
        with client:
            client.sendall(
                b"GET http://deb.example/pkg HTTP/1.1\r\nHost: deb.example\r\n\r\n"
            )
            got = b""
            while len(got) < len(response):
                chunk = client.recv(4096)
                if not chunk:
                    break
                got += chunk
        assert got == response
    finally:
        srv.shutdown()
        srv.server_close()

    hosts = _published(usage)
    assert hosts["deb.example"]["down"] == len(response)
    # The request the proxy rewrote and sent on, counted as it went out.
    assert hosts["deb.example"]["up"] == len(received[0])
