from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from canyongis.web import migrate_previews


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(path)


def test_migrate_converts_previews_updates_jobs_and_deletes_old(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(migrate_previews, "OVERLAY_DOWNSAMPLE_FACTOR", 1)
    out_dir = tmp_path / "jobs" / "one"
    png = out_dir / "panel_hillshade.png"
    _write_png(png)
    rows = [
        {
            "id": "job-1",
            "result": {
                "out_dir": str(out_dir),
                "panel_hillshade_url": "/outputs/jobs/one/panel_hillshade.png",
            },
        }
    ]
    (tmp_path / "jobs.json").write_text(json.dumps(rows))

    migrate_previews.migrate(tmp_path, delete_old=True)

    assert not png.exists()
    assert (out_dir / "panel_hillshade.webp").exists()
    updated = json.loads((tmp_path / "jobs.json").read_text())
    assert updated[0]["result"]["panel_hillshade_url"] == "/outputs/jobs/one/panel_hillshade.webp"
    assert "converted 1 files" in capsys.readouterr().out


def test_migrate_adds_missing_url_from_existing_webp(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(migrate_previews, "OVERLAY_DOWNSAMPLE_FACTOR", 1)
    out_dir = tmp_path / "jobs" / "one"
    out_dir.mkdir(parents=True)
    Image.new("RGBA", (2, 2), (0, 255, 0, 255)).save(out_dir / "panel_b_tpi.webp")
    rows = [{"id": "job-1", "result": {"out_dir": str(out_dir)}}]
    (tmp_path / "jobs.json").write_text(json.dumps(rows))

    migrate_previews.migrate(tmp_path)

    updated = json.loads((tmp_path / "jobs.json").read_text())
    assert updated[0]["result"]["panel_b_tpi_url"] == "/outputs/jobs/one/panel_b_tpi.webp"


def test_migrate_ignores_webp_outside_output_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(migrate_previews, "OVERLAY_DOWNSAMPLE_FACTOR", 1)
    outside = tmp_path.parent / "outside-canyongis-preview-test"
    outside.mkdir(exist_ok=True)
    Image.new("RGBA", (2, 2), (0, 0, 255, 255)).save(outside / "panel_b_tpi.webp")
    rows = [{"id": "job-1", "result": {"out_dir": str(outside)}}]
    (tmp_path / "jobs.json").write_text(json.dumps(rows))

    migrate_previews.migrate(tmp_path)

    updated = json.loads((tmp_path / "jobs.json").read_text())
    assert "panel_b_tpi_url" not in updated[0]["result"]


def test_cli_passes_arguments(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(migrate_previews, "migrate", lambda root, *, delete_old: calls.append((root, delete_old)))

    migrate_previews.cli(["--output-root", str(tmp_path), "--delete-old"])

    assert calls == [(tmp_path, True)]
