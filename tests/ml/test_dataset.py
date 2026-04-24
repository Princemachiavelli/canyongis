from __future__ import annotations

from canyongis.ml.dataset import SpatialKFoldSplitter, TileRecord


def _make_records(n_super: int, n_folds_worth: int = 5) -> list[TileRecord]:
    recs = []
    for s in range(n_super):
        super_id = f"demA:{s // 4}:{s % 4}"
        for t in range(n_folds_worth):
            recs.append(
                TileRecord(
                    dem_id="demA", row=s * 256, col=t * 128, super_tile_id=super_id, has_positive=False
                )
            )
    return recs


def test_spatial_kfold_disjoint_val_sets():
    recs = _make_records(n_super=40)
    splitter = SpatialKFoldSplitter(n_folds=5, seed=42)
    folds = splitter.split(recs)
    val_union: set[int] = set()
    for train, val in folds:
        # No overlap within a fold.
        assert not (set(train) & set(val))
        # Train + val cover all records.
        assert set(train) | set(val) == set(range(len(recs)))
        val_union.update(val)
    # Every record is in exactly one val set.
    assert val_union == set(range(len(recs)))


def test_spatial_kfold_groups_supertiles():
    """All records with the same super_tile_id must land in the same fold."""
    recs = _make_records(n_super=20)
    splitter = SpatialKFoldSplitter(n_folds=5, seed=7)
    folds = splitter.split(recs)
    # Reverse map: record index -> fold k (via val membership).
    rec_fold: dict[int, int] = {}
    for k, (_tr, val) in enumerate(folds):
        for i in val:
            rec_fold[i] = k
    # Group by super_tile_id.
    by_super: dict[str, set[int]] = {}
    for i, r in enumerate(recs):
        by_super.setdefault(r.super_tile_id, set()).add(rec_fold[i])
    for sid, ks in by_super.items():
        assert len(ks) == 1, f"super-tile {sid} was split across folds: {ks}"
