from __future__ import annotations


def test_legacy_config_alias_exports_mlconfig() -> None:
    from canyongis.config import Config, __all__
    from canyongis.ml.config import MLConfig

    assert Config is MLConfig
    assert __all__ == ["Config"]
