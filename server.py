#!/usr/bin/env python3
"""MCP server: English-language access to Japanese company earnings via EDINET.

Tool: get_english_earnings(ticker) -> latest 有価証券報告書 (yuho / annual
securities report) key financial figures for a Japanese ticker, in English.

Two independent pieces:
  1. Ticker -> EDINET code resolution. Uses a free, keyless CSV mirror of the
     official EDINET code list. No EDINET_API_KEY required.
  2. EDINET API v2 (document search + XBRL download). Requires a
     Subscription-Key, read from the EDINET_API_KEY environment variable.
"""

import base64
import csv
import io
import os
import re
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

import requests
from lxml import etree, html
from mcp.server.fastmcp import FastMCP
from mcp.types import Icon, ToolAnnotations
from pydantic import Field
from typing_extensions import TypedDict


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a .env file next to this script into
    os.environ, only when the key isn't already set. Dependency-free."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
SECCODE_CSV_URL = "https://code4fukui.github.io/EDINET/data/seccode.csv"
DEEPSEEK_API_BASE = "https://api.deepseek.com/chat/completions"
DEEPSEEK_TIMEOUT_SECONDS = 60

CACHE_DIR = Path(__file__).parent / ".cache"
SECCODE_CACHE_PATH = CACHE_DIR / "seccode.csv"
SECCODE_CACHE_TTL_SECONDS = 24 * 60 * 60  # 1 day

HTTP_TIMEOUT_SECONDS = 20
DOC_SEARCH_MAX_DAYS = 130  # scan window past a fiscal year end, per fiscal year tried
DOC_SEARCH_MAX_FISCAL_YEARS_BACK = 2
DOC_SEARCH_REQUEST_DELAY_SECONDS = 0.15

YUHO_DOC_TYPE_CODE = "120"  # 有価証券報告書


class ConfigError(Exception):
    """Raised when required configuration (the EDINET API key) is missing."""


def get_edinet_api_key() -> str:
    """Read the EDINET Subscription-Key from the environment.

    Raises ConfigError with a clear, actionable message if it isn't set.
    This is only called right before an EDINET API v2 request is made --
    ticker resolution does not need it.
    """
    key = os.environ.get("EDINET_API_KEY")
    if not key or not key.strip():
        raise ConfigError(
            "EDINET_API_KEY environment variable is not set. "
            "Get a free Subscription-Key by registering at the EDINET API "
            "developer portal (https://api.edinet-fsa.go.jp/), then set it, e.g.:\n"
            "  export EDINET_API_KEY=your-subscription-key\n"
            "Ticker-to-EDINET-code lookup works without this key; fetching "
            "document lists and XBRL filings requires it."
        )
    return key.strip()


# --------------------------------------------------------------------------
# Ticker -> EDINET code resolution (no API key required)
# --------------------------------------------------------------------------


class TickerNotFoundError(Exception):
    pass


def _download_seccode_csv() -> str:
    resp = requests.get(SECCODE_CSV_URL, timeout=HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.content.decode("utf-8-sig")


def _load_seccode_csv_text() -> str:
    """Return the EDINET code list CSV text, using a local cache when fresh."""
    if SECCODE_CACHE_PATH.exists():
        age = time.time() - SECCODE_CACHE_PATH.stat().st_mtime
        if age < SECCODE_CACHE_TTL_SECONDS:
            return SECCODE_CACHE_PATH.read_text(encoding="utf-8-sig")

    text = _download_seccode_csv()
    CACHE_DIR.mkdir(exist_ok=True)
    SECCODE_CACHE_PATH.write_text(text, encoding="utf-8")
    return text


def resolve_ticker(ticker: str) -> dict:
    """Resolve a 4-digit TSE ticker to its EDINET code and company info.

    Source: a free, keyless mirror of the official EDINET code list
    (https://disclosure2.edinet-fsa.go.jp/) published at
    https://code4fukui.github.io/EDINET/ . Cached locally for a day.
    """
    ticker = ticker.strip()
    if not ticker.isdigit() or len(ticker) != 4:
        raise ValueError(f"Ticker must be a 4-digit TSE securities code, got: {ticker!r}")

    text = _load_seccode_csv_text()
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        sec_code = (row.get("証券コード") or "").strip()
        if sec_code[:4] == ticker:
            return {
                "ticker": ticker,
                "edinet_code": row["ＥＤＩＮＥＴコード"].strip(),
                "company_name_ja": row["提出者名"].strip(),
                "company_name_en": row["提出者名（英字）"].strip(),
                "industry_ja": row.get("提出者業種", "").strip(),
                "fiscal_year_end_ja": row.get("決算日", "").strip(),  # e.g. "3月31日"
                "listing_status_ja": row.get("上場区分", "").strip(),
            }

    raise TickerNotFoundError(
        f"Ticker {ticker} not found in the EDINET code list. It may be delisted, "
        f"not a TSE-listed operating company, or the local mirror may be stale."
    )


def _parse_fiscal_year_end(fiscal_year_end_ja: str) -> tuple:
    """Parse '3月31日' -> (3, 31). Falls back to (3, 31) if unparseable."""
    try:
        month_part, day_part = fiscal_year_end_ja.replace("日", "").split("月")
        return int(month_part), int(day_part)
    except (ValueError, AttributeError):
        return 3, 31


# --------------------------------------------------------------------------
# EDINET API v2: document search + download (requires EDINET_API_KEY)
# --------------------------------------------------------------------------


class DocumentNotFoundError(Exception):
    pass


class EdinetApiError(Exception):
    pass


def _edinet_get(path: str, params: dict, api_key: str, raw: bool = False):
    params = dict(params)
    params["Subscription-Key"] = api_key
    url = f"{EDINET_API_BASE}{path}"
    try:
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise EdinetApiError(f"Network error calling EDINET API ({path}): {e}") from e

    if resp.status_code == 401:
        raise ConfigError(
            "EDINET rejected the Subscription-Key in EDINET_API_KEY as invalid. "
            "Check that the key is correct and that your EDINET API subscription is active."
        )
    if resp.status_code != 200:
        raise EdinetApiError(
            f"EDINET API returned HTTP {resp.status_code} for {path}: {resp.text[:300]}"
        )
    return resp.content if raw else resp.json()


def _list_documents_for_date(day: date, api_key: str) -> list:
    data = _edinet_get(
        "/documents.json",
        {"date": day.isoformat(), "type": 2},
        api_key,
    )
    results = data.get("results") or []
    return results


def find_latest_yuho(edinet_code: str, fiscal_year_end_ja: str, api_key: str) -> dict:
    """Scan EDINET's daily document lists for the most recent 有価証券報告書
    (docTypeCode 120) filed by this EDINET code.

    EDINET API v2 has no "search by company" endpoint, so this walks
    candidate filing-window dates (fiscal year end + up to ~130 days, the
    statutory 3-month deadline plus buffer) newest-first, for up to the last
    two completed fiscal years, and stops at the first match.
    """
    month, day_num = _parse_fiscal_year_end(fiscal_year_end_ja)
    today = date.today()

    def _safe_date(year: int) -> date:
        try:
            return date(year, month, day_num)
        except ValueError:  # Feb 29 in a non-leap year
            return date(year, month, day_num - 1)

    most_recent_completed_fy_year = today.year if _safe_date(today.year) <= today else today.year - 1

    for years_back in range(DOC_SEARCH_MAX_FISCAL_YEARS_BACK):
        fy_end = _safe_date(most_recent_completed_fy_year - years_back)

        window_start = fy_end + timedelta(days=1)
        window_end = min(fy_end + timedelta(days=DOC_SEARCH_MAX_DAYS), today)
        if window_end < window_start:
            continue

        candidate_day = window_end
        while candidate_day >= window_start:
            results = _list_documents_for_date(candidate_day, api_key)
            for doc in results:
                if (
                    doc.get("edinetCode") == edinet_code
                    and doc.get("docTypeCode") == YUHO_DOC_TYPE_CODE
                ):
                    return doc
            time.sleep(DOC_SEARCH_REQUEST_DELAY_SECONDS)
            candidate_day -= timedelta(days=1)

    raise DocumentNotFoundError(
        f"No 有価証券報告書 (yuho) found for EDINET code {edinet_code} in the last "
        f"{DOC_SEARCH_MAX_FISCAL_YEARS_BACK} fiscal-year filing windows "
        f"(fiscal year end: {fiscal_year_end_ja})."
    )


def download_xbrl(doc_id: str, api_key: str) -> bytes:
    """Download the XBRL bundle (type=1) for a document ID. Returns zip bytes."""
    return _edinet_get(f"/documents/{doc_id}", {"type": 1}, api_key, raw=True)


# --------------------------------------------------------------------------
# XBRL parsing (yuho "5-year summary of business results" facts)
# --------------------------------------------------------------------------

CURRENT_DURATION_CTXS = ["CurrentYearDuration", "CurrentYearDuration_NonConsolidatedMember"]
CURRENT_INSTANT_CTXS = ["CurrentYearInstant", "CurrentYearInstant_NonConsolidatedMember"]
PRIOR_DURATION_CTXS = ["Prior1YearDuration", "Prior1YearDuration_NonConsolidatedMember"]
PRIOR_INSTANT_CTXS = ["Prior1YearInstant", "Prior1YearInstant_NonConsolidatedMember"]

# Each concept: candidate element local names across JP-GAAP / IFRS / US-GAAP,
# tried in order, all from the jpcrp_cor "Summary of Business Results" table
# that every yuho filer includes regardless of accounting standard.
CONCEPTS = {
    "revenue": [
        "NetSalesSummaryOfBusinessResults",
        "RevenueIFRSSummaryOfBusinessResults",
        "RevenuesUSGAAPSummaryOfBusinessResults",
        "OperatingRevenuesSummaryOfBusinessResults",
        "NetSalesIFRS",
        "TotalNetRevenuesIFRS",
        "RevenueIFRS",
        "NetSales",
        "Revenues",
        "Revenue",
        # Banks don't report 売上高 (net sales) — their top line is 経常収益
        # (ordinary income/revenues), tagged under these bank-taxonomy names.
        "OrdinaryIncomeSummaryOfBusinessResults",
        "OrdinaryIncomeBNK",
    ],
    "operating_income": [
        "OperatingIncomeLossSummaryOfBusinessResults",
        "OperatingProfitLossIFRSSummaryOfBusinessResults",
        "OperatingIncomeLossUSGAAPSummaryOfBusinessResults",
        "OperatingProfitLossIFRS",
        "OperatingIncomeLoss",
        "OperatingIncome",
        "OperatingProfitLoss",
    ],
    "ordinary_income": [
        "OrdinaryIncomeLossSummaryOfBusinessResults",
        "OrdinaryIncomeLoss",
        "OrdinaryIncome",
    ],
    "net_income": [
        "ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
        "ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
        "NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults",
        "ProfitLossAttributableToOwnersOfParentIFRS",
        "ProfitLossAttributableToOwnersOfParent",
        "NetIncomeLoss",
        "ProfitLoss",
    ],
    "eps": [
        "BasicEarningsLossPerShareSummaryOfBusinessResults",
        "BasicEarningsLossPerShareIFRSSummaryOfBusinessResults",
        "BasicEarningsLossPerShareUSGAAPSummaryOfBusinessResults",
        "BasicEarningsLossPerShareIFRS",
        "BasicEarningsLossPerShare",
    ],
    "dividend_per_share": [
        "DividendPaidPerShareSummaryOfBusinessResults",
    ],
    "total_assets": [
        "TotalAssetsSummaryOfBusinessResults",
        "TotalAssetsIFRSSummaryOfBusinessResults",
        "AssetsIFRS",
        "Assets",
        "TotalAssets",
    ],
    "net_assets": [
        "NetAssetsSummaryOfBusinessResults",
        "EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
        "NetAssets",
        "EquityAttributableToOwnersOfParentIFRS",
    ],
}





def _extract_public_doc_xbrl(zip_bytes: bytes) -> bytes:
    """Pull the main instance XBRL file (under XBRL/PublicDoc/) out of the
    EDINET document zip."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        candidates = [
            name
            for name in zf.namelist()
            if "/PublicDoc/" in name and name.endswith(".xbrl")
        ]
        if not candidates:
            raise EdinetApiError(
                "Downloaded EDINET document zip did not contain a PublicDoc XBRL "
                "instance file; the document may not be in the expected format."
            )
        return zf.read(candidates[0])


def _build_fact_index(xbrl_bytes: bytes) -> dict:
    """Map (element_local_name, contextRef) -> raw text value for every fact."""
    root = etree.fromstring(xbrl_bytes)
    index = {}
    for elem in root.iter():
        context_ref = elem.get("contextRef")
        if context_ref is None or elem.text is None:
            continue
        local_name = etree.QName(elem.tag).localname
        index.setdefault((local_name, context_ref), elem.text.strip())
    return index


def _lookup_concept(index: dict, element_names: list, context_refs: list):
    """Try every candidate element name at each context, context by context.

    context_refs is ordered consolidated-first, non-consolidated-fallback
    last (see CURRENT_*_CTXS / PRIOR_*_CTXS). Context must be the outer loop:
    many IFRS filers tag a JP-GAAP-named concept (e.g.
    NetAssetsSummaryOfBusinessResults) only in the mandatory non-consolidated
    parent-company table, while the true consolidated figure lives under a
    differently-named IFRS concept (e.g.
    EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults). Looping
    name-then-context would match the non-consolidated JP-GAAP name first and
    silently return the wrong (parent-only) figure instead of falling
    through to the correct consolidated IFRS name.
    """
    for ctx in context_refs:
        for name in element_names:
            value = index.get((name, ctx))
            if value is not None:
                try:
                    return float(value)
                except ValueError:
                    continue
    return None


def extract_key_figures(xbrl_bytes: bytes) -> dict:
    index = _build_fact_index(xbrl_bytes)
    figures = {}
    for concept, element_names in CONCEPTS.items():
        is_instant = concept in ("total_assets", "net_assets")
        current_ctxs = CURRENT_INSTANT_CTXS if is_instant else CURRENT_DURATION_CTXS
        prior_ctxs = PRIOR_INSTANT_CTXS if is_instant else PRIOR_DURATION_CTXS

        current = _lookup_concept(index, element_names, current_ctxs)
        prior = _lookup_concept(index, element_names, prior_ctxs)

        yoy_pct = None
        if current is not None and prior not in (None, 0):
            yoy_pct = round((current - prior) / abs(prior) * 100, 2)

        figures[concept] = {
            "value": current,
            "prior_year_value": prior,
            "yoy_change_pct": yoy_pct,
        }
    return figures


# --------------------------------------------------------------------------
# Narrative extraction (事業の状況 / リスク情報 / 経営方針) + Deepseek translation
# --------------------------------------------------------------------------

# Canonicalize the various headings EDINET filers use for the same section
# (e.g. some filers write "事業等のリスク" instead of "リスク情報") down to one
# key per conceptual section, and give each an English label.
NARRATIVE_HEADING_CANONICAL = {
    "事業の状況": "事業の状況",
    "リスク情報": "リスク情報",
    "事業等のリスク": "リスク情報",
    "経営方針": "経営方針",
}
NARRATIVE_SECTION_EN_LABELS = {
    "事業の状況": "Business Overview",
    "リスク情報": "Risk Information",
    "経営方針": "Management Policy",
}
NARRATIVE_HEADING_RE = re.compile(r"第?\d*\s*(事業の状況|リスク情報|事業等のリスク|経営方針)")
MAJOR_HEADING_RE = re.compile(r"第\d+")
NARRATIVE_SECTION_MAX_CHARS = 4000
# Below this length, a heading match is almost certainly a table-of-contents
# listing (heading immediately followed by another heading) rather than the
# start of the actual section body.
NARRATIVE_SECTION_MIN_CHARS = 200


def _extract_narrative_sections(zip_bytes: bytes) -> list:
    """Pull the 事業の状況 / リスク情報 / 経営方針 narrative sections out of the
    honbun (main body) iXBRL files in an EDINET document zip, as plain text.

    Returns a list of {"section_ja": ..., "text_ja": ...} dicts, one per
    conceptual section found (deduplicated across the various headings
    filers use for the same section).
    """
    full_text_parts = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = sorted(
            name
            for name in zf.namelist()
            if "_honbun_" in name and name.endswith(".htm")
        )
        for name in names:
            raw = zf.read(name)
            try:
                tree = html.fromstring(raw)
            except Exception:
                continue
            full_text_parts.append(tree.text_content())

    full_text = "\n".join(full_text_parts)
    if not full_text.strip():
        return []

    matches = list(NARRATIVE_HEADING_RE.finditer(full_text))

    # Each heading (e.g. "事業の状況") typically appears once in the table of
    # contents and again at the start of its actual body -- the ToC
    # occurrence is immediately followed by the next heading, so it yields
    # almost no text. Pick, per canonical section, the first occurrence
    # whose captured text clears the ToC-vs-body length threshold, falling
    # back to the longest candidate if none do.
    candidates_by_canonical = {}
    for i, m in enumerate(matches):
        canonical = NARRATIVE_HEADING_CANONICAL[m.group(1)]

        end = len(full_text)
        next_major = MAJOR_HEADING_RE.search(full_text, m.end())
        if next_major:
            end = next_major.start()
        if i + 1 < len(matches):
            end = min(end, matches[i + 1].start())

        text_ja = full_text[m.end():end].strip()[:NARRATIVE_SECTION_MAX_CHARS]
        if not text_ja:
            continue
        candidates_by_canonical.setdefault(canonical, []).append(text_ja)

    sections = []
    for canonical, texts in candidates_by_canonical.items():
        best = next((t for t in texts if len(t) >= NARRATIVE_SECTION_MIN_CHARS), None)
        if best is None:
            best = max(texts, key=len)
        sections.append({"section_ja": canonical, "text_ja": best})

    return sections


def _translate_ja_to_en(text_ja: str, api_key: str):
    """Translate Japanese text to English via the Deepseek chat API.

    Returns None on any failure instead of raising, so callers can fall
    back to returning the untranslated Japanese text (partial results).
    """
    try:
        resp = requests.post(
            DEEPSEEK_API_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Translate the following Japanese financial disclosure "
                            "into professional, fluent English. Keep company names, "
                            "figures, and proper nouns as-is. Output only the "
                            "translation.\n\n" + text_ja
                        ),
                    }
                ],
                "temperature": 0.2,
            },
            timeout=DEEPSEEK_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return None


# --------------------------------------------------------------------------
# Output schemas (TypedDicts -- FastMCP derives each tool's outputSchema
# from these return-type annotations)
# --------------------------------------------------------------------------


class EdinetCodeResult(TypedDict):
    ticker: str
    edinet_code: str
    company_name_ja: str
    company_name_en: str
    industry_ja: str
    fiscal_year_end_ja: str
    listing_status_ja: str


class FigureDetail(TypedDict):
    value: float | None
    prior_year_value: float | None
    yoy_change_pct: float | None


class EarningsFigures(TypedDict):
    revenue: FigureDetail
    operating_income: FigureDetail
    ordinary_income: FigureDetail
    net_income: FigureDetail
    eps: FigureDetail
    dividend_per_share: FigureDetail
    total_assets: FigureDetail
    net_assets: FigureDetail


class EarningsResult(TypedDict):
    ticker: str
    company_name: str
    company_name_ja: str
    edinet_code: str
    document_type: str
    document_id: str
    period_end: str | None
    filed_date: str | None
    figures_currency: str
    figures: EarningsFigures


class NarrativeSection(TypedDict):
    section_ja: str
    section_en: str
    text_ja: str
    text_en: str | None


class NarrativeResult(TypedDict):
    ticker: str
    company_name_en: str
    document_id: str
    fiscal_year_end: str | None
    sections: list[NarrativeSection]


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------

# Minimal, dependency-free server icon: a red circle with "JP", inlined as an
# SVG data URL so no external image host is required.
_SERVER_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<circle cx="32" cy="32" r="32" fill="#bc002d"/>'
    '<text x="32" y="43" font-size="26" font-family="sans-serif" '
    'text-anchor="middle" fill="white">JP</text>'
    "</svg>"
)
SERVER_ICON = Icon(
    src="data:image/svg+xml;base64," + base64.b64encode(_SERVER_ICON_SVG.encode()).decode(),
    mimeType="image/svg+xml",
    sizes=["any"],
)

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

mcp = FastMCP(
    name="jp-earnings",
    instructions=(
        "Look up English-language financial figures and narrative disclosures "
        "for Japanese companies from their EDINET securities filings "
        "(有価証券報告書). Start with lookup_edinet_code to resolve a 4-digit "
        "TSE ticker to its EDINET code, then use get_english_earnings for "
        "financial statement figures or get_english_narrative for business "
        "overview / risk / management policy sections."
    ),
    website_url="https://github.com/jp-disclosures/jp-earnings",
    icons=[SERVER_ICON],
)


@mcp.tool(
    title="Look up EDINET code",
    annotations=READ_ONLY_ANNOTATIONS,
)
def lookup_edinet_code(
    ticker: Annotated[
        str,
        Field(description="4-digit Tokyo Stock Exchange securities code, e.g. '7203' for Toyota Motor."),
    ],
) -> EdinetCodeResult:
    """Resolve a 4-digit TSE ticker (e.g. '7203') to its EDINET code and
    company name. Does not require an EDINET API key."""
    return resolve_ticker(ticker)


@mcp.tool(
    title="Get English earnings figures",
    annotations=READ_ONLY_ANNOTATIONS,
)
def get_english_earnings(
    ticker: Annotated[
        str,
        Field(description="4-digit TSE securities code, e.g. '7203' for Toyota Motor."),
    ],
) -> EarningsResult:
    """Get the latest annual securities report (有価証券報告書) key
    financial figures for a Japanese ticker, translated to English field
    names. Requires the EDINET_API_KEY environment variable to be set."""
    api_key = get_edinet_api_key()  # fail fast with a clear message
    company = resolve_ticker(ticker)
    doc = find_latest_yuho(company["edinet_code"], company["fiscal_year_end_ja"], api_key)
    zip_bytes = download_xbrl(doc["docID"], api_key)
    xbrl_bytes = _extract_public_doc_xbrl(zip_bytes)
    figures = extract_key_figures(xbrl_bytes)

    return {
        "ticker": company["ticker"],
        "company_name": company["company_name_en"] or company["company_name_ja"],
        "company_name_ja": company["company_name_ja"],
        "edinet_code": company["edinet_code"],
        "document_type": "有価証券報告書 (annual securities report)",
        "document_id": doc["docID"],
        "period_end": doc.get("periodEnd"),
        "filed_date": doc.get("submitDateTime"),
        "figures_currency": "JPY",
        "figures": figures,
    }


@mcp.tool(
    title="Get English narrative disclosures",
    annotations=READ_ONLY_ANNOTATIONS,
)
def get_english_narrative(
    ticker: Annotated[
        str,
        Field(description="4-digit TSE securities code, e.g. '7203' for Toyota Motor."),
    ],
) -> NarrativeResult:
    """Get the latest annual securities report (有価証券報告書) narrative
    sections -- business overview / risk information / management policy --
    for a Japanese ticker, translated to English. Requires the
    EDINET_API_KEY environment variable. If DEEPSEEK_API_KEY is not set,
    translation is skipped and the untranslated Japanese text is returned
    instead (graceful degradation, no error)."""
    api_key = get_edinet_api_key()  # fail fast with a clear message
    company = resolve_ticker(ticker)
    doc = find_latest_yuho(company["edinet_code"], company["fiscal_year_end_ja"], api_key)
    zip_bytes = download_xbrl(doc["docID"], api_key)
    sections_ja = _extract_narrative_sections(zip_bytes)

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip() or None

    sections = []
    for section in sections_ja:
        text_en = _translate_ja_to_en(section["text_ja"], deepseek_key) if deepseek_key else None
        sections.append(
            {
                "section_ja": section["section_ja"],
                "section_en": NARRATIVE_SECTION_EN_LABELS.get(
                    section["section_ja"], section["section_ja"]
                ),
                "text_ja": section["text_ja"],
                "text_en": text_en,
            }
        )

    return {
        "ticker": company["ticker"],
        "company_name_en": company["company_name_en"] or company["company_name_ja"],
        "document_id": doc["docID"],
        "fiscal_year_end": doc.get("periodEnd"),
        "sections": sections,
    }


# --------------------------------------------------------------------------
# Self-test: exercises everything that works WITHOUT an EDINET API key.
# --------------------------------------------------------------------------


def _selftest():
    test_tickers = {
        "7203": "TOYOTA MOTOR",
        "6758": "SONY",
        "9984": "SoftBank",
        "8306": "Mitsubishi UFJ",
    }
    print("Running no-key self-test: ticker -> EDINET code resolution\n")
    failures = 0
    for ticker, expect_substring in test_tickers.items():
        try:
            info = resolve_ticker(ticker)
            ok = expect_substring.upper() in info["company_name_en"].upper()
            status = "OK" if ok else "MISMATCH"
            if not ok:
                failures += 1
            print(
                f"[{status}] {ticker} -> {info['edinet_code']} "
                f"{info['company_name_en']} (FY end: {info['fiscal_year_end_ja']})"
            )
        except Exception as e:
            failures += 1
            print(f"[FAIL] {ticker}: {e}")

    print()
    try:
        get_edinet_api_key()
        print("[INFO] EDINET_API_KEY is set; live API calls are possible.")
    except ConfigError as e:
        print(f"[EXPECTED] EDINET_API_KEY not set, live API calls will fail:\n{e}")

    print()
    if failures:
        print(f"Self-test finished with {failures} failure(s).")
        sys.exit(1)
    print("Self-test passed.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest()
    elif os.environ.get("MCP_TRANSPORT", "stdio").strip().lower() == "http":
        mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "8000"))
        # Behind a reverse proxy (Fly.io), the Host header is the public
        # hostname (e.g. jp-earnings.fly.dev), not localhost. Disable FastMCP's
        # default DNS-rebinding protection, which whitelists only localhost.
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
