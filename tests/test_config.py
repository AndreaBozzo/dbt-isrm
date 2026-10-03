import pytest

from dbt_isrm.config import load_config, new_releases, select, version_key


def test_default_config_resolves_paths_relative_to_file(repo):
    cfg = load_config(repo / "configs" / "default.yml")
    assert cfg.fixtures_dir == repo / "fixtures"
    assert list(cfg.stages) == ["parse", "compile", "compile_strict", "build"]
    assert cfg.stages["compile_strict"].args == ["compile", "--static-analysis", "strict"]
    assert cfg.stages["build"].warehouse and not cfg.stages["parse"].warehouse
    for fixture in cfg.fixtures:
        assert (cfg.fixtures_dir / fixture / "dbt_project.yml").is_file(), fixture


def test_select_filters_keep_config_order(repo):
    cfg = load_config(repo / "configs" / "default.yml")
    versions, fixtures, stages = select(cfg, None, ["exposure", "source"], ["build", "parse"])
    assert versions == cfg.versions
    assert fixtures == ["source", "exposure"]
    assert stages == ["parse", "build"]


def test_select_accepts_unconfigured_version(repo):
    cfg = load_config(repo / "configs" / "default.yml")
    assert select(cfg, ["9.9.9"], None, None)[0] == ["9.9.9"]


@pytest.mark.parametrize("kind", ["fixture", "stage"])
def test_select_rejects_unknown_names(repo, kind):
    cfg = load_config(repo / "configs" / "default.yml")
    args = {"fixture": (None, ["typo"], None), "stage": (None, None, ["typo"])}[kind]
    with pytest.raises(ValueError, match=f"unknown {kind}"):
        select(cfg, *args)


def test_version_key_orders_numerically():
    assert sorted(["2.0.10", "2.0.9", "2.0.0rc1", "2.1.0"], key=version_key) == [
        "2.0.9",
        "2.0.10",
        "2.1.0",
        "2.0.0rc1",
    ]


def test_new_releases_are_newer_stable_same_major():
    published = ["0.21.1", "1.0.0", "2.0.5", "2.0.6", "2.0.7", "2.0.10", "2.1.0rc1", "3.0.0"]
    assert new_releases(published, ["2.0.5", "2.0.6"]) == ["2.0.7", "2.0.10"]
    assert new_releases(published, ["2.0.10"]) == []
    assert new_releases(published, ["2.0.0rc1"]) == []
