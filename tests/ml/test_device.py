from __future__ import annotations

import torch

from canyongis.ml.device import amp_enabled, get_device


def test_get_device_returns_torch_device():
    d = get_device()
    assert isinstance(d, torch.device)
    assert d.type in ("cuda", "mps", "cpu")


def test_get_device_prefer_cpu():
    d = get_device(prefer="cpu")
    assert d.type == "cpu"


def test_amp_only_on_cuda():
    assert amp_enabled(torch.device("cpu")) is False
    assert amp_enabled(torch.device("cuda")) is True
    assert amp_enabled(torch.device("mps")) is False


def test_batch_size_for_picks_per_device():
    from canyongis.ml.config import MLConfig
    from canyongis.ml.device import batch_size_for

    cfg = MLConfig()
    assert batch_size_for(torch.device("cpu"), cfg) == cfg.batch_size_cpu
    assert batch_size_for(torch.device("cuda"), cfg) == cfg.batch_size_cuda
    assert batch_size_for(torch.device("mps"), cfg) == cfg.batch_size_mps
