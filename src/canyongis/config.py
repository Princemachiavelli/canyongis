"""Legacy shim: the rule-based Config has been replaced by `canyongis.ml.config.MLConfig`.

This module re-exports `MLConfig` under the name `Config` for any external code
that still imports `canyongis.config.Config`. New code should import `MLConfig`
directly from `canyongis.ml`.
"""

from __future__ import annotations

from .ml.config import MLConfig as Config

__all__ = ["Config"]
