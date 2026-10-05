"""Bind-address selection for the A2A server (issue #32).

The bug this guards against is not a typo, it is drift: the socket bind and the
agent card's advertised URL were decided independently, and the client sends
every JSON-RPC call to the URL *in the card* rather than to the address it was
pointed at. So a bind that does not match the card produces a server whose card
fetch works and whose every subsequent call is refused.

These tests only exercise flag/env resolution and the advertised-IP derivation.
Nothing here opens a socket, and the one path that would (LAN auto-detect) is
avoided by setting A2A_HOST, so no network access is required.
"""

import pytest

from agent.a2a_connector import (
    BIND_ALL_INTERFACES,
    BIND_LOCAL,
    _resolve_advertised_ip,
    _resolve_bind,
)


class TestResolveBind:
    def test_defaults_to_loopback(self, monkeypatch):
        # The whole point of the fix: no flag must mean "not reachable from the
        # network", not the old all-interfaces bind.
        monkeypatch.delenv("A2A_BIND", raising=False)
        assert _resolve_bind([]) == BIND_LOCAL

    @pytest.mark.parametrize("argv,expected", [([], BIND_LOCAL), (["--host", "local"], BIND_LOCAL), (["--host", "all_interfaces"], BIND_ALL_INTERFACES)])
    def test_two_presets(self, argv, expected, monkeypatch):
        monkeypatch.delenv("A2A_BIND", raising=False)
        assert _resolve_bind(argv) == expected

    def test_flag_takes_only_words_not_addresses(self, monkeypatch):
        # A specific address is deliberately not a third choice: pinning one
        # interface loses Wi-Fi IP-change resilience, which deserves a deliberate
        # env edit rather than a word on the command line.
        monkeypatch.delenv("A2A_BIND", raising=False)
        for rejected in (BIND_LOCAL, BIND_ALL_INTERFACES, "192.168.1.5", "LAN"):
            with pytest.raises(SystemExit):
                _resolve_bind(["--host", rejected])

    def test_env_var_is_the_escape_hatch_for_a_custom_address(self, monkeypatch):
        monkeypatch.setenv("A2A_BIND", "192.168.1.5")
        assert _resolve_bind([]) == "192.168.1.5"

    def test_env_var_overrides_the_preset(self, monkeypatch):
        # Resolved in favour of the env var so a machine that needs a custom
        # address does not have to remember the flag on every invocation.
        monkeypatch.setenv("A2A_BIND", "192.168.1.5")
        assert _resolve_bind(["--host", "local"]) == "192.168.1.5"

    def test_blank_env_var_falls_back_to_the_preset(self, monkeypatch):
        monkeypatch.setenv("A2A_BIND", "   ")
        assert _resolve_bind([]) == BIND_LOCAL


class TestAdvertisedIpTracksBind:
    def test_loopback_bind_advertises_loopback(self, monkeypatch):
        # A2A_HOST must NOT leak into the card under a loopback bind: the socket
        # is not on that address, so advertising it would break every call.
        monkeypatch.setenv("A2A_HOST", "192.168.1.5")
        assert _resolve_advertised_ip(BIND_LOCAL) == BIND_LOCAL

    def test_wildcard_bind_advertises_a2a_host_override(self, monkeypatch):
        # Set rather than auto-detected so no socket is opened.
        monkeypatch.setenv("A2A_HOST", "192.168.1.5")
        assert _resolve_advertised_ip(BIND_ALL_INTERFACES) == "192.168.1.5"

    def test_custom_bind_advertises_itself(self, monkeypatch):
        # The A2A_BIND case: pinned to one interface, so that interface is the
        # only address the socket is on and the only correct thing to name.
        # A2A_HOST is ignored here.
        monkeypatch.setenv("A2A_HOST", "10.9.9.9")
        assert _resolve_advertised_ip("192.168.1.5") == "192.168.1.5"

    def test_card_never_advertises_the_wildcard_address(self, monkeypatch):
        # `0.0.0.0` is a bind address, not a destination. Putting it in the card
        # would be the one case the client cannot satisfy, because there is
        # nothing to connect *to*.
        monkeypatch.setenv("A2A_HOST", "192.168.1.5")
        for bind in (BIND_LOCAL, BIND_ALL_INTERFACES, "192.168.1.5"):
            assert _resolve_advertised_ip(bind) != BIND_ALL_INTERFACES


@pytest.mark.parametrize(
    "bind,expected",
    [
        (BIND_LOCAL, "127.0.0.1"),
        (BIND_ALL_INTERFACES, "192.168.1.5"),
        ("192.168.1.5", "192.168.1.5"),
    ],
)
def test_card_names_an_address_the_bind_covers(bind, expected, monkeypatch):
    """The invariant in one place: the card names an address the bind covers."""
    monkeypatch.setenv("A2A_HOST", "192.168.1.5")
    assert _resolve_advertised_ip(bind) == expected