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
