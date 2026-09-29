"""peer_client.build_request — the raw HTTP head a peer call writes."""

from claude_launcher.daemon import peer_client


def test_non_ascii_machine_name_builds_an_ascii_head():
    # A relay machine name is free text; it went into Host verbatim and a
    # non-ASCII one (or the invite panel's "machine…" placeholder) raised
    # UnicodeEncodeError before the request ever left.
    raw = peer_client.build_request("/peer/sessions", {}, host="machine…")
    head, _, body = raw.partition(b"\r\n\r\n")
    head.decode("ascii")
    assert b"Host: machine%E2%80%A6\r\n" in head
    assert body == b"{}"


def test_ascii_machine_name_is_left_as_is():
    raw = peer_client.build_request("/peer/sessions", {}, host="yusanghyun-D09")
    assert b"Host: yusanghyun-D09\r\n" in raw
