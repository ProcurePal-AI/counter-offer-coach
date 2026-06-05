from pipeline.sec_edgar import (
    COMPANIES,
    MISSING_SEC_CIK_TYPE,
    _archive_url,
    _infer_covestro_form_type,
    _padded_cik,
    missing_sec_cik_record,
    normalize_fallback_filings,
    normalize_target_filings,
    resolve_cik,
)


def _sample_submissions() -> dict:
    return {
        "filings": {
            "recent": {
                "form": ["8-K", "10-Q", "20-F", "6-K", "10-K"],
                "filingDate": [
                    "2025-01-01",
                    "2025-05-01",
                    "2025-03-15",
                    "2025-08-01",
                    "2026-02-15",
                ],
                "reportDate": [
                    "2025-01-01",
                    "2025-03-31",
                    "2024-12-31",
                    "2025-06-30",
                    "2025-12-31",
                ],
                "accessionNumber": [
                    "0000000000-25-000001",
                    "0001437749-25-014251",
                    "0001104659-25-018854",
                    "0001157523-25-007918",
                    "0001437749-26-004524",
                ],
                "primaryDocument": [
                    "ignored.htm",
                    "hun20250331_10q.htm",
                    "basf-20f.htm",
                    "basf-6k.txt",
                    "hun20251231_10k.htm",
                ],
            }
        }
    }


def test_padded_cik_and_archive_url():
    assert _padded_cik("1307954") == "0001307954"
    assert (
        _archive_url("1307954", "0001437749-26-004524", "hun20251231_10k.htm")
        == "https://www.sec.gov/Archives/edgar/data/1307954/"
        "000143774926004524/hun20251231_10k.htm"
    )


def test_resolve_cik_uses_ticker_before_name_lookup():
    company_index = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [9999999, "Not Huntsman", "NOPE", "NYSE"],
            [1307954, "Huntsman Corporation", "HUN", "NYSE"],
        ],
    }

    assert resolve_cik(COMPANIES["huntsman"], company_index) == "1307954"


def test_normalize_target_filings_keeps_only_10k_and_10q():
    rows = normalize_target_filings(
        "huntsman",
        COMPANIES["huntsman"],
        "1307954",
        _sample_submissions(),
        max_filings=10,
    )

    assert [row.filing_type for row in rows] == ["10-Q", "10-K"]
    assert rows[0].source == "SEC EDGAR"
    assert rows[0].source_url.endswith("/000143774925014251/hun20250331_10q.htm")


def test_normalize_fallback_filings_keeps_foreign_forms():
    rows = normalize_fallback_filings(
        "basf",
        COMPANIES["basf"],
        "1024148",
        _sample_submissions(),
        max_filings=10,
    )

    assert [row.filing_type for row in rows] == ["20-F", "6-K"]
    assert "fallback foreign-filer pull" in rows[0].notes


def test_covestro_ir_url_classification():
    assert (
        _infer_covestro_form_type(
            "https://annualreport.covestro.com/annual-financial-report-2025/en/"
        )
        == "IR-FY"
    )
    assert (
        _infer_covestro_form_type(
            "https://annualreport.covestro.com/quarterly-statement-q1-2025/en/"
        )
        == "IR-Q1"
    )
    assert (
        _infer_covestro_form_type(
            "https://annualreport.covestro.com/half-year-financial-report-2025/en/"
        )
        == "IR-HY"
    )


def test_missing_sec_cik_record_makes_tosoh_auditable():
    row = missing_sec_cik_record("tosoh", COMPANIES["tosoh"])

    assert row.company_name == "Tosoh Corporation"
    assert row.filing_type == MISSING_SEC_CIK_TYPE
    assert "No SEC CIK found" in row.notes
