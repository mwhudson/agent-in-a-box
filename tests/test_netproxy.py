# Tests for aiab.netproxy.evaluate — the pure policy-matching core of the
# filtering proxy. The socket/parking machinery around it is exercised
# manually; this covers the verdict logic.

import aiab.state as state
from aiab.netproxy import ALLOW, ASK, DENY, evaluate


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
