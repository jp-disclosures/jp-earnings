# jp-earnings

An MCP (Model Context Protocol) server that gives AI agents and developers
**English-language access to Japanese company financial disclosures**, sourced
directly from EDINET — the Financial Services Agency (FSA)'s official
corporate disclosure system.

Japanese public companies file rich, standardized financial data every year,
but it's locked behind Japanese-only XBRL filings spread across a code list,
a document search API, and per-filing ZIP bundles. This server collapses
that into a couple of tool calls: give it a 4-digit Tokyo Stock Exchange
(TSE) ticker, get back English-labeled financial figures and English
translations of the narrative disclosure (business overview, risk factors,
management policy).

## What it does

- **Financial statements**: the 8 key figures from a company's latest annual
  securities report (有価証券報告書 / "yuho"), in English, each with its
  prior-year value and year-over-year % change.
- **Narrative disclosure**: business overview, risk information, and
  management policy sections, machine-translated to English.
- **Ticker resolution**: maps a plain TSE ticker like `7203` to the EDINET
  code and company name EDINET itself requires.

## Tools

### `lookup_edinet_code(ticker: str) -> dict`

Resolve a 4-digit TSE securities code (e.g. `"7203"`) to its EDINET code and
company name. Does **not** require an EDINET API key.

### `get_english_earnings(ticker: str) -> dict`

Get the latest annual securities report (有価証券報告書) key financial
figures for a Japanese ticker, with English field names. Requires
`EDINET_API_KEY`.

- `ticker`: 4-digit TSE securities code, e.g. `"7203"` for Toyota Motor.

### `get_english_narrative(ticker: str) -> dict`

Get the latest annual securities report's narrative sections — business
overview / risk information / management policy — translated to English.
Requires `EDINET_API_KEY`. If `DEEPSEEK_API_KEY` is not set, translation is
skipped and the original Japanese text is returned instead (no error).

- `ticker`: 4-digit TSE securities code, e.g. `"7203"` for Toyota Motor.

## Quickstart

Requires Python 3.11+.

```bash
pip install -r requirements.txt
```

### Get a free EDINET API key

`get_english_earnings` and `get_english_narrative` call the EDINET API v2,
which requires a free Subscription-Key. Register here:

https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1

Then set it as an environment variable:

```bash
export EDINET_API_KEY=your-subscription-key
```

`lookup_edinet_code` works without this key.

### (Optional) Enable narrative translation

`get_english_narrative` translates Japanese disclosure text via the
Deepseek API. Without a key, it still returns the sections, just untranslated
(in Japanese):

```bash
export DEEPSEEK_API_KEY=your-deepseek-key
```

### Run

```bash
python3 server.py
```

Speaks MCP over stdio — point your MCP client (Claude Desktop, the `mcp` CLI,
etc.) at `python3 /path/to/server.py`.

## The 8 financial figures

Each field is returned with `value`, `prior_year_value`, and
`yoy_change_pct`.

| Field | Japanese |
|---|---|
| `revenue` | 売上高 (or 経常収益 for banks) |
| `operating_income` | 営業利益 |
| `ordinary_income` | 経常利益 |
| `net_income` | 親会社株主に帰属する当期純利益 |
| `eps` | 1株当たり当期純利益 |
| `dividend_per_share` | 1株当たり配当額 |
| `total_assets` | 総資産 |
| `net_assets` | 純資産 |

Figures are read from the yuho's standard "5-year summary of business
results" table, so this works uniformly across JP GAAP, IFRS, and US GAAP
filers.

## Example output

```json
{
  "ticker": "7203",
  "company_name": "TOYOTA MOTOR CORPORATION",
  "company_name_ja": "トヨタ自動車株式会社",
  "edinet_code": "E02144",
  "document_type": "有価証券報告書 (annual securities report)",
  "document_id": "S100XXXX",
  "period_end": "2024-03-31",
  "filed_date": "2024-06-25 10:00",
  "figures_currency": "JPY",
  "figures": {
    "revenue": {
      "value": 45095325000000.0,
      "prior_year_value": 37154298000000.0,
      "yoy_change_pct": 21.38
    },
    "operating_income": {
      "value": 5352934000000.0,
      "prior_year_value": 2725025000000.0,
      "yoy_change_pct": 96.44
    },
    "net_income": {
      "value": 4944933000000.0,
      "prior_year_value": 2451318000000.0,
      "yoy_change_pct": 101.72
    },
    "eps": {
      "value": 379.03,
      "prior_year_value": 187.66,
      "yoy_change_pct": 101.98
    }
  }
}
```

## Known limitations

- Ticker resolution uses a free, keyless community mirror of the official
  EDINET code list (`code4fukui.github.io/EDINET`), because the official
  EDINET site gates that download behind a JavaScript/token flow. Treat it
  as a best-effort dependency.
- Document search has no "search by company" endpoint in EDINET API v2, so
  it scans daily document lists across a fiscal year's statutory filing
  window. This can take a number of API calls per lookup.
- Only 有価証券報告書 (annual report) is targeted, not 決算短信 (quarterly
  earnings flash) — figures are annual, not quarterly.
- If a filer omits the standard summary table or uses a nonstandard
  taxonomy extension, a figure comes back `null` rather than a guess.
