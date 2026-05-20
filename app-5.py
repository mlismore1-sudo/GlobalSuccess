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


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = text.replace("-", " ")
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    aliases = {
        "usa": "united states",
        "u s a": "united states",
        "u s": "us",
        "united states of america": "united states",
        "america": "american",
        "hongkong": "hong kong",
        "the netherlands": "netherlands",
    }
    return aliases.get(text, text)


NORMALIZED_COUNTRY_TERMS = {normalize_text(x) for x in COUNTRY_TERMS}
NORMALIZED_NATIONALITY_TERMS = {normalize_text(x) for x in NATIONALITY_TERMS}
NORMALIZED_ALLOWED_TYPES = {normalize_text(x) for x in ALLOWED_COMPANY_TYPES}


class CHClient:
    def __init__(self, api_keys: List[str]):
        self.api_keys = [k.strip() for k in api_keys if str(k).strip()]
        if not self.api_keys:
            raise ValueError("No Companies House API keys supplied.")
        self.idx = 0
        self.session = requests.Session()

    def _auth(self) -> Tuple[str, str]:
        return self.api_keys[self.idx % len(self.api_keys)], ""

    def _rotate(self) -> None:
        self.idx = (self.idx + 1) % len(self.api_keys)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        last_error = None
        for _ in range(max(3, len(self.api_keys) * 3)):
            try:
                response = self.session.get(
                    f"{BASE_URL}{path}",
                    params=params,
                    auth=self._auth(),
                    headers={"Accept": "application/json"},
                    timeout=30,
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
    return conn


def existing_company_numbers(conn: sqlite3.Connection, incorporation_date: str) -> set:
    rows = conn.execute(
        "SELECT company_number FROM screened_companies WHERE incorporation_date = ?",
        (incorporation_date,),
    ).fetchall()
    return {row[0] for row in rows}


def upsert_company(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO screened_companies (
            company_number, company_name, sic_code, incorporation_date, company_type,
            international_director, international_shareholder, owned_by_company, pulled_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["company_number"],
            row["company_name"],
            row["sic_code"],
            row["incorporation_date"],
            row["company_type"],
            int(row["international_director"]),
            int(row["international_shareholder"]),
            int(row["owned_by_company"]),
            row["pulled_at"],
            json.dumps(row.get("raw_json", {})),
        ),
    )
    conn.commit()


def read_results(conn: sqlite3.Connection, incorporation_date: Optional[str] = None, only_ticked: bool = True) -> pd.DataFrame:
    if incorporation_date:
        df = pd.read_sql_query(
            "SELECT * FROM screened_companies WHERE incorporation_date = ? ORDER BY pulled_at DESC",
            conn,
            params=(incorporation_date,),
        )
    else:
        df = pd.read_sql_query("SELECT * FROM screened_companies ORDER BY pulled_at DESC", conn)

    columns = [
        "Company Name", "SIC Code", "International Director?", "International Shareholder?", "Owned By A Company?", "Pulled At"
    ]

    if df.empty:
        return pd.DataFrame(columns=columns)

    if only_ticked:
        df = df[
            (df["international_director"].astype(int) == 1)
            | (df["international_shareholder"].astype(int) == 1)
            | (df["owned_by_company"].astype(int) == 1)
        ]

    if df.empty:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(
        {
            "Company Name": df["company_name"],
            "SIC Code": df["sic_code"],
            "International Director?": df["international_director"].map(lambda x: "✓" if int(x) else ""),
            "International Shareholder?": df["international_shareholder"].map(lambda x: "✓" if int(x) else ""),
            "Owned By A Company?": df["owned_by_company"].map(lambda x: "✓" if int(x) else ""),
            "Pulled At": df["pulled_at"],
        }
    )


def validate_api_keys() -> List[str]:
    if "COMPANIES_HOUSE_API_KEYS" not in st.secrets:
        raise ValueError("Missing COMPANIES_HOUSE_API_KEYS in .streamlit/secrets.toml")
    keys = [str(k).strip() for k in list(st.secrets["COMPANIES_HOUSE_API_KEYS"]) if str(k).strip()]
    if not keys:
        raise ValueError("COMPANIES_HOUSE_API_KEYS is empty")
    return keys


def matches_term(value: Any, lookup: set) -> bool:
    normalized = normalize_text(value)
    return bool(normalized) and normalized in lookup


def paged_get_items(client: CHClient, path: str, page_size: int, extra_params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    start_index = 0
    while True:
        params: Dict[str, Any] = {"start_index": start_index}
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
    return normalize_text(value) in NORMALIZED_ALLOWED_TYPES


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
        item_sics = [str(code) for code in (item.get("sic_codes") or [])]
        if not any(code in ALLOWED_SIC_CODES for code in item_sics):
            continue
        if str(item.get("company_status", "")).lower() != "active":
            continue
        if not is_allowed_company_type(item.get("company_type", "")):
            continue
        filtered.append(item)

    deduped: Dict[str, Dict[str, Any]] = {}
    for item in filtered:
        company_number = item.get("company_number")
        if company_number:
            deduped[company_number] = item

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


def has_international_director(client: CHClient, company_number: str) -> bool:
    officers = get_all_officers(client, company_number)
    for officer in officers:
        role = normalize_text(officer.get("officer_role"))
        if "director" not in role and role != "designated member":
            continue
        if (
            matches_term(officer.get("nationality"), NORMALIZED_NATIONALITY_TERMS)
            or matches_term(officer.get("country_of_residence"), NORMALIZED_COUNTRY_TERMS)
            or matches_term((officer.get("address") or {}).get("country"), NORMALIZED_COUNTRY_TERMS)
        ):
            return True
    return False


def analyse_psc_flags(client: CHClient, company_number: str) -> Tuple[bool, bool]:
    pscs = get_all_pscs(client, company_number)
    international_shareholder = False
    owned_by_company = False
    for psc in pscs:
        kind = str(psc.get("kind", ""))
        if (
            matches_term(psc.get("nationality"), NORMALIZED_NATIONALITY_TERMS)
            or matches_term(psc.get("country_of_residence"), NORMALIZED_COUNTRY_TERMS)
            or matches_term((psc.get("address") or {}).get("country"), NORMALIZED_COUNTRY_TERMS)
        ):
            international_shareholder = True
        if kind in COMPANY_OWNER_KINDS or "corporate" in kind or "legal-person" in kind:
            owned_by_company = True
    return international_shareholder, owned_by_company


def parse_matching_sic(item: Dict[str, Any]) -> str:
    item_sics = [str(code) for code in (item.get("sic_codes") or [])]
    matched = [code for code in item_sics if code in ALLOWED_SIC_CODES]
    return ", ".join(matched or item_sics[:1])


def process_company(client: CHClient, item: Dict[str, Any], target_date: str) -> Dict[str, Any]:
    company_number = item.get("company_number", "")
    international_director = has_international_director(client, company_number)
    international_shareholder, owned_by_company = analyse_psc_flags(client, company_number)
    return {
        "company_number": company_number,
        "company_name": item.get("company_name") or item.get("title") or "",
        "sic_code": parse_matching_sic(item),
        "incorporation_date": target_date,
        "company_type": item.get("company_type", ""),
        "international_director": international_director,
        "international_shareholder": international_shareholder,
        "owned_by_company": owned_by_company,
        "pulled_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "raw_json": item,
    }


def main() -> None:
    st.title("Companies House New Incorporations Screener")
    st.caption("Pulls newly incorporated active companies, screens target SIC codes, then enriches results with officer and PSC checks.")

    with st.expander("Secrets format", expanded=False):
        st.code(
            'COMPANIES_HOUSE_API_KEYS = [\n  "key-1",\n  "key-2",\n  "key-3"\n]',
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
    st.write(
        f"Loaded {len(api_keys)} API key(s), {len(ALLOWED_SIC_CODES)} SIC codes, {len(ALLOWED_COMPANY_TYPES)} company type values."
    )

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
                st.code("\n".join(failures[:50]))
                status.update(label="Completed with some errors", state="error")
            else:
                status.update(label="Refresh complete", state="complete")

    result_df = read_results(conn, date_str, only_ticked=True)
    st.subheader("Results")
    st.caption("Only companies with at least one tick are shown.")
    st.dataframe(result_df, use_container_width=True, hide_index=True)

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
- Display rule: only rows with at least one tick are shown in the results table and CSV
        """
    )


if __name__ == "__main__":
    main()
    "greek", "italian", "hungarian", "croatian", "irish", "chinese", "indian", "hong kong",
    "hongkong", "singaporean", "dutch", "netherlands",
}
COMPANY_OWNER_KINDS = {
    "corporate-entity-person-with-significant-control",
    "legal-person-person-with-significant-control",
    "super-secure-person-with-significant-control",
}


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = text.replace("-", " ")
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
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


def matches_term(value: Any, lookup: set) -> bool:
    norm = normalize_text(value)
    return bool(norm) and norm in lookup


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
            international_director, international_shareholder, owned_by_company, pulled_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["company_number"],
            row["company_name"],
            row["sic_code"],
            row["incorporation_date"],
            row["company_type"],
            int(row["international_director"]),
            int(row["international_shareholder"]),
            int(row["owned_by_company"]),
            row["pulled_at"],
            json.dumps(row.get("raw_json", {})),
        ),
    )
    conn.commit()


def read_results(conn: sqlite3.Connection, incorporation_date: Optional[str] = None, only_ticked: bool = True) -> pd.DataFrame:
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
            "Company Name", "SIC Code", "International Director?", "International Shareholder?", "Owned By A Company?", "Pulled At"
        ])
    if only_ticked:
        df = df[
            (df["international_director"].astype(int) == 1)
            | (df["international_shareholder"].astype(int) == 1)
            | (df["owned_by_company"].astype(int) == 1)
        ]
    if df.empty:
        return pd.DataFrame(columns=[
            "Company Name", "SIC Code", "International Director?", "International Shareholder?", "Owned By A Company?", "Pulled At"
        ])
    return pd.DataFrame({
        "Company Name": df["company_name"],
        "SIC Code": df["sic_code"],
        "International Director?": df["international_director"].map(lambda x: "✓" if int(x) else ""),
        "International Shareholder?": df["international_shareholder"].map(lambda x: "✓" if int(x) else ""),
        "Owned By A Company?": df["owned_by_company"].map(lambda x: "✓" if int(x) else ""),
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
    return normalize_text(value) in {normalize_text(x) for x in ALLOWED_COMPANY_TYPES}


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


def has_international_director(client: CHClient, company_number: str) -> bool:
    officers = get_all_officers(client, company_number)
    for officer in officers:
        role = normalize_text(officer.get("officer_role"))
        if "director" not in role and role != "designated member":
            continue
        if (
            matches_term(officer.get("nationality"), NORMALIZED_NATIONALITY_TERMS)
            or matches_term(officer.get("country_of_residence"), NORMALIZED_COUNTRY_TERMS)
            or matches_term((officer.get("address") or {}).get("country"), NORMALIZED_COUNTRY_TERMS)
        ):
            return True
    return False


def analyse_psc_flags(client: CHClient, company_number: str) -> Tuple[bool, bool]:
    pscs = get_all_pscs(client, company_number)
    international_shareholder = False
    owned_by_company = False
    for psc in pscs:
        kind = str(psc.get("kind", ""))
        if (
            matches_term(psc.get("nationality"), NORMALIZED_NATIONALITY_TERMS)
            or matches_term(psc.get("country_of_residence"), NORMALIZED_COUNTRY_TERMS)
            or matches_term((psc.get("address") or {}).get("country"), NORMALIZED_COUNTRY_TERMS)
        ):
            international_shareholder = True
        if kind in COMPANY_OWNER_KINDS or "corporate" in kind or "legal-person" in kind:
            owned_by_company = True
    return international_shareholder, owned_by_company


def parse_matching_sic(item: Dict[str, Any]) -> str:
    item_sics = [str(code) for code in (item.get("sic_codes") or [])]
    matched = [code for code in item_sics if code in ALLOWED_SIC_CODES]
    return ", ".join(matched or item_sics[:1])


def process_company(client: CHClient, item: Dict[str, Any], target_date: str) -> Dict[str, Any]:
    company_number = item.get("company_number", "")
    international_director = has_international_director(client, company_number)
    international_shareholder, owned_by_company = analyse_psc_flags(client, company_number)
    return {
        "company_number": company_number,
        "company_name": item.get("company_name") or item.get("title") or "",
        "sic_code": parse_matching_sic(item),
        "incorporation_date": target_date,
        "company_type": item.get("company_type", ""),
        "international_director": international_director,
        "international_shareholder": international_shareholder,
        "owned_by_company": owned_by_company,
        "pulled_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "raw_json": item,
    }


def main():
    st.title("Companies House New Incorporations Screener")
    st.caption("Pulls newly incorporated active companies, screens target SIC codes, then enriches results with officer and PSC checks.")

    with st.expander("Secrets format", expanded=False):
        st.code(
            'COMPANIES_HOUSE_API_KEYS = [\n  "key-1",\n  "key-2",\n  "key-3"\n]',
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
                st.code("\n".join(failures[:50]))
                status.update(label="Completed with some errors", state="error")
            else:
                status.update(label="Refresh complete", state="complete")

    result_df = read_results(conn, date_str, only_ticked=True)
    st.subheader("Results")
    st.caption("Only companies with at least one tick are shown.")
    st.dataframe(result_df, use_container_width=True, hide_index=True)

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
- Display rule: only rows with at least one tick are shown in the results table and CSV
        """
    )


if __name__ == "__main__":
    main()
    "greek", "italian", "hungarian", "croatian", "irish", "chinese", "indian", "hong kong",
    "hongkong", "singaporean", "dutch", "netherlands",
}
COMPANY_OWNER_KINDS = {
    "corporate-entity-person-with-significant-control",
    "legal-person-person-with-significant-control",
    "super-secure-person-with-significant-control",
}


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = text.replace("-", " ")
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
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


def matches_term(value: Any, lookup: set) -> bool:
    norm = normalize_text(value)
    return bool(norm) and norm in lookup


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
            international_director, international_shareholder, owned_by_company, pulled_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["company_number"],
            row["company_name"],
            row["sic_code"],
            row["incorporation_date"],
            row["company_type"],
            int(row["international_director"]),
            int(row["international_shareholder"]),
            int(row["owned_by_company"]),
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
            "Company Name", "SIC Code", "International Director?", "International Shareholder?", "Owned By A Company?", "Pulled At"
        ])
    return pd.DataFrame({
        "Company Name": df["company_name"],
        "SIC Code": df["sic_code"],
        "International Director?": df["international_director"].map(lambda x: "✓" if int(x) else ""),
        "International Shareholder?": df["international_shareholder"].map(lambda x: "✓" if int(x) else ""),
        "Owned By A Company?": df["owned_by_company"].map(lambda x: "✓" if int(x) else ""),
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
    return normalize_text(value) in {normalize_text(x) for x in ALLOWED_COMPANY_TYPES}


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


def has_international_director(client: CHClient, company_number: str) -> bool:
    officers = get_all_officers(client, company_number)
    for officer in officers:
        role = normalize_text(officer.get("officer_role"))
        if "director" not in role and role != "designated member":
            continue
        if (
            matches_term(officer.get("nationality"), NORMALIZED_NATIONALITY_TERMS)
            or matches_term(officer.get("country_of_residence"), NORMALIZED_COUNTRY_TERMS)
            or matches_term((officer.get("address") or {}).get("country"), NORMALIZED_COUNTRY_TERMS)
        ):
            return True
    return False


def analyse_psc_flags(client: CHClient, company_number: str) -> Tuple[bool, bool]:
    pscs = get_all_pscs(client, company_number)
    international_shareholder = False
    owned_by_company = False
    for psc in pscs:
        kind = str(psc.get("kind", ""))
        if (
            matches_term(psc.get("nationality"), NORMALIZED_NATIONALITY_TERMS)
            or matches_term(psc.get("country_of_residence"), NORMALIZED_COUNTRY_TERMS)
            or matches_term((psc.get("address") or {}).get("country"), NORMALIZED_COUNTRY_TERMS)
        ):
            international_shareholder = True
        if kind in COMPANY_OWNER_KINDS or "corporate" in kind or "legal-person" in kind:
            owned_by_company = True
    return international_shareholder, owned_by_company


def parse_matching_sic(item: Dict[str, Any]) -> str:
    item_sics = [str(code) for code in (item.get("sic_codes") or [])]
    matched = [code for code in item_sics if code in ALLOWED_SIC_CODES]
    return ", ".join(matched or item_sics[:1])


def process_company(client: CHClient, item: Dict[str, Any], target_date: str) -> Dict[str, Any]:
    company_number = item.get("company_number", "")
    international_director = has_international_director(client, company_number)
    international_shareholder, owned_by_company = analyse_psc_flags(client, company_number)
    return {
        "company_number": company_number,
        "company_name": item.get("company_name") or item.get("title") or "",
        "sic_code": parse_matching_sic(item),
        "incorporation_date": target_date,
        "company_type": item.get("company_type", ""),
        "international_director": international_director,
        "international_shareholder": international_shareholder,
        "owned_by_company": owned_by_company,
        "pulled_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "raw_json": item,
    }


def main():
    st.title("Companies House New Incorporations Screener")
    st.caption("Pulls newly incorporated active companies, screens target SIC codes, then enriches results with officer and PSC checks.")

    with st.expander("Secrets format", expanded=False):
        st.code(
            'COMPANIES_HOUSE_API_KEYS = [\n  "key-1",\n  "key-2",\n  "key-3"\n]',
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
                st.code("\n".join(failures[:50]))
                status.update(label="Completed with some errors", state="error")
            else:
                status.update(label="Refresh complete", state="complete")

    result_df = read_results(conn, date_str)
    st.subheader("Results")
    st.dataframe(result_df, use_container_width=True, hide_index=True)

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
        """
    )


if __name__ == "__main__":
    main()
