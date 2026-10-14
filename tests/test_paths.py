from pathlib import Path

from click.testing import CliRunner

from bagarium import paths, video
from bagarium.cli import main


def test_precedence(monkeypatch, tmp_path):
    for var in ("BAGARIUM_DB", "BAGARIUM_CACHE_DIR", "BAGARIUM_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    assert paths.db_path() == Path.home() / ".local/share/bagarium/bagarium.db"
    assert paths.cache_dir() == Path.home() / ".cache/bagarium"
    assert paths.source("db") == paths.source("cache") == "default"

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.db_path() == tmp_path / "xdg/bagarium/bagarium.db"

    monkeypatch.setenv("BAGARIUM_HOME", str(tmp_path / "home"))
    assert paths.db_path() == tmp_path / "home/bagarium.db"
    assert paths.cache_dir() == tmp_path / "home/cache"
    assert video.cache_dir() == tmp_path / "home/cache"
    assert paths.source("cache") == "BAGARIUM_HOME"

    monkeypatch.setenv("BAGARIUM_DB", str(tmp_path / "own.db"))
    monkeypatch.setenv("BAGARIUM_CACHE_DIR", "~/somewhere")
    assert paths.db_path() == tmp_path / "own.db"
    assert paths.cache_dir() == Path.home() / "somewhere"
    assert paths.source("db") == "BAGARIUM_DB"


def test_home_option(monkeypatch, tmp_path, bag_root):
    for var in ("BAGARIUM_DB", "BAGARIUM_CACHE_DIR", "BAGARIUM_HOME"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "data"
    runner = CliRunner()
    r = runner.invoke(main, ["--home", str(home), "scan", str(bag_root)])
    assert r.exit_code == 0, r.output
    assert (home / "bagarium.db").exists()
    out = runner.invoke(main, ["--home", str(home), "info"]).output
    assert f"{home / 'bagarium.db'}" in out and "from BAGARIUM_HOME" in out and f"{home / 'cache'}" in out
    assert "4 (0 missing, 0 broken) in 1 root(s)" in out
    # --db still wins for the catalog
    out = runner.invoke(main, ["--home", str(home), "--db", str(tmp_path / "x.db"), "info"]).output
    assert f"{tmp_path / 'x.db'}" in out and "from --db" in out
