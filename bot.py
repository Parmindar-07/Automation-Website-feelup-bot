import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

import gspread
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing in .env.")
    return value


def parse_field_map(raw: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise RuntimeError(f"FIELD_MAP item is invalid: {item}")
        column, selector = item.split(":", 1)
        mapping[column.strip()] = selector.strip()
    return mapping


def parse_label_list(raw: str) -> Set[str]:
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def strip_llc(value: str) -> str:
    return re.sub(r"\bL\.?\s*L\.?\s*C\.?\b", "", value, flags=re.IGNORECASE).strip()


def column_to_number(value: str) -> int:
    text = value.strip().upper()
    if not text:
        return 2
    if text.isdigit():
        return max(int(text), 2)

    number = 0
    for char in text:
        if not ("A" <= char <= "Z"):
            raise RuntimeError(f"Invalid start column: {value}")
        number = number * 26 + (ord(char) - ord("A") + 1)
    return max(number, 2)


def ask_start_column(default: str) -> int:
    if env_bool("ASK_START_COLUMN", True):
        try:
            import tkinter as tk
            from tkinter import simpledialog

            root = tk.Tk()
            root.withdraw()
            answer = simpledialog.askstring(
                "Start Column",
                "Enter starting customer column (example: B, C, D):",
                initialvalue=default,
            )
            root.destroy()
            if answer is None:
                raise RuntimeError("Start column selection was cancelled.")
            return column_to_number(answer)
        except Exception as exc:
            print(f"Input box could not be opened: {exc}")

    answer = input(f"Enter start customer column (default {default}): ").strip() or default
    return column_to_number(answer)


@dataclass
class SheetRecord:
    column_number: int
    column_name: str
    data: Dict[str, str]


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
OAUTH_CREDENTIALS_FILE = "oauth_credentials.json"
OAUTH_TOKEN_FILE = "oauth_token.json"


def _get_oauth_client() -> gspread.Client:
    creds = None
    if os.path.exists(OAUTH_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(OAUTH_TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(OAUTH_TOKEN_FILE, "w") as token_file:
            token_file.write(creds.to_json())
    return gspread.authorize(creds)


class GoogleSheetClient:
    def __init__(self) -> None:
        sheet_url = required_env("GOOGLE_SHEET_URL")
        worksheet_name = os.getenv("WORKSHEET_NAME", "").strip()

        # Use OAuth if oauth_credentials.json exists, otherwise use service account
        if os.path.exists(OAUTH_CREDENTIALS_FILE):
            client = _get_oauth_client()
        else:
            service_account_json = required_env("GOOGLE_SERVICE_ACCOUNT_JSON")
            client = gspread.service_account(filename=service_account_json)

        spreadsheet = client.open_by_url(sheet_url)
        self.worksheet = (
            spreadsheet.worksheet(worksheet_name)
            if worksheet_name
            else spreadsheet.sheet1
        )
        self.values = self.worksheet.get_all_values()
        self.label_rows = self._build_label_rows()

    def _build_label_rows(self) -> Dict[str, int]:
        labels: Dict[str, int] = {}
        for row_index, row in enumerate(self.values, start=1):
            label = row[0].strip() if row else ""
            if label:
                labels[label.lower()] = row_index
        return labels

    def get_pending_records(
        self,
        customer_id_label: str,
        skip_labels: Set[str],
        required_label: str,
        start_column: int,
    ) -> List[SheetRecord]:
        customer_id_row = self.ensure_label_row(customer_id_label)
        max_columns = max((len(row) for row in self.values), default=0)
        pending: List[SheetRecord] = []

        for column_index in range(start_column, max_columns + 1):
            data: Dict[str, str] = {}
            for row_index, row in enumerate(self.values, start=1):
                if not row:
                    continue
                label = row[0].strip()
                if not label or label.lower() in skip_labels:
                    continue
                value = row[column_index - 1].strip() if len(row) >= column_index else ""
                data[label] = value

            required_labels = [r.strip() for r in required_label.split(",") if r.strip()]
            required_value = next(
                (data.get(r, "").strip() for r in required_labels if data.get(r, "").strip()), ""
            )
            existing_customer_id = self._cell_value(customer_id_row, column_index)
            if required_value and not existing_customer_id:
                pending.append(
                    SheetRecord(
                        column_number=column_index,
                        column_name=required_value,
                        data=data,
                    )
                )

        return pending

    def ensure_label_row(self, label: str) -> int:
        wanted = label.strip().lower()
        if wanted in self.label_rows:
            return self.label_rows[wanted]

        # Fuzzy match ignoring special characters and spaces
        wanted_clean = re.sub(r"[^a-z0-9]", "", wanted)
        for existing_label, row_idx in self.label_rows.items():
            existing_clean = re.sub(r"[^a-z0-9]", "", existing_label)
            if wanted_clean == existing_clean:
                self.label_rows[wanted] = row_idx
                return row_idx

        # Label not found — expand sheet and add new row
        next_row = len(self.values) + 1
        current_cols = max((len(r) for r in self.values), default=1)
        self.worksheet.resize(rows=next_row, cols=max(current_cols, 1000))
        self.worksheet.update_cell(next_row, 1, label)
        self.values.append([label])
        self.label_rows[wanted] = next_row
        return next_row

    def update_customer_id(self, column_number: int, label: str, customer_id: str) -> None:
        row_number = self.ensure_label_row(label)
        self.worksheet.update_cell(row_number, column_number, customer_id)

    def _cell_value(self, row_number: int, column_number: int) -> str:
        if row_number > len(self.values):
            return ""
        row = self.values[row_number - 1]
        if column_number > len(row):
            return ""
        return row[column_number - 1].strip()


class WebsiteBot:
    def __init__(self) -> None:
        self.website_url = required_env("WEBSITE_URL")
        self.headless = env_bool("HEADLESS", False)
        self.field_map = parse_field_map(os.getenv("FIELD_MAP", ""))
        self.start_selector = os.getenv("START_SELECTOR", "text=Submit App").strip()
        self.dba_info_selector = os.getenv("DBA_INFO_SELECTOR", "text=DBA Info").strip()
        self.submit_selector = os.getenv("SUBMIT_SELECTOR", "text=Submit Application").strip()
        self.logo_selector = os.getenv("LOGO_SELECTOR", ".navbar-brand, img").strip()
        self.after_start_wait_seconds = int(os.getenv("AFTER_START_WAIT_SECONDS", "10"))
        self.after_submit_wait_seconds = int(os.getenv("AFTER_SUBMIT_WAIT_SECONDS", "5"))
        self.login_wait_seconds = int(os.getenv("LOGIN_WAIT_SECONDS", "120"))
        self.user_data_dir = os.getenv("USER_DATA_DIR", "browser_profile").strip()
        self.customer_id_selector = os.getenv("CUSTOMER_ID_SELECTOR", "").strip()
        self.customer_id_regex = os.getenv(
            "CUSTOMER_ID_REGEX",
            r"(?:Customer\s*ID\s*#?|Cust\s*#)\s*[:#-]?\s*(\d{10})",
        ).strip()

    def submit_row(self, row: Dict[str, str]) -> str:
        prepared_row = self._prepare_row(row)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=self.headless,
                channel="chrome",
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(self.website_url, wait_until="domcontentloaded", timeout=60000)
                page.locator(self.start_selector).first.wait_for(
                    timeout=self.login_wait_seconds * 1000
                )
                self._click(page, self.start_selector, "Submit App")
                page.wait_for_timeout(self.after_start_wait_seconds * 1000)
                self._click(page, self.dba_info_selector, "DBA Info")
                page.wait_for_load_state("networkidle", timeout=30000)
                page.wait_for_selector("input[type='text']:visible", timeout=15000)

                for column_name, selector in self.field_map.items():
                    value = prepared_row.get(column_name, "")
                    if value:
                        for target in selector.split("&&"):
                            try:
                                self._fill(page, target.strip(), value)
                                print(f"  OK: {column_name} -> {target.strip()}")
                            except Exception as e:
                                print(f"  FAIL: {column_name} -> {target.strip()} | Error: {e}")

                page.evaluate("""
                    const el = Array.from(document.querySelectorAll('input[type=button],input[type=submit],button'))
                        .find(e => (e.value || e.innerText || '').trim() === 'Submit Application');
                    if (el) { el.scrollIntoView(); el.click(); }
                    else { throw new Error('Submit Application button not found'); }
                """)
                page.wait_for_timeout(500)
                page.wait_for_timeout(self.after_submit_wait_seconds * 1000)
                customer_id = self._extract_customer_id(page, prepared_row)
                if not customer_id:
                    raise RuntimeError("Customer ID not found on website.")
                self._click(page, self.logo_selector, "logo", required=False)
                return customer_id
            finally:
                context.close()

    def _prepare_row(self, row: Dict[str, str]) -> Dict[str, str]:
        prepared = dict(row)
        legal_name = prepared.get("Business Legal Name", "").strip()
        dba_name = prepared.get("Business DBA Name", "").strip() or legal_name
        prepared["Business DBA Name"] = strip_llc(dba_name)
        return prepared

    def _click(self, page, selector: str, name: str, required: bool = True) -> None:
        try:
            locator = page.locator(selector).first
            locator.scroll_into_view_if_needed(timeout=10000)
            locator.click(timeout=20000)
        except Exception:
            # Fallback: try clicking via JavaScript
            try:
                page.evaluate(f"""
                    const el = document.querySelector({repr(selector)}) ||
                        Array.from(document.querySelectorAll('input[type=button],input[type=submit],button,a'))
                        .find(e => e.value === 'Submit Application' || e.innerText.trim() === 'Submit Application');
                    if (el) {{ el.scrollIntoView(); el.click(); }}
                """)
            except Exception:
                if required:
                    raise RuntimeError(f"{name} could not be clicked. Check selector: {selector}")

    def _fill(self, page, target: str, value: str) -> None:
        if target.startswith("label="):
            label, index = self._split_target(target.removeprefix("label="))
            # Try standard get_by_label first
            try:
                page.get_by_label(label, exact=True).nth(index).fill(value, timeout=3000)
                return
            except Exception:
                pass
            # Fallback: find input adjacent to label
            locator = page.locator(f"label:has-text('{label}') ~ input, label:has-text('{label}') + input").nth(index)
            try:
                locator.fill(value, timeout=3000)
                return
            except Exception:
                pass
            # Fallback 2: fill via JavaScript
            page.evaluate(f"""
                const labels = Array.from(document.querySelectorAll('label'));
                const lbl = labels.filter(l => l.innerText.trim() === {repr(label)})[{index}];
                if (lbl) {{
                    const input = lbl.querySelector('input') || document.getElementById(lbl.htmlFor);
                    if (input) {{ input.value = {repr(value)}; input.dispatchEvent(new Event('input', {{bubbles:true}})); input.dispatchEvent(new Event('change', {{bubbles:true}})); }}
                }}
            """)
            return

        if target.startswith("select-label="):
            label, index = self._split_target(target.removeprefix("select-label="))
            try:
                page.get_by_label(label, exact=True).nth(index).select_option(label=value, timeout=3000)
                return
            except Exception:
                pass
            # Fallback: select via JavaScript
            page.evaluate(f"""
                const labels = Array.from(document.querySelectorAll('label'));
                const lbl = labels.filter(l => l.innerText.trim() === {repr(label)})[{index}];
                if (lbl) {{
                    const sel = lbl.querySelector('select') || document.getElementById(lbl.htmlFor);
                    if (sel) {{
                        const opt = Array.from(sel.options).find(o => o.text.trim() === {repr(value)});
                        if (opt) {{ sel.value = opt.value; sel.dispatchEvent(new Event('change', {{bubbles:true}})); }}
                    }}
                }}
            """)
            return

        if target.startswith("select="):
            selector, index = self._split_target(target.removeprefix("select="))
            page.locator(selector).nth(index).select_option(label=value, timeout=15000)
            return

        page.locator(target).fill(value, timeout=15000)

    def _split_target(self, raw: str) -> tuple[str, int]:
        text = raw.strip()
        if "|" not in text:
            return text, 0
        label, index_text = text.rsplit("|", 1)
        return label.strip(), max(int(index_text.strip()) - 1, 0)

    def _extract_customer_id(self, page, row: Dict[str, str]) -> Optional[str]:
        if self.customer_id_selector:
            try:
                text = page.locator(self.customer_id_selector).first.inner_text(timeout=15000)
                customer_id = self._match_customer_id(text, row)
                if customer_id:
                    return customer_id
            except PlaywrightTimeoutError:
                pass

        page_text = page.locator("body").inner_text(timeout=15000)
        return self._match_customer_id(page_text, row)

    def _match_customer_id(self, text: str, row: Dict[str, str]) -> Optional[str]:
        if not self._result_matches_current_business(text, row):
            return None

        match = re.search(self.customer_id_regex, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None

    def _result_matches_current_business(self, text: str, row: Dict[str, str]) -> bool:
        legal_name = row.get("Business Legal Name", "").strip()
        dba_name = row.get("Business DBA Name", "").strip()
        normalized_text = normalize_match_text(text)
        return (
            bool(legal_name)
            and bool(dba_name)
            and normalize_match_text(legal_name) in normalized_text
            and normalize_match_text(dba_name) in normalized_text
        )


def process_pending(sheet: "GoogleSheetClient", website, customer_id_column: str, skip_labels, required_label: str, start_column: int) -> int:
    pending_records = sheet.get_pending_records(
        customer_id_column,
        skip_labels,
        required_label,
        start_column,
    )
    if not pending_records:
        return 0

    print(f"{len(pending_records)} pending customer column(s) found.")
    for record in pending_records:
        print(f"Processing column {record.column_number}: {record.column_name}")
        try:
            if website is None:
                customer_id = f"DRY-RUN-{int(time.time())}-{record.column_number}"
                print("WEBSITE_ENABLED=false. Dry-run customer id generated.")
            else:
                customer_id = website.submit_row(record.data)
            sheet.update_customer_id(record.column_number, customer_id_column, customer_id)
            print(f"Column {record.column_number} updated: {customer_id}")
        except Exception as exc:
            print(f"Column {record.column_number} failed: {exc}")

    return len(pending_records)


def run() -> int:
    load_dotenv()

    google_enabled = env_bool("GOOGLE_SHEETS_ENABLED", True)
    website_enabled = env_bool("WEBSITE_ENABLED", True)
    customer_id_column = os.getenv("CUSTOMER_ID_COLUMN", "customer id").strip()
    skip_labels = parse_label_list(os.getenv("SKIP_LABELS", ""))
    required_label = os.getenv("REQUIRED_LABEL", "Business Legal Name").strip()
    start_column = ask_start_column(os.getenv("START_COLUMN", "B"))
    poll_seconds = int(os.getenv("POLL_SECONDS", "5"))

    if not google_enabled:
        print("GOOGLE_SHEETS_ENABLED=false. Bot will not read/write sheet.")
        return 0

    website = WebsiteBot() if website_enabled else None
    print(f"Bot is running — checking sheet every {poll_seconds} sec. Press Ctrl+C to stop.")

    while True:
        try:
            sheet = GoogleSheetClient()
            processed = process_pending(sheet, website, customer_id_column, skip_labels, required_label, start_column)
            if processed == 0:
                print(f"No pending records. Checking again in {poll_seconds} sec...", end="\r")
        except Exception as exc:
            print(f"Error: {exc}")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        print("Bot stopped manually.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)
