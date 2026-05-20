import os
import re
import time
import math
import json
import sqlite3
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Companies House New Incorporations Screener", layout="wide")

BASE_URL = "https://api.company-information.service.gov.uk"
DB_PATH = "companies_house_screening.db"
ALLOWED_SIC_CODES = [
    "62012", "62020", "63120", "47910", "46190", "46499", "70229", "73110", "74909", "68209",
    "64209", "68100", "32990", "10890", "86900", "93130", "96040", "82990", "72110", "56101",
]
COUNTRY_TERMS = {
    "usa", "united states", "united states of america", "america", "france", "germany", "belgium",
    "norway", "sweden", "finland", "denmark", "austria", "poland", "spain", "portugal", "greece",
    "italy", "hungary", "croatia", "ireland", "china", "netherlands", "india", "hong kong",
    "hong-kong", "singapore",
}
NATIONALITY_TERMS = {
    "american", "us", "u.s.", "u.s.a.", "united states", "united states of america", "french", "german",
    "belgian", "norwegian", "swedish", "finnish", "danish", "austrian", "polish", "spanish", "portuguese",
    "greek", "italian", "hungarian", "croatian", "irish", "chinese", "indian", "hong kong", "hong-kong",
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
        "the netherlands": "netherlands",
    }
    return aliases.get(text, text)


def matches_country_or_nationality(value: Any, lookup: set) -> bool:
    norm = normalize_text(value)
    if not norm:
        return False
    return norm in lookup


class CHClient:
    def __init__(self, api_keys: List[str]):
        self.api_keys = [k.strip() for k in api_keys if str(k).strip()]
        if not self.api_keys:
            raise ValueError("No Companies House API keys supplied.")
        self.idx = 0
        self.session = requests.Session()

    def _auth(self) -> Tuple[str, str]:
        key = self.api_keys[self.idx % len(self.api_keys)]
        return (key, "")

    def _rotate(self):
        self.idx = (self.idx + 1) % len(self.api_keys)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        last_error = None
        for _ in range(len(self.api_keys) * 2):
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
                    time.sleep(0.4)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = str(exc)
                self._rotate()
                time.sleep(0.4)
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
            row["company_number"], row["company_name"], row["sic_code"], row["incorporation_date"], row["company_type"],
            int(row["international_director"]), int(row["international_shareholder"]), int(row["owned_by_company"]),
            row["pulled_at"], json.dumps(row.get("raw_json", {})),
        ),
    )
    conn.commit()


def read_results(conn: sqlite3.Connection, incorporation_date: Optional[str] = None) -> pd.DataFrame:
    if incorporation_date:
        query = "SELECT * FROM screened_companies WHERE incorporation_date = ? ORDER BY pulled_at DESC"
        df = pd.read_sql_query(query, conn, params=(incorporation_date,))
    else:
        query = "SELECT * FROM screened_companies ORDER BY pulled_at DESC"
        df = pd.read_sql_query(query, conn)
    if df.empty:
        return pd.DataFrame(columns=[
            "Company Name", "SIC Code", "International Director?", "International Shareholder?", "Owned By A Company?", "Pulled At"
        ])
    display = pd.DataFrame({
        "Company Name": df["company_name"],
        "SIC Code": df["sic_code"],
        "International Director?": df["international_director"].map(lambda x: "✓" if int(x) else ""),
        "International Shareholder?": df["international_shareholder"].map(lambda x: "✓" if int(x) else ""),
        "Owned By A Company?": df["owned_by_company"].map(lambda x: "✓" if int(x) else ""),
        "Pulled At": df["pulled_at"],
    })
    return display


def get_company_type_candidates() -> List[str]:
    return [
        "ltd",
        "private-limited-guarant-nsc",
        "private-limited-shares-section-30-exemption",
        "llp",
    ]


def search_new_companies(client: CHClient, target_date: str, sic_codes: List[str]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    start_index = 0
    size = 100
    type_candidates = get_company_type_candidates()
    for company_type in type_candidates:
        while True:
            params = {
                "incorporated_from": target_date,
                "incorporated_to": target_date,
                "company_status": "active",
                "company_type": company_type,
                "sic_codes": ",".join(sic_codes),
                "size": size,
                "start_index": start_index,
            }
            payload = client.get("/advanced-search/companies", params=params)
            batch = payload.get("items", []) or []
            items.extend(batch)
            total = int(payload.get("total_results", 0) or 0)
            start_index += size
            if start_index >= total or not batch:
                break
        start_index = 0
    deduped = {}
    for item in items:
        company_number = item.get("company_number")
        if company_number:
            deduped[company_number] = item
    return list(deduped.values())


def get_officers(client: CHClient, company_number: str) -> List[Dict[str, Any]]:
    payload = client.get(f"/company/{company_number}/officers")
    return payload.get("items", []) or []


def get_pscs(client: CHClient, company_number: str) -> List[Dict[str, Any]]:
    payload = client.get(f"/company/{company_number}/persons-with-significant-control")
    return payload.get("items", []) or []


def has_international_director(client: CHClient, company_number: str) -> bool:
    officers = get_officers(client, company_number)
    for officer in officers:
        role = normalize_text(officer.get("officer_role"))
        if "director" not in role and role != "designated member":
            continue
        nationality = officer.get("nationality")
        residence = officer.get("country_of_residence")
        address_country = ((officer.get("address") or {}).get("country"))
        if (
            matches_country_or_nationality(nationality, NATIONALITY_TERMS)
            or matches_country_or_nationality(residence, COUNTRY_TERMS)
            or matches_country_or_nationality(address_country, COUNTRY_TERMS)
        ):
            return True
    return False


def analyse_psc_flags(client: CHClient, company_number: str) -> Tuple[bool, bool]:
    international_shareholder = False
    owned_by_company = False
    pscs = get_pscs(client, company_number)
    for psc in pscs:
        kind = psc.get("kind", "")
        n = psc.get("nationality")
        country = psc.get("country_of_residence")
        address_country = ((psc.get("address") or {}).get("country"))
        if (
            matches_country_or_nationality(n, NATIONALITY_TERMS)
            or matches_country_or_nationality(country, COUNTRY_TERMS)
            or matches_country_or_nationality(address_country, COUNTRY_TERMS)
        ):
            international_shareholder = True
        if kind in COMPANY_OWNER_KINDS or psc.get("name"):
            if "corporate" in kind or "legal-person" in kind:
                owned_by_company = True
    return international_shareholder, owned_by_company


def parse_matching_sic(item: Dict[str, Any]) -> str:
    sic_codes = item.get("sic_codes") or item.get("sic_codes") or []
    matched = [str(code) for code in sic_codes if str(code) in ALLOWED_SIC_CODES]
    if not matched and sic_codes:
        matched = [str(sic_codes[0])]
    return ", ".join(matched)


def process_company(client: CHClient, item: Dict[str, Any], target_date: str) -> Dict[str, Any]:
    company_number = item.get("company_number", "")
    name = item.get("company_name") or item.get("title") or ""
    international_director = has_international_director(client, company_number)
    international_shareholder, owned_by_company = analyse_psc_flags(client, company_number)
    return {
        "company_number": company_number,
        "company_name": name,
        "sic_code": parse_matching_sic(item),
        "incorporation_date": target_date,
        "company_type": item.get("company_type", ""),
        "international_director": international_director,
        "international_shareholder": international_shareholder,
        "owned_by_company": owned_by_company,
        "pulled_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "raw_json": item,
    }


def validate_api_keys() -> List[str]:
    if "COMPANIES_HOUSE_API_KEYS" not in st.secrets:
        raise ValueError(
            "Missing COMPANIES_HOUSE_API_KEYS in .streamlit/secrets.toml. "
            "Expected a TOML list named COMPANIES_HOUSE_API_KEYS."
        )
    keys = list(st.secrets["COMPANIES_HOUSE_API_KEYS"])
    cleaned = [str(k).strip() for k in keys if str(k).strip()]
    if not cleaned:
        raise ValueError("COMPANIES_HOUSE_API_KEYS is empty.")
    return cleaned


def main():
    st.title("Companies House New Incorporations Screener")
    st.caption("Searches new active incorporations by date, SIC code and company type, then enriches each result with officer and PSC checks.")

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

    col1, col2 = st.columns([1, 1])
    with col1:
        target_date = st.date_input("Incorporation date", value=date.today(), format="YYYY-MM-DD")
    with col2:
        run = st.button("Pull new companies", type="primary", use_container_width=True)

    date_str = target_date.strftime("%Y-%m-%d")
    st.write(f"Using {len(api_keys)} API key(s). Allowed SIC codes loaded: {len(ALLOWED_SIC_CODES)}")

    if run:
        with st.status("Pulling companies and enriching results...", expanded=True) as status:
            st.write("Searching Companies House advanced search endpoint...")
            companies = search_new_companies(client, date_str, ALLOWED_SIC_CODES)
            already_seen = existing_company_numbers(conn, date_str)
            new_companies = [c for c in companies if c.get("company_number") not in already_seen]
            st.write(f"Found {len(companies)} matching companies, {len(new_companies)} new to process.")

            progress = st.progress(0)
            total = max(len(new_companies), 1)
            processed = 0
            for item in new_companies:
                try:
                    row = process_company(client, item, date_str)
                    upsert_company(conn, row)
                except Exception as exc:
                    st.warning(f"Skipped {item.get('company_number', 'unknown')} due to error: {exc}")
                processed += 1
                progress.progress(min(processed / total, 1.0))
            status.update(label="Refresh complete", state="complete")

    result_df = read_results(conn, date_str)
    st.subheader("Results")
    st.dataframe(result_df, use_container_width=True, hide_index=True)

    export_df = result_df.copy()
    csv = export_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv,
        file_name=f"companies_house_screening_{date_str}.csv",
        mime="text/csv",
        use_container_width=True,
    )

    st.subheader("Rules applied")
    st.markdown(
        """
- Company status: Active
- SIC codes: predefined allow-list of 20 codes
- Company types searched: Private Limited Company variants and LLP
- International Director?: ticks when director/designated member nationality, country of residence or address country matches target countries
- International Shareholder?: ticks when PSC nationality, residence or address country matches target countries/nationalities
- Owned By A Company?: ticks when a PSC record indicates a corporate or legal-person owner
- Refresh logic: already-screened company numbers for the selected incorporation date are skipped on future refreshes
        """
    )


if __name__ == "__main__":
    main()
