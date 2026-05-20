import json
import re
import sqlite3
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Companies House New Incorporations Screener", layout="wide")

BASE_URL = "https://api.company-information.service.gov.uk"
DB_PATH = "companies_house_screening.db"
SEARCH_PAGE_SIZE = 5000
OFFICERS_PAGE_SIZE = 100
PSC_PAGE_SIZE = 100
ALLOWED_SIC_CODES = [
    "62012", "62020", "63120", "47910", "46190", "46499", "70229", "73110", "74909", "68209",
    "64209", "68100", "32990", "10890", "86900", "93130", "96040", "82990", "72110", "56101",
]
ALLOWED_COMPANY_TYPES = [
    "ltd",
    "llp",
    "private-limited-guarant-nsc",
    "private-limited-shares-section-30-exemption",
]
COUNTRY_TERMS = {
    "usa", "united states", "united states of america", "france", "germany", "belgium", "norway",
    "sweden", "finland", "denmark", "austria", "poland", "spain", "portugal", "greece", "italy",
    "hungary", "croatia", "ireland", "china", "netherlands", "india", "hong kong", "singapore",
}
NATIONALITY_TERMS = {
    "american", "us", "united states", "united states of america", "french", "german", "belgian",
    "norwegian", "swedish", "finnish", "danish", "austrian", "polish", "spanish", "portuguese",
    "greek", "italian", "hungarian", "croatian", "irish", "chinese", "indian", "hong kong",
    "hongkong", "singaporean", "dutch", "netherlands",
}
COMPANY_OWNER_KINDS = {
    "corporate-entity-person-with-significant-control",
    "legal-person-person-with-significant-control",
    "super-secure-person-with-significant-control",
}
COUNTRY_FLAG_MAP = {
    "united states": "🇺🇸",
    "france": "🇫🇷",
    "germany": "🇩🇪",
    "belgium": "🇧🇪",
    "norway": "🇳🇴",
    "sweden": "🇸🇪",
    "finland": "🇫🇮",
    "denmark": "🇩🇰",
    "austria": "🇦🇹",
    "poland": "🇵🇱",
    "spain": "🇪🇸",
    "portugal": "🇵🇹",
    "greece": "🇬🇷",
    "italy": "🇮🇹",
    "hungary": "🇭🇺",
    "croatia": "🇭🇷",
    "ireland": "🇮🇪",
    "china": "🇨🇳",
    "netherlands": "🇳🇱",
    "india": "🇮🇳",
    "hong kong": "🇭🇰",
    "singapore": "🇸🇬",
}
NATIONALITY_TO_COUNTRY = {
    "american": "united states",
    "us": "united states",
    "united states": "united states",
    "french": "france",
    "german": "germany",
    "belgian": "belgium",
    "norwegian": "norway",
    "swedish": "sweden",
    "finnish": "finland",
    "danish": "denmark",
    "austrian": "austria",
    "polish": "poland",
    "spanish": "spain",
    "portuguese": "portugal",
    "greek": "greece",
    "italian": "italy",
    "hungarian": "hungary",
    "croatian": "croatia",
    "irish": "ireland",
    "chinese": "china",
    "indian": "india",
    "hong kong": "hong kong",
    "hongkong": "hong kong",
    "singaporean": "singapore",
    "dutch": "netherlands",
    "netherlands": "netherlands",
}


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = text.replace("-", " ")
    text = re.sub(r"[^a-z0-9\\s]", "", text)
    text = re.sub(r"\\s+", " ", text).strip()
    aliases = {
        "usa": "united states",
        "u s a": "united states",
        "u s": "us",
        "united states of america": "united states",
        "america": "american",
        "hong kong": "hong kong",
        "hongkong": "hong kong",
        "the netherlands": "netherlands",
    }
    return aliases.get(text, text)


NORMALIZED_COUNTRY_TERMS = {normalize_text(x) for x in COUNTRY_TERMS}
NORMALIZED_NATIONALITY_TERMS = {normalize_text(x) for x in NATIONALITY_TERMS}
NORMALIZED_ALLOWED_COMPANY_TYPES = {normalize_text(x) for x in ALLOWED_COMPANY_TYPES}


def matches_term(value: Any, lookup: set) -> bool:
    norm = normalize_text(value)
    return bool(norm) and norm in lookup


def canonical_country_from_value(value: Any) -> str:
    norm = normalize_text(value)
    if not norm:
        return ""
    if norm in NORMALIZED_COUNTRY_TERMS:
        return norm
    if norm in NATIONALITY_TO_COUNTRY:
        return NATIONALITY_TO_COUNTRY[norm]
    return ""


def dedupe_preserve_order(values: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        norm = normalize_text(value)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(value)
    return out


def country_label(value: str) -> str:
    if value == "united states":
        return "USA"
    if value == "hong kong":
        return "Hong Kong"
    return value.title()


def format_flagged_countries(values: List[str]) -> str:
    canonical_values = dedupe_preserve_order([canonical_country_from_value(v) for v in values if canonical_country_from_value(v)])
    if not canonical_values:
        return ""
    parts = [f"{COUNTRY_FLAG_MAP.get(v, '🌍')} {country_label(v)}" for v in canonical_values]
    return "✓ " + ", ".join(parts)


class CHClient:
    def __init__(self, api_keys: List[str]):
        self.api_keys = [k.strip() for k in api_keys if str(k).strip()]
        if not self.api_keys:
            raise ValueError("No Companies House API keys supplied.")
        self.idx = 0
        self.session = requests.Session()

    def _auth(self) -> Tuple[str, str]:
        return (self.api_keys[self.idx % len(self.api_keys)], "")

    def _rotate(self) -> None:
        self.idx = (self.idx + 1) % len(self.api_keys)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        last_error = None
        for _ in range(max(len(self.api_keys) * 3, 3)):
            try:
                response = self.session.get(
                    f"{BASE_URL}{path}",
                    params=params,
                    auth=self._auth(),
                    timeout=30,
                    headers={"Accept": "application/json"},
                )
                if response.status_code == 404:
                    return {}
                if response.status_code in (401, 403, 429):
                    last_error = f"HTTP {response.status_code}"
                    self._rotate()
                    time.sleep(0.5)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = str(exc)
                self._rotate()
                time.sleep(0.5)
        raise RuntimeError(f"Companies House API request failed after retries: {last_error}")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screened_companies (
            company_number TEXT PRIMARY KEY,
            company_name TEXT,
            sic_code TEXT,
            incorporation_date TEXT,
            company_type TEXT,
            international_director INTEGER,
            international_shareholder INTEGER,
            owned_by_company INTEGER,
            pulled_at TEXT,
            raw_json TEXT
        )
        """
    )
    conn.commit()
    ensure_column(conn, "screened_companies", "international_director_detail", "TEXT")
    ensure_column(conn, "screened_companies", "international_shareholder_detail", "TEXT")
    ensure_column(conn, "screened_companies", "owner_company_name", "TEXT")
    return conn


def existing_company_numbers(conn: sqlite3.Connection, incorporation_date: str) -> set:
    rows = conn.execute(
        "SELECT company_number FROM screened_companies WHERE incorporation_date = ?",
        (incorporation_date,),
    ).fetchall()
    return {r[0] for r in rows}


def upsert_company(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO screened_companies (
            company_number, company_name, sic_code, incorporation_date, company_type,
            international_director, international_director_detail,
            international_shareholder, international_shareholder_detail,
            owned_by_company, owner_company_name,
            pulled_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["company_number"],
            row["company_name"],
            row["sic_code"],
            row["incorporation_date"],
            row["company_type"],
            int(row["international_director"]),
            row.get("international_director_detail", ""),
            int(row["international_shareholder"]),
            row.get("international_shareholder_detail", ""),
            int(row["owned_by_company"]),
            row.get("owner_company_name", ""),
            row["pulled_at"],
            json.dumps(row.get("raw_json", {})),
        ),
    )
    conn.commit()


def read_results(conn: sqlite3.Connection, incorporation_date: Optional[str] = None) -> pd.DataFrame:
    if incorporation_date:
        df = pd.read_sql_query(
            "SELECT * FROM screened_companies WHERE incorporation_date = ? ORDER BY pulled_at DESC",
            conn,
            params=(incorporation_date,),
        )
    else:
        df = pd.read_sql_query("SELECT * FROM screened_companies ORDER BY pulled_at DESC", conn)
    if df.empty:
        return pd.DataFrame(columns=[
            "Company Name", "SIC Code", "International Director", "International Shareholder", "Owned By A Company", "Pulled At"
        ])
    return pd.DataFrame({
        "Company Name": df["company_name"],
        "SIC Code": df["sic_code"],
        "International Director": df.get("international_director_detail", pd.Series(dtype=str)).fillna(""),
        "International Shareholder": df.get("international_shareholder_detail", pd.Series(dtype=str)).fillna(""),
        "Owned By A Company": df.get("owner_company_name", pd.Series(dtype=str)).fillna(""),
        "Pulled At": df["pulled_at"],
    })


def validate_api_keys() -> List[str]:
    if "COMPANIES_HOUSE_API_KEYS" not in st.secrets:
        raise ValueError("Missing COMPANIES_HOUSE_API_KEYS in .streamlit/secrets.toml")
    keys = [str(k).strip() for k in list(st.secrets["COMPANIES_HOUSE_API_KEYS"]) if str(k).strip()]
    if not keys:
        raise ValueError("COMPANIES_HOUSE_API_KEYS is empty")
    return keys


def paged_get_items(client: CHClient, path: str, page_size: int, extra_params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    start_index = 0
    while True:
        params = {"start_index": start_index}
        if extra_params:
            params.update(extra_params)
        if path == "/advanced-search/companies":
            params["size"] = page_size
        else:
            params["items_per_page"] = page_size
        payload = client.get(path, params=params)
        batch = payload.get("items", []) or []
        items.extend(batch)
        total = payload.get("total_results")
        if total is None:
            total = payload.get("total_count")
        total = int(total or len(items))
        start_index += page_size
        if not batch or start_index >= total:
            break
    return items


def is_allowed_company_type(value: Any) -> bool:
    return normalize_text(value) in NORMALIZED_ALLOWED_COMPANY_TYPES


def search_new_companies(client: CHClient, target_date: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    params = {
        "incorporated_from": target_date,
        "incorporated_to": target_date,
        "company_status": "active",
        "company_type": ",".join(ALLOWED_COMPANY_TYPES),
        "sic_codes": ",".join(ALLOWED_SIC_CODES),
    }
    items = paged_get_items(client, "/advanced-search/companies", SEARCH_PAGE_SIZE, params)
    filtered: List[Dict[str, Any]] = []
    for item in items:
        item_sics = [str(x) for x in (item.get("sic_codes") or [])]
        if not any(code in ALLOWED_SIC_CODES for code in item_sics):
            continue
        if item.get("company_status", "").lower() != "active":
            continue
        if not is_allowed_company_type(item.get("company_type", "")):
            continue
        filtered.append(item)
    deduped = {}
    for item in filtered:
        number = item.get("company_number")
        if number:
            deduped[number] = item
    diagnostics = {
        "raw_results": len(items),
        "filtered_results": len(filtered),
        "deduped_results": len(deduped),
        "company_types_sent": ", ".join(ALLOWED_COMPANY_TYPES),
        "sic_count": len(ALLOWED_SIC_CODES),
    }
    return list(deduped.values()), diagnostics


def get_all_officers(client: CHClient, company_number: str) -> List[Dict[str, Any]]:
    return paged_get_items(client, f"/company/{company_number}/officers", OFFICERS_PAGE_SIZE)


def get_all_pscs(client: CHClient, company_number: str) -> List[Dict[str, Any]]:
    return paged_get_items(client, f"/company/{company_number}/persons-with-significant-control", PSC_PAGE_SIZE)


def collect_international_director_details(client: CHClient, company_number: str) -> Tuple[bool, List[str]]:
    officers = get_all_officers(client, company_number)
    matches: List[str] = []
    for officer in officers:
        role = normalize_text(officer.get("officer_role"))
        if "director" not in role and role != "designated member":
            continue
        candidate_values = [
            officer.get("country_of_residence"),
            (officer.get("address") or {}).get("country"),
            officer.get("nationality"),
        ]
        for value in candidate_values:
            if canonical_country_from_value(value):
                matches.append(str(value))
    deduped = dedupe_preserve_order(matches)
    return bool(deduped), deduped


def analyse_psc_flags(client: CHClient, company_number: str) -> Tuple[bool, List[str], bool, List[str]]:
    pscs = get_all_pscs(client, company_number)
    shareholder_matches: List[str] = []
    owner_names: List[str] = []
    for psc in pscs:
        kind = str(psc.get("kind", ""))
        candidate_values = [
            psc.get("country_of_residence"),
            (psc.get("address") or {}).get("country"),
            psc.get("nationality"),
        ]
        for value in candidate_values:
            if canonical_country_from_value(value):
                shareholder_matches.append(str(value))
        if kind in COMPANY_OWNER_KINDS or "corporate" in kind or "legal-person" in kind:
            owner_name = str(psc.get("name") or "").strip()
            if owner_name:
                owner_names.append(owner_name)
    deduped_shareholders = dedupe_preserve_order(shareholder_matches)
    deduped_owners = dedupe_preserve_order(owner_names)
    return bool(deduped_shareholders), deduped_shareholders, bool(deduped_owners), deduped_owners


def parse_matching_sic(item: Dict[str, Any]) -> str:
    item_sics = [str(code) for code in (item.get("sic_codes") or [])]
    matched = [code for code in item_sics if code in ALLOWED_SIC_CODES]
    return ", ".join(matched or item_sics[:1])


def process_company(client: CHClient, item: Dict[str, Any], target_date: str) -> Dict[str, Any]:
    company_number = item.get("company_number", "")
    international_director, director_details = collect_international_director_details(client, company_number)
    international_shareholder, shareholder_details, owned_by_company, owner_names = analyse_psc_flags(client, company_number)
    owner_display = f"✓ {', '.join(owner_names)}" if owner_names else ""
    return {
        "company_number": company_number,
        "company_name": item.get("company_name") or item.get("title") or "",
        "sic_code": parse_matching_sic(item),
        "incorporation_date": target_date,
        "company_type": item.get("company_type", ""),
        "international_director": international_director,
        "international_director_detail": format_flagged_countries(director_details),
        "international_shareholder": international_shareholder,
        "international_shareholder_detail": format_flagged_countries(shareholder_details),
        "owned_by_company": owned_by_company,
        "owner_company_name": owner_display,
        "pulled_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "raw_json": item,
    }


def apply_result_filters(df: pd.DataFrame, only_flagged: bool, selected_flags: List[str]) -> pd.DataFrame:
    if df.empty:
        return df
    filtered = df.copy()
    if only_flagged:
        mask = pd.Series(False, index=filtered.index)
        flag_map = {
            "International Director": filtered["International Director"].astype(str).str.startswith("✓", na=False),
            "International Shareholder": filtered["International Shareholder"].astype(str).str.startswith("✓", na=False),
            "Owned By A Company": filtered["Owned By A Company"].astype(str).str.startswith("✓", na=False),
        }
        if selected_flags:
            for label in selected_flags:
                mask |= flag_map[label]
        else:
            for series in flag_map.values():
                mask |= series
        filtered = filtered[mask].copy()
    return filtered


def main() -> None:
    st.title("Companies House New Incorporations Screener")
    st.caption("Pulls newly incorporated active companies, screens target SIC codes, then enriches results with officer and PSC checks.")

    with st.expander("Secrets format", expanded=False):
        st.code(
            'COMPANIES_HOUSE_API_KEYS = [\\n  "key-1",\\n  "key-2",\\n  "key-3"\\n]',
            language="toml",
        )

    try:
        api_keys = validate_api_keys()
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    conn = init_db()
    client = CHClient(api_keys)

    left, right = st.columns([1, 1])
    with left:
        target_date = st.date_input("Incorporation date", value=date.today(), format="YYYY-MM-DD")
    with right:
        run = st.button("Pull new companies", type="primary", use_container_width=True)

    date_str = target_date.strftime("%Y-%m-%d")
    st.write(f"Loaded {len(api_keys)} API key(s), {len(ALLOWED_SIC_CODES)} SIC codes, {len(ALLOWED_COMPANY_TYPES)} company type values.")

    if run:
        failures: List[str] = []
        with st.status("Running Companies House screening...", expanded=True) as status:
            st.write("Querying advanced search with all SIC codes and company types in one request pattern.")
            companies, diagnostics = search_new_companies(client, date_str)
            already_seen = existing_company_numbers(conn, date_str)
            new_companies = [c for c in companies if c.get("company_number") not in already_seen]
            st.write(f"Raw search results: {diagnostics['raw_results']}")
            st.write(f"Filtered results retained: {diagnostics['filtered_results']}")
            st.write(f"Deduped company numbers: {diagnostics['deduped_results']}")
            st.write(f"Already screened for {date_str}: {len(already_seen)}")
            st.write(f"New companies to enrich: {len(new_companies)}")

            progress = st.progress(0)
            total = max(len(new_companies), 1)
            for idx, item in enumerate(new_companies, start=1):
                company_number = item.get("company_number", "unknown")
                try:
                    row = process_company(client, item, date_str)
                    upsert_company(conn, row)
                except Exception as exc:
                    failures.append(f"{company_number}: {exc}")
                progress.progress(min(idx / total, 1.0))

            if failures:
                st.warning(f"Failed enrichments: {len(failures)}")
                st.code("\\n".join(failures[:50]))
                status.update(label="Completed with some errors", state="error")
            else:
                status.update(label="Refresh complete", state="complete")

    st.subheader("Results filters")
    f1, f2 = st.columns([1, 2])
    with f1:
        only_flagged = st.checkbox("Show only flagged rows", value=False, help="Shows only rows where at least one of the selected signals is present.")
    with f2:
        selected_flags = st.multiselect(
            "Signals to include when filtering",
            options=["International Director", "International Shareholder", "Owned By A Company"],
            default=["International Director", "International Shareholder", "Owned By A Company"],
            help="Used only when 'Show only flagged rows' is enabled.",
        )

    result_df = read_results(conn, date_str)
    result_df = apply_result_filters(result_df, only_flagged=only_flagged, selected_flags=selected_flags)

    st.subheader("Results")
    st.dataframe(
        result_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Company Name": st.column_config.TextColumn("Company Name", width="large"),
            "SIC Code": st.column_config.TextColumn("SIC Code", width="small"),
            "International Director": st.column_config.TextColumn("International Director", width="medium"),
            "International Shareholder": st.column_config.TextColumn("International Shareholder", width="medium"),
            "Owned By A Company": st.column_config.TextColumn("Owned By A Company", width="large"),
            "Pulled At": st.column_config.TextColumn("Pulled At", width="medium"),
        },
    )

    csv = result_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv,
        file_name=f"companies_house_screening_{date_str}.csv",
        mime="text/csv",
        use_container_width=True,
    )

    st.subheader("Current search settings")
    st.markdown(
        f"""
- Company status: Active
- Company types sent to API: `{', '.join(ALLOWED_COMPANY_TYPES)}`
- SIC codes sent to API: {len(ALLOWED_SIC_CODES)} values
- Advanced search page size: {SEARCH_PAGE_SIZE}
- Officers page size: {OFFICERS_PAGE_SIZE}
- PSC page size: {PSC_PAGE_SIZE}
- Dedupe rule: company numbers already screened for the selected incorporation date are skipped
- Filter mode: optional UI filter to show only flagged rows by selected signal types
        """
    )


if __name__ == "__main__":
    main()
