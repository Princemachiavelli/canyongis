from __future__ import annotations

import torch


def get_device(prefer: str | None = None) -> torch.device:
    """Pick the best available device: cuda > mps > cpu.

    `prefer` can force a specific backend ("cuda", "mps", "cpu") when available;
    falls back to the next option if not.
    """
    order: list[str]
    if prefer is None:
        order = ["cuda", "mps", "cpu"]
    else:
        order = [prefer] + [d for d in ("cuda", "mps", "cpu") if d != prefer]

    for name in order:
        if name == "cuda" and torch.cuda.is_available():
            return torch.device("cuda")
        if name == "mps" and getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            return torch.device("mps")
        if name == "cpu":
            return torch.device("cpu")
    return torch.device("cpu")


def describe_device(device: torch.device) -> str:
    """Return a short backend/device summary using the active torch runtime."""
    if device.type == "cuda":
        idx = device.index if device.index is not None else torch.cuda.current_device()
        props = torch.cuda.get_device_properties(idx)
        total_mem_gib = props.total_memory / (1024 ** 3)
        return (
            f"backend=CUDA device=cuda:{idx} name={props.name} "
            f"cc={props.major}.{props.minor} vram_gib={total_mem_gib:.1f}"
        )

    if device.type == "mps":
        built = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_built())
        available = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
        return f"backend=Metal (MPS) device=mps built={built} available={available}"

    return "backend=CPU device=cpu"


def amp_enabled(device: torch.device) -> bool:
    """Enable autocast on CUDA and MPS.

    MPS autocast is less battle-tested than CUDA in this project, but there is
    no current PyTorch doc stating it should be avoided outright.
    """
    return device.type in {"cuda", "mps"}


def grad_scaler_enabled(device: torch.device) -> bool:
    """Gradient scaling is only enabled on CUDA."""
    return device.type == "cuda"


def batch_size_for(device: torch.device, cfg) -> int:
    """Pick batch size from MLConfig based on device."""
    if device.type == "cuda":
        return cfg.batch_size_cuda
    if device.type == "mps":
        return cfg.batch_size_mps
    return cfg.batch_size_cpu
