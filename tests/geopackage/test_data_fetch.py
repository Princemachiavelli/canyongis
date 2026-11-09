from __future__ import annotations

from canyongis.geopackage.data_fetch import latest_per_quad, quad_key, total_bytes


def test_quad_key_drops_the_date() -> None:
    assert quad_key("ca_m_3411752_nw_11_060_20220530") == "ca_m_3411752_nw_11_060"


def test_latest_per_quad_keeps_newest_scene() -> None:
    items = [
        {"id": "ca_m_1_nw_11_060_20200101", "datetime": "2020-01-01T00:00:00Z", "href": "old"},
        {"id": "ca_m_1_nw_11_060_20220101", "datetime": "2022-01-01T00:00:00Z", "href": "new"},
        {"id": "ca_m_2_ne_11_060_20210101", "datetime": "2021-01-01T00:00:00Z", "href": "other"},
    ]
    kept = latest_per_quad(items)
    assert [i["href"] for i in kept] == ["new", "other"]


def test_total_bytes_ignores_missing_sizes() -> None:
    assert total_bytes([{"sizeInBytes": 10}, {"sizeInBytes": "5"}, {}]) == 15
