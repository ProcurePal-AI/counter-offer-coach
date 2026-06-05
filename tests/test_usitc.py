import json
import sys
from pathlib import Path

# pipeline/ is on sys.path when the connectors run as scripts; mirror that here.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import storage  # noqa: E402
import usitc  # noqa: E402


def _sample_response():
    return {
        "dto": {
            "tables": [
                {
                    "column_groups": [
                        {"label": "Period"},
                        {"label": "Customs Value"},
                        {"label": "First Unit Quantity"},
                    ],
                    "row_groups": [
                        {
                            "rowsNew": [
                                {
                                    "rowEntries": [
                                        {"value": "01/2026"},
                                        {"value": "1,500"},
                                        {"value": "300"},
                                    ]
                                },
                                {
                                    "rowEntries": [
                                        {"value": "02/2026"},
                                        {"value": "100"},
                                        {"value": "0"},
                                    ]
                                },
                                {
                                    "rowEntries": [
                                        {"value": "03/2026"},
                                        {"value": ""},
                                        {"value": "15"},
                                    ]
                                },
                            ]
                        }
                    ],
                }
            ]
        }
    }


def _wide_response(unit_description: str, quantity_unit: str):
    return {
        "dto": {
            "errors": [],
            "tables": [
                {
                    "tab_name": "Customs Value",
                    "column_groups": [
                        {"columns": [{"label": "Year"}, {"label": "Quantity Description"}]},
                        {"columns": [{"label": "January"}, {"label": "February"}]},
                    ],
                    "row_groups": [
                        {
                            "rowsNew": [
                                {
                                    "rowEntries": [
                                        {"value": "2026"},
                                        {"value": unit_description},
                                        {"value": "1,500"},
                                        {"value": "100"},
                                    ]
                                }
                            ]
                        }
                    ],
                },
                {
                    "tab_name": "First Unit of Quantity",
                    "column_groups": [
                        {"columns": [{"label": "Year"}, {"label": "Quantity Description"}]},
                        {"columns": [{"label": "January"}, {"label": "February"}]},
                    ],
                    "row_groups": [
                        {
                            "rowsNew": [
                                {
                                    "rowEntries": [
                                        {"value": "2026"},
                                        {"value": quantity_unit},
                                        {"value": "300"},
                                        {"value": "0"},
                                    ]
                                }
                            ]
                        }
                    ],
                },
            ],
        }
    }


def test_build_query_targets_normalized_hts_monthly_imports():
    query = usitc.build_query("2902.20.00", months=12)

    assert query["reportOptions"] == {"tradeType": "Import", "classificationSystem": "HTS"}
    assert query["searchOptions"]["commodities"]["commodities"] == ["29022000"]
    assert query["searchOptions"]["commodities"]["commoditiesExpanded"] == [
        {"name": "29022000", "value": "29022000"}
    ]
    assert query["searchOptions"]["commodities"]["commoditiesManual"] == "29022000"
    assert query["searchOptions"]["commodities"]["commoditySelectType"] == "list"
    assert query["searchOptions"]["componentSettings"]["yearsTimeline"] == "Monthly"
    assert query["searchOptions"]["componentSettings"]["timeframeSelectType"] == "specificDateRange"
    assert query["searchOptions"]["componentSettings"]["dataToReport"] == [
        "CONS_CUSTOMS_VALUE",
        "CONS_FIR_UNIT_QUANT",
    ]


def test_normalize_rows_retains_incomplete_observations_with_null_unit_value():
    rows = usitc.normalize_rows(
        "benzene",
        "2902.20.00",
        _sample_response(),
        fetched_at="2026-06-02T00:00:00+00:00",
    )

    assert rows == [
        {
            "chemical_id": "benzene",
            "source": "USITC",
            "hts_code": "29022000",
            "region": "US_IMPORTS_ALL_ORIGINS",
            "period": "2026-01",
            "price_usd_per_kg": 5.0,
            "fetched_at": "2026-06-02T00:00:00+00:00",
        },
        {
            "chemical_id": "benzene",
            "source": "USITC",
            "hts_code": "29022000",
            "region": "US_IMPORTS_ALL_ORIGINS",
            "period": "2026-02",
            "price_usd_per_kg": None,
            "fetched_at": "2026-06-02T00:00:00+00:00",
        },
        {
            "chemical_id": "benzene",
            "source": "USITC",
            "hts_code": "29022000",
            "region": "US_IMPORTS_ALL_ORIGINS",
            "period": "2026-03",
            "price_usd_per_kg": None,
            "fetched_at": "2026-06-02T00:00:00+00:00",
        },
    ]


def test_normalize_rows_expands_dataweb_monthly_wide_tables():
    rows = usitc.normalize_rows(
        "aniline",
        "29214120",
        _wide_response("Value for: kilograms", "kilograms"),
        fetched_at="2026-06-02T00:00:00+00:00",
    )

    assert rows == [
        {
            "chemical_id": "aniline",
            "source": "USITC",
            "hts_code": "29214120",
            "region": "US_IMPORTS_ALL_ORIGINS",
            "period": "2026-01",
            "price_usd_per_kg": 5.0,
            "fetched_at": "2026-06-02T00:00:00+00:00",
        },
        {
            "chemical_id": "aniline",
            "source": "USITC",
            "hts_code": "29214120",
            "region": "US_IMPORTS_ALL_ORIGINS",
            "period": "2026-02",
            "price_usd_per_kg": None,
            "fetched_at": "2026-06-02T00:00:00+00:00",
        },
    ]


def test_normalize_rows_retains_non_kg_quantity_with_null_unit_value():
    rows = usitc.normalize_rows(
        "benzene",
        "29022000",
        _wide_response("Value for: liters", "liters"),
        fetched_at="2026-06-02T00:00:00+00:00",
    )

    assert rows[0]["period"] == "2026-01"
    assert rows[0]["price_usd_per_kg"] is None


def test_preserve_raw_response_writes_without_overwrite(tmp_path):
    first = usitc.preserve_raw_response(_sample_response(), "benzene", "29022000", tmp_path)
    second = usitc.preserve_raw_response(_sample_response(), "benzene", "29022000", tmp_path)

    assert first.exists()
    assert second.exists()
    assert first != second
    assert json.loads(first.read_text()) == _sample_response()


def test_pull_all_can_use_mock_response_without_token(tmp_path):
    mock = tmp_path / "run_report.json"
    mock.write_text(json.dumps(_sample_response()))

    rows = usitc.pull_all(token=None, raw_dir=tmp_path / "raw", mock_response=mock)

    assert len(rows) == 6
    assert {row["chemical_id"] for row in rows} == {"benzene", "aniline"}
    raw_files = sorted(path.name for path in (tmp_path / "raw").iterdir())
    assert len(raw_files) == 2
    assert any(name.endswith("_benzene_29022000_runReport.json") for name in raw_files)
    assert any(name.endswith("_aniline_29214120_runReport.json") for name in raw_files)


def test_storage_dry_run_does_not_touch_database_or_csv(capsys):
    storage.write_price_observations(
        [
            {
                "chemical_id": "benzene",
                "source": "USITC",
                "hts_code": "29022000",
                "region": "US_IMPORTS_ALL_ORIGINS",
                "period": "2026-01",
                "price_usd_per_kg": None,
                "fetched_at": "2026-06-02T00:00:00+00:00",
            }
        ],
        dry_run=True,
    )

    assert "storage write skipped" in capsys.readouterr().out


def test_storage_writes_price_observations_to_postgres(db_conn):
    rows = [
        {
            "chemical_id": "benzene",
            "source": "USITC",
            "hts_code": "29022000",
            "region": "US_IMPORTS_ALL_ORIGINS",
            "period": "2026-01",
            "price_usd_per_kg": None,
            "fetched_at": "2026-06-02T00:00:00+00:00",
        },
        {
            "chemical_id": "aniline",
            "source": "USITC",
            "hts_code": "29214120",
            "region": "US_IMPORTS_ALL_ORIGINS",
            "period": "2026-01",
            "price_usd_per_kg": 5.0,
            "fetched_at": "2026-06-02T00:00:00+00:00",
        },
    ]

    assert storage.write_price_observations(rows, conn=db_conn) == 2
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT chemical_id, hts_code, price_usd_per_kg FROM price_observations "
            "ORDER BY chemical_id"
        )
        got = cur.fetchall()
    assert got == [
        ("aniline", "29214120", 5.0),
        ("benzene", "29022000", None),
    ]
