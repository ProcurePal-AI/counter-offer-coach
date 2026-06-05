"""
sec_edgar.py -- Pull producer filing metadata from SEC EDGAR.
sec_edgar.py -- 从 SEC EDGAR 拉取生产商/竞争对手的申报文件元数据。

Tracks public filings for producer/competitor companies that inform the
should-cost model. SEC EDGAR is free, but automated requests must identify the
caller with a descriptive User-Agent that includes contact information.

这个文件的用途：
  - 先确认 BASF、Covestro、Huntsman、Tosoh 在 EDGAR 里能查到哪些 filings（在哪里可以找到这些）
  - 再决定后续下载哪些年报/季报文件。
  - 当前版本会保存 10-K/10-Q metadata；加 --download 时会下载原始 filing 文件。

Initial scope:
  1. BASF       -- foreign filer; expect 20-F/6-K rather than 10-K/10-Q
  2. Covestro   -- German company; supplement with Frankfurt/IR filings later
  3. Huntsman   -- US filer; expect 10-K/10-Q
  4. Tosoh      -- foreign company; EDGAR availability must be confirmed

Run:
  python pipeline/sec_edgar.py
  python pipeline/sec_edgar.py --download
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests

SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
SEC_COMPANY_TICKERS_EXCHANGE = "https://www.sec.gov/files/company_tickers_exchange.json"
SOURCE = "SEC EDGAR"
COVESTRO_IR_SOURCE = "Covestro IR"
COVESTRO_REPORTS_URL = "https://www.covestro.com/en/investors/reports-and-presentations"
MISSING_SEC_CIK_TYPE = "NO-SEC-CIK"
DEFAULT_MAX_FILINGS_PER_COMPANY = 12
REQUEST_SLEEP_SECONDS = 0.2

# SEC requires automated tools to identify themselves.
# SEC 要求自动化请求必须声明 User-Agent，通常包含项目名和联系邮箱。
#
# Set this before real pulls:
#   export SEC_USER_AGENT="CounterOfferCoach/0.1 Lujia Chen lujia.chen.luke@gmail.com"
USER_AGENT_ENV = "SEC_USER_AGENT"
DEFAULT_USER_AGENT = "CounterOfferCoach/0.1 Lujia Chen lujia.chen.luke@gmail.com"


@dataclass(frozen=True)
class CompanyConfig:
    """Configuration for one company we want to audit.

    每家公司一条配置：
      - name: 公司正式名称
      - cik: SEC 公司识别码；先留空，下一步审计时补上
      - ticker: 股票代码；没有或不确定就先留空
      - target_forms: 我们想关注的 filing 类型
      - fallback_forms: 10-K/10-Q 找不到时，foreign filer 可改查的等价类型
      - notes: 为什么这样拉、有什么特殊情况
    """

    name: str
    cik: str | None
    ticker: str | None
    target_forms: tuple[str, ...]
    fallback_forms: tuple[str, ...]
    cik_lookup_terms: tuple[str, ...]
    ir_reports_url: str | None
    notes: str


COMPANIES: dict[str, CompanyConfig] = {
    # BASF is a foreign filer. Foreign private issuers normally file 20-F/6-K,
    # not US domestic 10-K/10-Q reports.
    # BASF 是外国发行人，所以重点不是 10-K/10-Q，而是 20-F/6-K。
    "basf": CompanyConfig(
        name="BASF SE",
        cik="1024148",
        ticker=None,
        target_forms=("10-K", "10-K/A", "10-Q", "10-Q/A"),
        fallback_forms=("20-F", "20-F/A", "6-K", "6-K/A"),
        cik_lookup_terms=("BASF SE", "BASF"),
        ir_reports_url=None,
        notes=(
            "Foreign filer; first checks 10-K/10-Q, then falls back to 20-F/6-K "
            "when EDGAR has no domestic issuer forms."
        ),
    ),
    # Covestro may not have the same SEC coverage as a US issuer. We keep it in
    # the EDGAR audit first, then add Frankfurt/IR sources if EDGAR is incomplete.
    # Covestro 是德国公司；先查 EDGAR，如果不完整，再补 Frankfurt/公司 IR 文件。
    "covestro": CompanyConfig(
        name="Covestro AG",
        cik=None,
        ticker=None,
        target_forms=("10-K", "10-K/A", "10-Q", "10-Q/A"),
        fallback_forms=(),
        cik_lookup_terms=("Covestro AG", "Covestro"),
        ir_reports_url=COVESTRO_REPORTS_URL,
        notes=(
            "German company; SEC 10-K/10-Q is unlikely, so supplement with "
            "official Covestro IR financial reports."
        ),
    ),
    # Huntsman is the straightforward US-listed company in this group.
    # Huntsman 是美国上市公司，正常应该有 10-K 和 10-Q。
    "huntsman": CompanyConfig(
        name="Huntsman Corporation",
        cik=None,
        ticker="HUN",
        target_forms=("10-K", "10-K/A", "10-Q", "10-Q/A"),
        fallback_forms=(),
        cik_lookup_terms=("Huntsman Corporation", "Huntsman"),
        ir_reports_url=None,
        notes="US filer; standard 10-K/10-Q pull.",
    ),
    # Tosoh is a foreign company. We need to confirm whether it has an EDGAR CIK.
    # Tosoh 是日本公司；下一步要确认 SEC 里是否有对应 CIK。
    "tosoh": CompanyConfig(
        name="Tosoh Corporation",
        cik=None,
        ticker=None,
        target_forms=("10-K", "10-K/A", "10-Q", "10-Q/A"),
        fallback_forms=("20-F", "20-F/A", "6-K", "6-K/A"),
        cik_lookup_terms=("Tosoh Corporation", "Tosoh"),
        ir_reports_url=None,
        notes="Foreign company; checks 10-K/10-Q first, then 20-F/6-K if a CIK exists.",
    ),
}


@dataclass(frozen=True)
class FilingRecord:
    """One normalized filing row ready to save.

    这是一条已经整理好的 filing 记录：
      - metadata 会写进 CSV
      - source_url 可以直接打开 SEC 原始文件
      - local_file_path 只有下载文件后才会有值
    """

    company_key: str
    company_name: str
    source: str
    cik: str
    ticker: str | None
    filing_type: str
    filing_date: str
    period_end_date: str | None
    accession_number: str
    primary_document: str
    source_url: str
    local_file_path: str | None
    fetched_at: str
    notes: str


class LinkCollector(HTMLParser):
    """Tiny HTML link collector so we do not need BeautifulSoup.

    简单收集页面里的链接，避免为这一个功能额外增加 BeautifulSoup 依赖。
    """

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        href = attrs_dict.get("href")
        if href:
            self._current_href = href
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._text_parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href:
            text = " ".join(part for part in self._text_parts if part)
            self.links.append((self._current_href, text))
            self._current_href = None
            self._text_parts = []


def _padded_cik(cik: str) -> str:
    """SEC submission URLs require CIKs padded to 10 digits.

    SEC 的 submissions API 要求 CIK 是 10 位数字；不足 10 位就在左边补 0。
    Example: "51143" -> "0000051143"
    """

    return cik.zfill(10)


def _headers(user_agent: str) -> dict[str, str]:
    return {"User-Agent": user_agent}


def _request_json(url: str, user_agent: str) -> dict:
    """Small wrapper for SEC JSON requests.

    SEC API 请求的小包装：
      - 统一加 User-Agent
      - 统一 timeout
      - 失败时抛出明确异常
    """

    r = requests.get(url, headers=_headers(user_agent), timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_company_index(user_agent: str) -> dict:
    """Fetch SEC's ticker/exchange/company-name index.

    拉取 SEC 官方公司索引，用它把 ticker 或公司名解析成 CIK。
    """

    return _request_json(SEC_COMPANY_TICKERS_EXCHANGE, user_agent)


def fetch_submissions(cik: str, user_agent: str) -> dict:
    """Fetch SEC submissions metadata for one CIK.

    拉取某一个 CIK 的 filing metadata。
    返回的是 JSON 字典，里面会包含公司名称、最近 filings、历史 filings 等。
    这一步只拿 metadata，不下载具体 HTML/TXT filing 文件。
    """

    url = f"{SEC_SUBMISSIONS_BASE}/CIK{_padded_cik(cik)}.json"
    return _request_json(url, user_agent)


def resolve_cik(company: CompanyConfig, company_index: dict) -> str | None:
    """Resolve a company config to a CIK using SEC's public company index.

    用 SEC 官方 company_tickers_exchange.json 自动找 CIK：
      1. 如果配置里已经写了 CIK，直接用
      2. 如果有 ticker，优先按 ticker 匹配
      3. 否则按公司名称关键词模糊匹配
    """

    if company.cik:
        return company.cik

    fields = company_index.get("fields", [])
    rows = company_index.get("data", [])
    records = [dict(zip(fields, row)) for row in rows]

    if company.ticker:
        ticker = company.ticker.upper()
        for record in records:
            if str(record.get("ticker", "")).upper() == ticker:
                return str(record["cik"])

    for term in company.cik_lookup_terms:
        term_normalized = term.casefold()
        for record in records:
            name = str(record.get("name", "")).casefold()
            if term_normalized in name:
                return str(record["cik"])

    return None


def _archive_url(cik: str, accession_number: str, primary_document: str) -> str:
    """Build the direct SEC Archives URL for a filing document.

    用 CIK + accession number + primary document 拼出 SEC 原始文件地址。
    """

    accession_dir = accession_number.replace("-", "")
    return (
        f"{SEC_ARCHIVES_BASE}/{int(cik)}/{accession_dir}/"
        f"{quote(primary_document, safe='._-/')}"
    )


def normalize_target_filings(
    company_key: str,
    company: CompanyConfig,
    cik: str,
    submissions: dict,
    max_filings: int,
) -> list[FilingRecord]:
    """Filter SEC submissions to the target forms and normalize rows.

    从 SEC submissions JSON 里筛选目标 filing 类型，并整理成统一 CSV 行。
    """

    return _normalize_filings_for_forms(
        company_key=company_key,
        company=company,
        cik=cik,
        submissions=submissions,
        forms_to_keep=company.target_forms,
        max_filings=max_filings,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        note_suffix="primary 10-K/10-Q pull",
    )


def normalize_fallback_filings(
    company_key: str,
    company: CompanyConfig,
    cik: str,
    submissions: dict,
    max_filings: int,
) -> list[FilingRecord]:
    """Filter SEC submissions to foreign-filer fallback forms.

    如果 10-K/10-Q 没有结果，用这个函数改查 20-F/6-K。
    """

    return _normalize_filings_for_forms(
        company_key=company_key,
        company=company,
        cik=cik,
        submissions=submissions,
        forms_to_keep=company.fallback_forms,
        max_filings=max_filings,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        note_suffix="fallback foreign-filer pull",
    )


def _normalize_filings_for_forms(
    company_key: str,
    company: CompanyConfig,
    cik: str,
    submissions: dict,
    forms_to_keep: tuple[str, ...],
    max_filings: int,
    fetched_at: str,
    note_suffix: str,
) -> list[FilingRecord]:
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_documents = recent.get("primaryDocument", [])

    rows: list[FilingRecord] = []
    target_forms = set(forms_to_keep)

    for form, filing_date, report_date, accession, primary_document in zip(
        forms, filing_dates, report_dates, accessions, primary_documents
    ):
        if form not in target_forms:
            continue

        source_url = _archive_url(cik, accession, primary_document)
        rows.append(
            FilingRecord(
                company_key=company_key,
                company_name=company.name,
                source=SOURCE,
                cik=_padded_cik(cik),
                ticker=company.ticker,
                filing_type=form,
                filing_date=filing_date,
                period_end_date=report_date or None,
                accession_number=accession,
                primary_document=primary_document,
                source_url=source_url,
                local_file_path=None,
                fetched_at=fetched_at,
                notes=f"{company.notes} ({note_suffix})",
            )
        )

        if len(rows) >= max_filings:
            break

    return rows


def _infer_covestro_form_type(url: str) -> str | None:
    """Classify Covestro IR URLs into annual/interim filing-like buckets.

    Covestro 不是 SEC 10-K/10-Q filer，所以这里把官方 IR 文件归类成：
      - IR-FY: annual report / full-year report
      - IR-HY: half-year financial report
      - IR-Q1 / IR-Q3: quarterly statements
    """

    normalized = url.casefold()
    if "annual-financial-report" in normalized or "annual-report" in normalized:
        return "IR-FY"
    if "half-year-financial-report" in normalized or "_q2_" in normalized:
        return "IR-HY"
    if "quarterly-statement-q1" in normalized or "_q1_" in normalized:
        return "IR-Q1"
    if "quarterly-statement-q3" in normalized or "_q3_" in normalized:
        return "IR-Q3"
    return None


def _infer_year(url: str) -> str | None:
    match = re.search(r"20\d{2}", url)
    return match.group(0) if match else None


def missing_sec_cik_record(company_key: str, company: CompanyConfig) -> FilingRecord:
    """Create an audit row when SEC has no CIK for a configured company.

    如果某家公司在 SEC company index 里找不到 CIK，也写一条记录进 CSV。
    这样 Tosoh 这类“没有 EDGAR 可拉”的结果可以被审计，而不只是终端日志。
    """

    return FilingRecord(
        company_key=company_key,
        company_name=company.name,
        source=SOURCE,
        cik="",
        ticker=company.ticker,
        filing_type=MISSING_SEC_CIK_TYPE,
        filing_date="",
        period_end_date=None,
        accession_number="",
        primary_document="",
        source_url="",
        local_file_path=None,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        notes=(
            "No SEC CIK found via SEC company_tickers_exchange.json; "
            "no SEC 10-K/10-Q or 20-F/6-K filings were pulled."
        ),
    )


def fetch_covestro_ir_filings(user_agent: str, max_filings: int) -> list[FilingRecord]:
    """Pull Covestro official IR financial report links.

    从 Covestro 官方 reports-and-presentations 页面抓取 FY/Q1/HY/Q3 财务报告链接。
    这是 Covestro 的 Frankfurt/IR supplement，不是 SEC EDGAR 数据。
    """

    r = requests.get(COVESTRO_REPORTS_URL, headers=_headers(user_agent), timeout=30)
    r.raise_for_status()

    parser = LinkCollector()
    parser.feed(r.text)

    fetched_at = datetime.now(timezone.utc).isoformat()
    rows: list[FilingRecord] = []
    seen: set[str] = set()

    for href, text in parser.links:
        url = urljoin(COVESTRO_REPORTS_URL, href)
        form_type = _infer_covestro_form_type(url)
        if not form_type or url in seen:
            continue
        seen.add(url)

        parsed = urlparse(url)
        primary_document = Path(parsed.path).name or "web-report"
        year = _infer_year(url)
        rows.append(
            FilingRecord(
                company_key="covestro",
                company_name="Covestro AG",
                source=COVESTRO_IR_SOURCE,
                cik="",
                ticker=None,
                filing_type=form_type,
                filing_date="",
                period_end_date=year,
                accession_number="",
                primary_document=primary_document,
                source_url=url,
                local_file_path=None,
                fetched_at=fetched_at,
                notes=(
                    "Official Covestro IR financial report supplement; "
                    f"link_text={text or 'n/a'}"
                ),
            )
        )

        if len(rows) >= max_filings:
            break

    return rows


def download_filing(record: FilingRecord, user_agent: str, out_dir: Path) -> FilingRecord:
    """Download one filing document and return the row with local_file_path set.

    下载一份 SEC 原始 filing 文件，并把本地路径写回 FilingRecord。
    """

    company_dir = out_dir / record.company_key / record.filing_type.replace("/", "_")
    company_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(record.primary_document).suffix or ".html"
    if record.accession_number:
        file_name = f"{record.filing_date}_{record.accession_number.replace('-', '')}{suffix}"
    else:
        year = record.period_end_date or "unknown-period"
        url_hash = hashlib.sha1(record.source_url.encode("utf-8")).hexdigest()[:10]
        stem = Path(urlparse(record.source_url).path.rstrip("/")).name or "web-report"
        file_name = f"{year}_{stem}_{url_hash}{suffix}"
    local_path = company_dir / file_name

    if not local_path.exists():
        r = requests.get(record.source_url, headers=_headers(user_agent), timeout=60)
        r.raise_for_status()
        local_path.write_bytes(r.content)
        time.sleep(REQUEST_SLEEP_SECONDS)

    return FilingRecord(
        company_key=record.company_key,
        company_name=record.company_name,
        source=record.source,
        cik=record.cik,
        ticker=record.ticker,
        filing_type=record.filing_type,
        filing_date=record.filing_date,
        period_end_date=record.period_end_date,
        accession_number=record.accession_number,
        primary_document=record.primary_document,
        source_url=record.source_url,
        local_file_path=str(local_path),
        fetched_at=record.fetched_at,
        notes=record.notes,
    )


def write_filing_metadata(rows: list[FilingRecord], out_path: Path) -> None:
    """Write normalized filing rows to CSV.

    把筛选后的 filing metadata 写成 CSV，后面可以再接 producer_filings 数据表。
    """

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(FilingRecord.__dataclass_fields__.keys())
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def print_company_audit_plan() -> None:
    """Print the current audit plan before we perform real SEC requests.

    打印当前审计计划，方便先人工审核：
      - 哪些公司会被检查
      - 当前 CIK 是否已经补上
      - 目标 filing 类型是什么
    """

    print("SEC EDGAR company audit plan:")
    for key, company in COMPANIES.items():
        cik = company.cik or "TODO"
        ticker = company.ticker or "n/a"
        forms = ", ".join(company.target_forms)
        fallback = ", ".join(company.fallback_forms) if company.fallback_forms else "none"
        print(f"  {key}: {company.name} | CIK={cik} | ticker={ticker} | forms={forms}")
        print(f"    fallback_forms={fallback}")
        if company.ir_reports_url:
            print(f"    ir_reports_url={company.ir_reports_url}")
        print(f"    {company.notes}")


def pull_covestro_ir_supplement(
    user_agent: str,
    max_filings: int,
    download: bool,
    filing_dir: Path,
) -> list[FilingRecord]:
    """Pull Covestro official IR supplement rows.

    Covestro 没有 SEC CIK 时也要执行这段，因为它是非 SEC 的官方 IR 补充源。
    """

    print("    pulling Covestro IR financial report supplement...")
    ir_rows = fetch_covestro_ir_filings(user_agent, max_filings)
    print(f"    {len(ir_rows)} Covestro IR report links found")
    if download:
        downloaded_ir: list[FilingRecord] = []
        for row in ir_rows:
            downloaded_ir.append(download_filing(row, user_agent, filing_dir))
        return downloaded_ir
    return ir_rows


def pull_all(user_agent: str, max_filings: int, download: bool) -> list[FilingRecord]:
    """Resolve CIKs, pull SEC metadata, optionally download filing documents.

    完整执行流程：
      1. 读取 SEC 公司索引
      2. 给每家公司找 CIK
      3. 拉 submissions metadata
      4. 筛选 10-K / 10-Q
      5. 可选下载原始 filing 文件
    """

    base_dir = Path(__file__).resolve().parents[1]
    metadata_path = base_dir / "data" / "producer_filings_sec_edgar.csv"
    filing_dir = base_dir / "data" / "sec_edgar_filings"

    print("Pulling SEC company index...")
    company_index = fetch_company_index(user_agent)

    all_rows: list[FilingRecord] = []
    for key, company in COMPANIES.items():
        print(f"  resolving {company.name}...")
        cik = resolve_cik(company, company_index)
        if not cik:
            print("    no CIK found for SEC filings")
            all_rows.append(missing_sec_cik_record(key, company))
            if key == "covestro" and company.ir_reports_url:
                all_rows.extend(
                    pull_covestro_ir_supplement(user_agent, max_filings, download, filing_dir)
                )
            continue

        print(f"    CIK {cik}; pulling submissions...")
        submissions = fetch_submissions(cik, user_agent)
        rows = normalize_target_filings(key, company, cik, submissions, max_filings)
        print(f"    {len(rows)} 10-K/10-Q filings found")
        if not rows and company.fallback_forms:
            rows = normalize_fallback_filings(key, company, cik, submissions, max_filings)
            print(f"    {len(rows)} fallback 20-F/6-K filings found")

        if download:
            downloaded: list[FilingRecord] = []
            for row in rows:
                downloaded.append(download_filing(row, user_agent, filing_dir))
            rows = downloaded

        all_rows.extend(rows)
        time.sleep(REQUEST_SLEEP_SECONDS)

        if key == "covestro" and company.ir_reports_url:
            all_rows.extend(
                pull_covestro_ir_supplement(user_agent, max_filings, download, filing_dir)
            )

    write_filing_metadata(all_rows, metadata_path)
    print(f"Wrote {len(all_rows)} rows -> {metadata_path}")
    return all_rows


def main() -> int:
    """Entry point for running this file as a script.

    当前版本已经会：
      1. 自动解析 CIK
      2. 调 SEC API
      3. 过滤 10-K / 10-Q
      4. 保存 metadata
      5. 按参数可选下载原始 filing 文件
    """

    parser = argparse.ArgumentParser(description="Pull SEC EDGAR 10-K/10-Q filings.")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download raw SEC filing documents in addition to writing metadata.",
    )
    parser.add_argument(
        "--max-filings",
        type=int,
        default=DEFAULT_MAX_FILINGS_PER_COMPANY,
        help="Maximum target filings to keep per company.",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Only print the company audit plan; do not call SEC APIs.",
    )
    args = parser.parse_args()

    user_agent = os.environ.get(USER_AGENT_ENV, DEFAULT_USER_AGENT)

    print_company_audit_plan()
    if args.plan_only:
        return 0

    rows = pull_all(user_agent=user_agent, max_filings=args.max_filings, download=args.download)
    if not rows:
        print("WARNING: no 10-K/10-Q rows found for the configured companies.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
