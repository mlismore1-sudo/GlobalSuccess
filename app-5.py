import base64
import json
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Companies House International Screening", layout="wide")

BASE_URL = "https://api.company-information.service.gov.uk"
DATA_DIR = Path("data")
REVIEWED_FILE = DATA_DIR / "reviewed_companies.json"
RESULTS_FILE = DATA_DIR / "results_history.csv"

TARGET_SIC_CODES = {
    "62012", "62020", "63120", "47910", "46190", "46499", "70229", "73110", "74909", "68209",
    "64209", "68100", "32990", "10890", "86900", "93130", "96040", "82990", "72110", "56101",
}

ALLOWED_COMPANY_TYPES = {"ltd", "llp"}
ALLOWED_OFFICER_ROLES = {"director", "llp member"}

TARGET_DIRECTOR_COUNTRIES = {
    "usa", "united states of america", "america", "france", "germany", "belgium", "norway", "sweden",
    "finland", "denmark", "austria", "poland", "spain", "portugal", "greece", "italy", "hungary",
    "croatia", "ireland", "china", "netherlands", "india", "hong kong", "singapore",
}

TARGET_PSC_NATIONALITIES = {
    "american", "usa", "us", "french", "german", "belgian", "norwegian", "swedish", "finnish",
    "danish", "austrian", "polish", "spanish", "portuguese", "greek", "italian", "hungarian",
    "croatian", "irish", "chinese", "dutch", "indian", "hong konger", "hong kong chinese", "singaporean",
}

PSC_CORPORATE_KINDS = {
    "corporate entity person with significant control",
    "corporate entity beneficial owner",
    "legal person person with significant control",
    "legal person beneficial owner",
    "super secure person with significant control",
    "super secure beneficial owner",
}


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not REVIEWED_FILE.exists():
        REVIEWED_FILE.write_text("{}", encoding="utf-8")
    if not RESULTS_FILE.exists():
        pd.DataFrame(columns=[
            "screened_at",
            "search_date",
            "company_number",
            "company_name",
            "sic_code",
            "international_director",
            "international_shareholder",
            "owned_by_a_company",
        ]).to_csv(RESULTS_FILE, index=False)


def normalize_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    for ch in [",", ".", ";", ":", "(", ")", "[", "]", "{", "}", "'", '"']:
        text = text.replace(ch, " ")
    text = text.replace("-", " ")
    return " ".join(text.split())


def load_api_keys() -> List[str]:
    keys: List[str] = []
    try:
        raw = st.secrets.get("COMPANIES_HOUSE_API_KEYS", [])
        if isinstance(raw, list):
            keys.extend([str(x).strip() for x in raw if str(x).strip()])
        elif isinstance(raw, str) and raw.strip():
            keys.append(raw.strip())
    except Exception:
        pass
    return keys


class CompaniesHouseClient:
    def __init__(self, api_keys: List[str]):
        self.api_keys = [k for k in api_keys if k]
        if not self.api_keys:
            raise ValueError("No API keys found. Add COMPANIES_HOUSE_API_KEYS to .streamlit/secrets.toml")
        self.session = requests.Session()
        self.key_queue = deque(self.api_keys)

    def _auth_header(self, api_key: str) -> Dict[str, str]:
        token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("utf-8")
        return {"Authorization": f"Basic {token}"}

    def get(self, path: str, params: Optional[Dict] = None, timeout: int = 30) -> requests.Response:
        attempts = 0
        last_response = None
        while attempts < max(3, len(self.api_keys) * 2):
            api_key = self.key_queue[0]
            self.key_queue.rotate(-1)
            try:
                response = self.session.get(
                    f"{BASE_URL}{path}",
                    params=params,
                    headers=self._auth_header(api_key),
                    timeout=timeout,
                )
                last_response = response
                if response.status_code == 429:
                    attempts += 1
                    time.sleep(0.5)
                    continue
                return response
            except requests.RequestException:
                attempts += 1
                time.sleep(0.5)
        if last_response is not None:
            return last_response
        raise requests.RequestException("All API requests failed")


def load_reviewed() -> Dict[str, dict]:
    ensure_storage()
    try:
        return json.loads(REVIEWED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_reviewed(reviewed: Dict[str, dict]) -> None:
    REVIEWED_FILE.write_text(json.dumps(reviewed, indent=2), encoding="utf-8")


def load_results() -> pd.DataFrame:
    ensure_storage()
    try:
        return pd.read_csv(RESULTS_FILE)
    except Exception:
        return pd.DataFrame(columns=[
            "screened_at", "search_date", "company_number", "company_name", "sic_code",
            "international_director", "international_shareholder", "owned_by_a_company"
        ])


def save_results(df: pd.DataFrame) -> None:
    df.to_csv(RESULTS_FILE, index=False)


def fetch_companies_for_date(client: CompaniesHouseClient, target_date: str) -> List[dict]:
    companies: List[dict] = []
    start_index = 0
    page_size = 200
    while True:
        params = {
            "incorporated_from": target_date,
            "incorporated_to": target_date,
            "company_status": "active",
            "company_type": "ltd,llp",
            "sic_codes": ",".join(sorted(TARGET_SIC_CODES)),
            "size": page_size,
            "start_index": start_index,
        }
        resp = client.get("/advanced-search/companies", params=params)
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", [])
        if not items:
            break
        companies.extend(items)
        if len(items) < page_size:
            break
        start_index += page_size
        time.sleep(0.1)
    return companies


def extract_target_sic(item: dict) -> str:
    codes = item.get("sic_codes") or []
    if isinstance(codes, str):
        codes = [c.strip() for c in codes.split(",") if c.strip()]
    for code in codes:
        if str(code) in TARGET_SIC_CODES:
            return str(code)
    return str(codes[0]) if codes else ""


def is_active_officer(officer: dict) -> bool:
    return not officer.get("resigned_on")


def officer_role_matches(officer: dict) -> bool:
    return normalize_text(officer.get("officer_role")) in ALLOWED_OFFICER_ROLES


def has_international_director(client: CompaniesHouseClient, company_number: str) -> bool:
    start_index = 0
    while True:
        resp = client.get(f"/company/{company_number}/officers", params={"items_per_page": 100, "start_index": start_index})
        if resp.status_code in (404, 400):
            return False
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", [])
        if not items:
            return False
        for officer in items:
            if not is_active_officer(officer):
                continue
            if not officer_role_matches(officer):
                continue
            cor = normalize_text(officer.get("country_of_residence"))
            if cor in TARGET_DIRECTOR_COUNTRIES:
                return True
        if len(items) < 100:
            return False
        start_index += 100
        time.sleep(0.05)


def has_psc_nationality_match(psc: dict) -> bool:
    nationality = normalize_text(psc.get("nationality"))
    return nationality in TARGET_PSC_NATIONALITIES


def is_corporate_psc(psc: dict) -> bool:
    kind = normalize_text(psc.get("kind"))
    return kind in PSC_CORPORATE_KINDS


def psc_is_ceased(psc: dict) -> bool:
    return bool(psc.get("ceased_on"))


def get_psc_flags(client: CompaniesHouseClient, company_number: str) -> Tuple[bool, bool]:
    start_index = 0
    shareholder_match = False
    corporate_owner = False
    while True:
        resp = client.get(
            f"/company/{company_number}/persons-with-significant-control",
            params={"items_per_page": 100, "start_index": start_index, "register_view": "true"},
        )
        if resp.status_code in (404, 400):
            return shareholder_match, corporate_owner
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", [])
        if not items:
            return shareholder_match, corporate_owner
        for psc in items:
            if psc_is_ceased(psc):
                continue
            if has_psc_nationality_match(psc):
                shareholder_match = True
            if is_corporate_psc(psc):
                corporate_owner = True
        if len(items) < 100:
            return shareholder_match, corporate_owner
        start_index += 100
        time.sleep(0.05)


def screen_new_companies(client: CompaniesHouseClient, target_date: str, progress_bar=None, status_box=None):
    reviewed = load_reviewed()
    results_df = load_results()
    raw_companies = fetch_companies_for_date(client, target_date)
    candidates: List[dict] = []
    for item in raw_companies:
        company_number = str(item.get("company_number", "")).strip()
        if not company_number:
            continue
        if reviewed.get(company_number):
            continue
        company_type = normalize_text(item.get("company_type"))
        if company_type not in ALLOWED_COMPANY_TYPES:
            continue
        sic_code = extract_target_sic(item)
        if sic_code not in TARGET_SIC_CODES:
            continue
        candidates.append(item)

    new_rows = []
    total_candidates = len(candidates)
    if progress_bar is not None:
        progress_bar.progress(0, text=f"Starting screening for {total_candidates} company(ies)...")
    if status_box is not None:
        status_box.info(f"Preparing to screen {total_candidates} new company(ies).")

    for idx, item in enumerate(candidates, start=1):
        company_number = str(item.get("company_number", "")).strip()
        company_name = item.get("company_name") or item.get("title") or ""
        sic_code = extract_target_sic(item)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if status_box is not None:
            status_box.info(f"Screening {idx}/{total_candidates}: {company_name} ({company_number})")

        director_flag = has_international_director(client, company_number)
        shareholder_flag, company_owner_flag = get_psc_flags(client, company_number)
        row = {
            "screened_at": ts,
            "search_date": target_date,
            "company_number": company_number,
            "company_name": company_name,
            "sic_code": sic_code,
            "international_director": "✓" if director_flag else "",
            "international_shareholder": "✓" if shareholder_flag else "",
            "owned_by_a_company": "✓" if company_owner_flag else "",
        }
        new_rows.append(row)
        reviewed[company_number] = {"search_date": target_date, "screened_at": ts}

        if progress_bar is not None and total_candidates > 0:
            progress_bar.progress(idx / total_candidates, text=f"Processed {idx} of {total_candidates} companies")

    save_reviewed(reviewed)

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        results_df = pd.concat([new_df, results_df], ignore_index=True)
        results_df = results_df.sort_values("screened_at", ascending=False, kind="stable").reset_index(drop=True)
        save_results(results_df)
    else:
        new_df = pd.DataFrame(columns=[
            "screened_at", "search_date", "company_number", "company_name", "sic_code",
            "international_director", "international_shareholder", "owned_by_a_company"
        ])

    filtered_new_df = new_df[
        (new_df["international_director"] == "✓") |
        (new_df["international_shareholder"] == "✓") |
        (new_df["owned_by_a_company"] == "✓")
    ].copy()

    filtered_all_df = results_df[
        (results_df["international_director"] == "✓") |
        (results_df["international_shareholder"] == "✓") |
        (results_df["owned_by_a_company"] == "✓")
    ].copy()

    display_new = filtered_new_df[[
        "company_name", "sic_code", "international_director", "international_shareholder", "owned_by_a_company"
    ]].rename(columns={
        "company_name": "Company Name",
        "sic_code": "SIC Code",
        "international_director": "International Director?",
        "international_shareholder": "International Shareholder?",
        "owned_by_a_company": "Owned By A Company?",
    })

    display_all = filtered_all_df[[
        "company_name", "sic_code", "international_director", "international_shareholder", "owned_by_a_company", "screened_at"
    ]].rename(columns={
        "company_name": "Company Name",
        "sic_code": "SIC Code",
        "international_director": "International Director?",
        "international_shareholder": "International Shareholder?",
        "owned_by_a_company": "Owned By A Company?",
        "screened_at": "Pulled At",
    })
    if progress_bar is not None:
        progress_bar.progress(1.0, text="Screening complete")
    if status_box is not None:
        status_box.success(f"Finished screening {len(candidates)} new company(ies).")
    return display_new, len(raw_companies), len(candidates), display_all


def reset_storage() -> None:
    if REVIEWED_FILE.exists():
        REVIEWED_FILE.unlink()
    if RESULTS_FILE.exists():
        RESULTS_FILE.unlink()
    ensure_storage()


def build_display_all() -> pd.DataFrame:
    all_results_df = load_results()
    if all_results_df.empty:
        return pd.DataFrame(columns=[
            "Company Name", "SIC Code", "International Director?", "International Shareholder?",
            "Owned By A Company?", "Pulled At"
        ])
    filtered_all_df = all_results_df[
        (all_results_df["international_director"] == "✓") |
        (all_results_df["international_shareholder"] == "✓") |
        (all_results_df["owned_by_a_company"] == "✓")
    ].copy()
    return filtered_all_df[[
        "company_name", "sic_code", "international_director", "international_shareholder", "owned_by_a_company", "screened_at"
    ]].rename(columns={
        "company_name": "Company Name",
        "sic_code": "SIC Code",
        "international_director": "International Director?",
        "international_shareholder": "International Shareholder?",
        "owned_by_a_company": "Owned By A Company?",
        "screened_at": "Pulled At",
    })


def main() -> None:
    ensure_storage()
    st.title("Companies House International Screening")
    st.caption("Single-file Streamlit app for daily Companies House screening using rotating API keys.")

    with st.sidebar:
        st.header("Search settings")
        target_date = st.date_input("Incorporation date", value=date.today(), format="YYYY-MM-DD")
        run_scan = st.button("Run scan", type="primary", use_container_width=True)
        reset_btn = st.button("Reset reviewed history", use_container_width=True)
        st.markdown("### Secrets format")
        st.code("""# .streamlit/secrets.toml
COMPANIES_HOUSE_API_KEYS = [
  "key_1",
  "key_2",
  "key_3"
]""", language="toml")

    if reset_btn:
        reset_storage()
        st.success("Reviewed history and saved results have been cleared.")

    api_keys = load_api_keys()
    if not api_keys:
        st.error("No API keys found. Add COMPANIES_HOUSE_API_KEYS to .streamlit/secrets.toml before running the app.")
        st.stop()

    st.info(f"Loaded {len(api_keys)} Companies House API key(s).")

    display_all = build_display_all()
    reviewed = load_reviewed()
    col1, col2, col3 = st.columns(3)
    col1.metric("Reviewed companies", len(reviewed))
    col2.metric("Stored results", len(display_all))
    col3.metric("Target SIC codes", len(TARGET_SIC_CODES))

    if run_scan:
        try:
            client = CompaniesHouseClient(api_keys)
            progress_bar = st.progress(0, text="Waiting to start...")
            status_box = st.empty()
            with st.spinner("Searching and screening new companies..."):
                new_df, raw_count, candidate_count, display_all = screen_new_companies(
                    client,
                    target_date.isoformat(),
                    progress_bar=progress_bar,
                    status_box=status_box,
                )
            st.success(f"Scan complete. Found {raw_count} raw companies, {candidate_count} new candidates, {len(new_df)} newly screened rows.")
            st.subheader("New companies from this scan")
            st.dataframe(new_df, use_container_width=True, hide_index=True)
        except Exception as exc:
            st.exception(exc)

    st.subheader("All screened companies")
    st.dataframe(display_all, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
