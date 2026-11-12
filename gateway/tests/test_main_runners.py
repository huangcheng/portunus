import pytest

from portunus_gateway import main


def test_parse_single_and_pinned():
    assert main.parse_runner_links("/a.sock:/a.tok") == [(None, "/a.sock", "/a.tok")]
    assert main.parse_runner_links("site-a=/a.sock:/a.tok, /b.sock:/b.tok") == [
        ("site-a", "/a.sock", "/a.tok"),
        (None, "/b.sock", "/b.tok"),
    ]


@pytest.mark.parametrize("raw", ["", " , ", "/a.sock", "=/a.sock:/t", "/a.sock:", "/a:/b:/c"])
def test_parse_rejects_bad(raw):
    with pytest.raises(main.ConfigError):
        main.parse_runner_links(raw)


def test_build_pool_multi(tmp_path, monkeypatch):
    for n in "ab":
        (tmp_path / f"{n}.tok").write_text("secret-" + n + "\n")
    monkeypatch.setenv(
        "PORTUNUS_RUNNERS", f"site-a={tmp_path}/a.sock:{tmp_path}/a.tok,{tmp_path}/b.sock:{tmp_path}/b.tok"
    )
    monkeypatch.setenv("PORTUNUS_RUNNER_UID", "0")
    pool, first = main.build_runner_pool(dev=True, heartbeat_s=1.0)
    assert len(pool) == 2 and pool.clients[0] is first
    assert pool.site_ids()[0] == "site-a"


def test_build_pool_duplicate_pin(tmp_path, monkeypatch):
    (tmp_path / "t").write_text("x")
    monkeypatch.setenv("PORTUNUS_RUNNERS", f"s=/a:{tmp_path}/t,s=/b:{tmp_path}/t")
    monkeypatch.setenv("PORTUNUS_RUNNER_UID", "0")
    with pytest.raises(main.ConfigError):
        main.build_runner_pool(dev=True, heartbeat_s=1.0)


def test_build_pool_default_single(tmp_path, monkeypatch):
    (tmp_path / "t").write_text("x")
    monkeypatch.delenv("PORTUNUS_RUNNERS", raising=False)
    monkeypatch.setenv("PORTUNUS_SOCKET", str(tmp_path / "s.sock"))
    monkeypatch.setenv("PORTUNUS_TOKEN_FILE", str(tmp_path / "t"))
    monkeypatch.setenv("PORTUNUS_RUNNER_UID", "0")
    pool, _ = main.build_runner_pool(dev=True, heartbeat_s=1.0)
    assert len(pool) == 1


# ---- networked runner links (tls:// / tcp:// addresses) -----------------------------------


def test_parse_network_links():
    assert main.parse_runner_links("hk=tls://runner.example.com:443:/etc/t") == [
        ("hk", "tls://runner.example.com:443", "/etc/t")
    ]
    # Whitespace after `site=` is tolerated for URL links as it is for socket paths.
    assert main.parse_runner_links("hk= tls://runner.example.com:443:/etc/t") == [
        ("hk", "tls://runner.example.com:443", "/etc/t")
    ]
    assert main.parse_runner_links("tcp://127.0.0.1:9000:/tmp/tok, /a.sock:/a.tok") == [
        (None, "tcp://127.0.0.1:9000", "/tmp/tok"),
        (None, "/a.sock", "/a.tok"),
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "tls://runner.example.com:443",  # no token file
        "tls://runner.example.com:/etc/t",  # no port
        "tcp://:9000:/etc/t",  # no host
        "hk=tls://host:443:/a:/b",  # colon in token path
    ],
)
def test_parse_rejects_bad_network_links(raw):
    with pytest.raises(main.ConfigError):
        main.parse_runner_links(raw)


def test_build_pool_all_remote_skips_local_uid(tmp_path, monkeypatch):
    """An all-remote fleet must not require a local portunus-runner user (SO_PEERCRED N/A)."""
    (tmp_path / "t").write_text("x")
    monkeypatch.setenv(
        "PORTUNUS_RUNNERS", f"hk=tls://runner.example.com:443:{tmp_path}/t"
    )
    monkeypatch.delenv("PORTUNUS_RUNNER_UID", raising=False)
    pool, first = main.build_runner_pool(dev=False, heartbeat_s=1.0)
    assert len(pool) == 1
    assert first.address == "tls://runner.example.com:443"


def test_portunus_socket_rejects_network_url(tmp_path, monkeypatch):
    (tmp_path / "t").write_text("x")
    monkeypatch.delenv("PORTUNUS_RUNNERS", raising=False)
    monkeypatch.setenv("PORTUNUS_SOCKET", "tls://runner.example.com:443")
    monkeypatch.setenv("PORTUNUS_TOKEN_FILE", str(tmp_path / "t"))
    monkeypatch.setenv("PORTUNUS_RUNNER_UID", "0")
    with pytest.raises(main.ConfigError):
        main.build_runner_pool(dev=True, heartbeat_s=1.0)


def test_cleartext_runner_link_off_loopback_refused():
    from portunus_gateway.runner_client import RunnerClient

    with pytest.raises(ValueError, match="cleartext"):
        RunnerClient("tcp://runner.example.com:9000", "tok")
    # A name is not a loopback guarantee: localhost can resolve off-box via hosts/DNS.
    with pytest.raises(ValueError, match="cleartext"):
        RunnerClient("tcp://localhost:9000", "tok")
    # Numeric loopback literals (127.0.0.0/8, ::1) stay usable (dev harnesses).
    RunnerClient("tcp://127.0.0.1:9000", "tok")
    RunnerClient("tcp://127.0.0.2:9000", "tok")
    RunnerClient("tls://runner.example.com:443", "tok")
