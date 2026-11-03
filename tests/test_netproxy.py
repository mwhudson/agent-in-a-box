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
