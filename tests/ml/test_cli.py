from __future__ import annotations

import argparse

from canyongis.ml.cli import (
    _add_config_flags,
    _cfg_from_args,
    _cli_defaults,
    _explicit_config_overrides,
)


def _parse_config_args(argv: list[str]):
    defaults = _cli_defaults()
    parser = argparse.ArgumentParser()
    _add_config_flags(
        parser,
        defaults,
        skip={"manifest_path", "out_dir", "dem_path", "checkpoint_path"},
    )
    explicit = _explicit_config_overrides(
        argv,
        skip={"manifest_path", "out_dir", "dem_path", "checkpoint_path"},
    )
    return _cfg_from_args(parser.parse_args(argv), defaults, explicit), explicit


def test_cli_defaults_enable_geomorphons():
    cfg = _cli_defaults()

    assert cfg.use_geomorphons is True
    assert cfg.geomorphon_search_m == (30.0,)
    assert cfg.in_channels == 12


def test_cli_can_disable_default_geomorphons():
    cfg, explicit = _parse_config_args(["--no-use-geomorphons"])

    assert cfg.use_geomorphons is False
    assert cfg.in_channels == 7
    assert "use_geomorphons" in explicit


def test_cli_derives_channels_for_explicit_geomorphon_scales():
    cfg, _explicit = _parse_config_args(["--geomorphon-search-m", "10,30,75"])

    assert cfg.use_geomorphons is True
    assert cfg.geomorphon_search_m == (10.0, 30.0, 75.0)
    assert cfg.in_channels == 22
