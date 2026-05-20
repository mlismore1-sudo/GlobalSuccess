import json
import re
import sqlite3
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

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
SIGNAL_OPTIONS = ["International Director", "International Shareholder", "Owned By A Company"]


def apply_custom_css() -> None:
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"][aria-expanded="true"] > div:first-child {
            width: 340px;
        }
        div[data-testid="metric-container"] {
            background: linear-gradient(180deg, rgba(14, 17, 23, 0.03), rgba(14, 17, 23, 0.01));
            border: 1px solid rgba(120, 120, 120, 0.18);
            padding: 14px 16px;
            border-radius: 14px;
        }
        .signal-legend {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin: 0.5rem 0 0.25rem 0;
        }
        .signal-pill {
            border: 1px solid rgba(120, 120, 120, 0.2);
            border-radius: 999px;
            padding: 6px 10px;
            font-size: 0.85rem;
            background: rgba(49, 51, 63, 0.04);
        }
        .app-note {
            padding: 0.85rem 1rem;
            border-radius: 12px;
            border: 1px solid rgba(120, 120, 120, 0.18);
            background: rgba(49, 51, 63, 0.04);
            margin-bottom: 1rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


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
    parts = [f"✓ {COUNTRY_FLAG_MAP.get(v, '🌍')} {country_label(v)}" for v in canonical_values]
    return " | ".join(parts)


def make_company_profile_url(company_number: str, company_name: str) -> str:
    safe_name = quote(company_name or "company")
    return f"https://find-and-update.company-information.service.gov.uk/company/{company_number}#{safe_name}"


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
    ensure_column(conn, "screened_companies", "profile_url", "TEXT")
    ensure_column(conn, "screened_companies", "shortlisted", "INTEGER DEFAULT 0")
    return conn


def existing_company_numbers(conn: sqlite3.Connection, incorporation_date: str) -> set:
    rows = conn.execute(
        "SELECT company_number FROM screened_companies WHERE incorporation_date = ?",
        (incorporation_date,),
    ).fetchall()
    return {r[0] for r in rows}


def set_shortlisted_state(conn: sqlite3.Connection, company_number: str, shortlisted: bool) -> None:
    conn.execute(
        "UPDATE screened_companies SET shortlisted = ? WHERE company_number = ?",
        (int(shortlisted), company_number),
    )
    conn.commit()


def upsert_company(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO screened_companies (
            company_number, company_name, sic_code, incorporation_date, company_type,
            international_director, international_director_detail,
            international_shareholder, international_shareholder_detail,
            owned_by_company, owner_company_name,
            pulled_at, raw_json, profile_url, shortlisted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            row.get("profile_url", ""),
            int(row.get("shortlisted", False)),
        ),
    )
    conn.commit()


def read_db_rows(conn: sqlite3.Connection, incorporation_date: Optional[str] = None) -> pd.DataFrame:
    if incorporation_date:
        return pd.read_sql_query(
            "SELECT * FROM screened_companies WHERE incorporation_date = ? ORDER BY pulled_at DESC",
            conn,
            params=(incorporation_date,),
        )
    return pd.read_sql_query("SELECT * FROM screened_companies ORDER BY pulled_at DESC", conn)


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
        for value in [
            officer.get("country_of_residence"),
            (officer.get("address") or {}).get("country"),
            officer.get("nationality"),
        ]:
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
        for value in [
            psc.get("country_of_residence"),
            (psc.get("address") or {}).get("country"),
            psc.get("nationality"),
        ]:
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
    company_name = item.get("company_name") or item.get("title") or ""
    international_director, director_details = collect_international_director_details(client, company_number)
    international_shareholder, shareholder_details, owned_by_company, owner_names = analyse_psc_flags(client, company_number)
    owner_display = " | ".join([f"✓ {name}" for name in owner_names]) if owner_names else ""
    return {
        "company_number": company_number,
        "company_name": company_name,
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
        "profile_url": make_company_profile_url(company_number, company_name),
        "shortlisted": False,
    }


def build_display_df(db_df: pd.DataFrame) -> pd.DataFrame:
    if db_df.empty:
        return pd.DataFrame(columns=[
            "Shortlist", "Company Name", "SIC Code", "Signals", "International Director",
            "International Shareholder", "Owned By A Company", "Profile", "Pulled At", "company_number"
        ])
    signal_labels = []
    for _, row in db_df.iterrows():
        labels = []
        if int(row.get("international_director", 0) or 0):
            labels.append("Director 🌍")
        if int(row.get("international_shareholder", 0) or 0):
            labels.append("Shareholder 🌍")
        if int(row.get("owned_by_company", 0) or 0):
            labels.append("Company owner 🏢")
        signal_labels.append(" · ".join(labels))
    return pd.DataFrame({
        "Shortlist": db_df.get("shortlisted", pd.Series(0, index=db_df.index)).fillna(0).astype(int).astype(bool),
        "Company Name": db_df["company_name"],
        "SIC Code": db_df["sic_code"],
        "Signals": signal_labels,
        "International Director": db_df.get("international_director_detail", pd.Series(dtype=str)).fillna(""),
        "International Shareholder": db_df.get("international_shareholder_detail", pd.Series(dtype=str)).fillna(""),
        "Owned By A Company": db_df.get("owner_company_name", pd.Series(dtype=str)).fillna(""),
        "Profile": db_df.get("profile_url", pd.Series(dtype=str)).fillna(""),
        "Pulled At": db_df["pulled_at"],
        "company_number": db_df["company_number"],
    })


def apply_filters(df: pd.DataFrame, only_flagged: bool, selected_signals: List[str], sic_search: str, company_name_search: str, shortlisted_only: bool) -> pd.DataFrame:
    filtered = df.copy()
    if shortlisted_only and "Shortlist" in filtered.columns:
        filtered = filtered[filtered["Shortlist"] == True].copy()
    if only_flagged:
        mask = pd.Series(False, index=filtered.index)
        if "International Director" in selected_signals:
            mask |= filtered["International Director"].astype(str).str.startswith("✓", na=False)
        if "International Shareholder" in selected_signals:
            mask |= filtered["International Shareholder"].astype(str).str.startswith("✓", na=False)
        if "Owned By A Company" in selected_signals:
            mask |= filtered["Owned By A Company"].astype(str).str.startswith("✓", na=False)
        filtered = filtered[mask].copy()
    if sic_search.strip():
        filtered = filtered[filtered["SIC Code"].astype(str).str.contains(re.escape(sic_search.strip()), case=False, na=False)].copy()
    if company_name_search.strip():
        filtered = filtered[filtered["Company Name"].astype(str).str.contains(re.escape(company_name_search.strip()), case=False, na=False)].copy()
    return filtered


def render_kpis(display_df: pd.DataFrame) -> None:
    total = len(display_df)
    director = int(display_df["International Director"].astype(str).str.startswith("✓", na=False).sum()) if not display_df.empty else 0
    shareholder = int(display_df["International Shareholder"].astype(str).str.startswith("✓", na=False).sum()) if not display_df.empty else 0
    owner = int(display_df["Owned By A Company"].astype(str).str.startswith("✓", na=False).sum()) if not display_df.empty else 0
    flagged = int(((display_df["International Director"].astype(str).str.startswith("✓", na=False)) |
                   (display_df["International Shareholder"].astype(str).str.startswith("✓", na=False)) |
                   (display_df["Owned By A Company"].astype(str).str.startswith("✓", na=False))).sum()) if not display_df.empty else 0
    shortlisted = int(display_df["Shortlist"].sum()) if not display_df.empty else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Results", f"{total:,}")
    c2.metric("Flagged Rows", f"{flagged:,}")
    c3.metric("Intl Directors", f"{director:,}")
    c4.metric("Intl Shareholders", f"{shareholder:,}")
    c5.metric("Shortlisted", f"{shortlisted:,}")


def render_sidebar(default_date: date) -> Tuple[date, bool, List[str], str, str, bool, bool]:
    with st.sidebar:
        st.header("Screening controls")
        target_date = st.date_input("Incorporation date", value=default_date, format="YYYY-MM-DD")
        run = st.button("Pull new companies", type="primary", use_container_width=True)
        st.divider()
        st.subheader("Result filters")
        only_flagged = st.checkbox("Show only flagged rows", value=False)
        selected_signals = st.multiselect(
            "Signals",
            options=SIGNAL_OPTIONS,
            default=SIGNAL_OPTIONS,
        )
        sic_search = st.text_input("Filter by SIC code", placeholder="e.g. 62012")
        company_name_search = st.text_input("Filter by company name", placeholder="e.g. Labs")
        shortlisted_only = st.checkbox("Show shortlisted only", value=False)
        st.divider()
        st.caption("The sidebar keeps controls separate from the results table for faster screening.")
    return target_date, run, selected_signals, sic_search, company_name_search, only_flagged, shortlisted_only


def main() -> None:
    apply_custom_css()
    st.title("Companies House New Incorporations Screener")
    st.caption("Pull newly incorporated active companies, screen target SIC codes, and enrich results with officer and PSC checks.")

    st.markdown(
        """
        <div class="app-note">
        Designed for rapid lead triage: run the pull, scan KPIs, filter the signals, shortlist candidates, and click through to Companies House profiles.
        </div>
        """,
        unsafe_allow_html=True,
    )

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

    target_date, run, selected_signals, sic_search, company_name_search, only_flagged, shortlisted_only = render_sidebar(date.today())
    date_str = target_date.strftime("%Y-%m-%d")

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

    db_df = read_db_rows(conn, date_str)
    display_df = build_display_df(db_df)
    render_kpis(display_df)

    st.markdown(
        """
        <div class="signal-legend">
            <div class="signal-pill">Director 🌍 = international director match</div>
            <div class="signal-pill">Shareholder 🌍 = international PSC match</div>
            <div class="signal-pill">Company owner 🏢 = corporate PSC match</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    filtered_df = apply_filters(
        display_df,
        only_flagged=only_flagged,
        selected_signals=selected_signals,
        sic_search=sic_search,
        company_name_search=company_name_search,
        shortlisted_only=shortlisted_only,
    )

    tab_results, tab_shortlist, tab_settings = st.tabs(["Results", "Shortlist", "Settings"])

    with tab_results:
        st.subheader("Results")
        st.caption(f"Loaded {len(api_keys)} API key(s) for {date_str}. {len(filtered_df):,} rows currently visible after filters.")

        editor_df = filtered_df[[
            "Shortlist", "Company Name", "SIC Code", "Signals", "International Director",
            "International Shareholder", "Owned By A Company", "Profile", "Pulled At", "company_number"
        ]].copy()

        edited_df = st.data_editor(
            editor_df,
            use_container_width=True,
            hide_index=True,
            disabled=[
                "Company Name", "SIC Code", "Signals", "International Director",
                "International Shareholder", "Owned By A Company", "Profile", "Pulled At", "company_number"
            ],
            column_config={
                "Shortlist": st.column_config.CheckboxColumn("Shortlist", help="Tick to mark this company for follow-up."),
                "Company Name": st.column_config.TextColumn("Company Name", width="large"),
                "SIC Code": st.column_config.TextColumn("SIC Code", width="small"),
                "Signals": st.column_config.TextColumn("Signals", width="medium"),
                "International Director": st.column_config.TextColumn("International Director", width="large"),
                "International Shareholder": st.column_config.TextColumn("International Shareholder", width="large"),
                "Owned By A Company": st.column_config.TextColumn("Owned By A Company", width="large"),
                "Profile": st.column_config.LinkColumn("Profile", display_text="Open record", width="small"),
                "Pulled At": st.column_config.TextColumn("Pulled At", width="medium"),
                "company_number": None,
            },
            key=f"results_editor_{date_str}",
        )

        if not edited_df.empty:
            changes = edited_df[["company_number", "Shortlist"]].merge(
                display_df[["company_number", "Shortlist"]],
                on="company_number",
                suffixes=("_new", "_old"),
                how="left",
            )
            changed_rows = changes[changes["Shortlist_new"] != changes["Shortlist_old"]]
            for _, row in changed_rows.iterrows():
                set_shortlisted_state(conn, row["company_number"], bool(row["Shortlist_new"]))
            if not changed_rows.empty:
                st.success(f"Updated shortlist state for {len(changed_rows)} compan{'y' if len(changed_rows) == 1 else 'ies'}.")
                st.rerun()

        csv = filtered_df.drop(columns=["company_number"], errors="ignore").to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download filtered CSV",
            data=csv,
            file_name=f"companies_house_screening_{date_str}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with tab_shortlist:
        st.subheader("Shortlist")
        shortlist_df = display_df[display_df["Shortlist"] == True].copy()
        if shortlist_df.empty:
            st.info("No shortlisted companies yet. Tick the shortlist checkbox in the Results tab to build a follow-up queue.")
        else:
            st.dataframe(
                shortlist_df.drop(columns=["company_number"], errors="ignore"),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Profile": st.column_config.LinkColumn("Profile", display_text="Open record"),
                },
            )
            shortlist_csv = shortlist_df.drop(columns=["company_number"], errors="ignore").to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download shortlist CSV",
                data=shortlist_csv,
                file_name=f"companies_house_shortlist_{date_str}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    with tab_settings:
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
- UI enhancements: sidebar filters, KPI cards, workflow tabs, clickable profile links, shortlist workflow
            """
        )
        st.write("Selected signals for current filter:", ", ".join(selected_signals) if selected_signals else "None")


if __name__ == "__main__":
    main()
