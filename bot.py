"""
bot.py — Core automation engine for the Google Sheet → Website Form Bot.

Reads pending customer records from a Google Sheet and automatically submits
each one to a web form using Playwright. Customer IDs returned by the website
are written back to the sheet.

Usage:
    python bot.py              # sheet 1 (default)
    python bot.py --sheet 2    # sheet 2
    python bot.py --sheet 3    # sheet 3
"""

# ============================================================
# Standard Library Imports
# ============================================================

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

# ============================================================
# Third-Party Imports
# ============================================================

import gspread
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


# ============================================================
# Environment Helpers
# ============================================================

def env_bool(name: str, default: bool = True) -> bool:
    """Return a boolean from an environment variable.

    Accepts '1', 'true', 'yes', 'y', 'on' as True (case-insensitive).
    Falls back to `default` when the variable is not set.
    """
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def required_env(name: str) -> str:
    """Return a non-empty environment variable value or raise RuntimeError."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing in .env.")
    return value


# ============================================================
# Config Parsers
# ============================================================

def parse_field_map(raw: str) -> Dict[str, str]:
    """Parse FIELD_MAP string into {sheet_column_label: css_selector} dict.

    Format:  "Sheet Label:selector, Another Label:selector2"
    Multiple selectors for one label can be joined with '&&'.
    """
    mapping: Dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise RuntimeError(f"FIELD_MAP item is invalid: {item!r}")
        column, selector = item.split(":", 1)
        mapping[column.strip()] = selector.strip()
    return mapping


def parse_label_list(raw: str) -> Set[str]:
    """Parse a comma-separated list of labels into a lowercase set."""
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


# ============================================================
# Text Utilities
# ============================================================

def normalize_match_text(value: str) -> str:
    """Collapse value to lowercase alphanumeric + spaces for fuzzy matching."""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def strip_llc(value: str) -> str:
    """Remove 'LLC' / 'L.L.C.' suffix from a business name (case-insensitive)."""
    return re.sub(r"\bL\.?\s*L\.?\s*C\.?\b", "", value, flags=re.IGNORECASE).strip()


# ============================================================
# Column Utilities
# ============================================================

def column_to_number(value: str) -> int:
    """Convert a column identifier to a 1-based column number.

    Accepts:
        - A plain integer string  → returns max(int, 2)
        - An Excel-style letter   → 'A'=1, 'B'=2, 'AA'=27 …, floor at 2
    """
    text = value.strip().upper()
    if not text:
        return 2
    if text.isdigit():
        return max(int(text), 2)

    number = 0
    for char in text:
        if not ("A" <= char <= "Z"):
            raise RuntimeError(f"Invalid start column: {value!r}")
        number = number * 26 + (ord(char) - ord("A") + 1)
    return max(number, 2)


def ask_start_column(default: str) -> int:
    """Prompt the user for the first customer column via a GUI dialog or stdin.

    Shows a Tkinter dialog when ASK_START_COLUMN=true (default).
    Falls back to a plain stdin prompt when Tkinter is unavailable.
    """
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


# ============================================================
# Data Models
# ============================================================

@dataclass
class SheetRecord:
    """One pending customer column read from the Google Sheet."""

    column_number: int          # 1-based spreadsheet column index
    column_name: str            # Value of the required label cell (e.g. business name)
    data: Dict[str, str]        # {row_label: cell_value} for every non-skipped row


# ============================================================
# Google OAuth Constants
# ============================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
OAUTH_CREDENTIALS_FILE = "oauth_credentials.json"   # Downloaded from Google Cloud Console
OAUTH_TOKEN_FILE = "oauth_token.json"               # Auto-generated after first login


# ============================================================
# Google Sheets Authentication
# ============================================================

def _get_oauth_client() -> gspread.Client:
    """Build an OAuth 2.0 gspread client, refreshing or re-authorising as needed."""
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


# ============================================================
# Google Sheet Client
# ============================================================

class GoogleSheetClient:
    """Thin wrapper around a single gspread Worksheet.

    Supports both OAuth (oauth_credentials.json) and service-account
    (GOOGLE_SERVICE_ACCOUNT_JSON) authentication — OAuth takes priority
    when oauth_credentials.json is present.
    """

    def __init__(self, sheet_number: int = 1) -> None:
        url_key = "GOOGLE_SHEET_URL" if sheet_number == 1 else f"GOOGLE_SHEET_URL_{sheet_number}"
        sheet_url = required_env(url_key)
        worksheet_name = os.getenv("WORKSHEET_NAME", "").strip()

        # Prefer OAuth when the credentials file exists
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

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _build_label_rows(self) -> Dict[str, int]:
        """Index {lowercase_label: row_number} for every non-empty col-A value."""
        labels: Dict[str, int] = {}
        for row_index, row in enumerate(self.values, start=1):
            label = row[0].strip() if row else ""
            if label:
                labels[label.lower()] = row_index
        return labels

    def _cell_value(self, row_number: int, column_number: int) -> str:
        """Return a single cell value (empty string when out of range)."""
        if row_number > len(self.values):
            return ""
        row = self.values[row_number - 1]
        if column_number > len(row):
            return ""
        return row[column_number - 1].strip()

    # ----------------------------------------------------------
    # Public API
    # ----------------------------------------------------------

    def get_pending_records(
        self,
        customer_id_label: str,
        skip_labels: Set[str],
        required_label: str,
        start_column: int,
    ) -> List[SheetRecord]:
        """Return every column that has data but no customer ID yet."""
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

            # A column is "pending" when the required label has a value but
            # the customer-ID cell is still empty.
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
        """Return the row number for `label`, creating the row if it is missing.

        Performs a fuzzy match (strips non-alphanumeric chars) before appending
        a new row so that minor formatting differences don't create duplicates.
        """
        wanted = label.strip().lower()
        if wanted in self.label_rows:
            return self.label_rows[wanted]

        # Fuzzy match: ignore punctuation / extra spaces
        wanted_clean = re.sub(r"[^a-z0-9]", "", wanted)
        for existing_label, row_idx in self.label_rows.items():
            existing_clean = re.sub(r"[^a-z0-9]", "", existing_label)
            if wanted_clean == existing_clean:
                self.label_rows[wanted] = row_idx
                return row_idx

        # Label not found — append a new row to the worksheet
        next_row = len(self.values) + 1
        current_cols = max((len(r) for r in self.values), default=1)
        self.worksheet.resize(rows=next_row, cols=max(current_cols, 1000))
        self.worksheet.update_cell(next_row, 1, label)
        self.values.append([label])
        self.label_rows[wanted] = next_row
        return next_row

    def update_customer_id(self, column_number: int, label: str, customer_id: str) -> None:
        """Write `customer_id` into the cell at (label_row, column_number)."""
        row_number = self.ensure_label_row(label)
        self.worksheet.update_cell(row_number, column_number, customer_id)


# ============================================================
# Website Automation Bot
# ============================================================

class WebsiteBot:
    """Playwright-based browser bot that fills and submits the web application form.

    A single persistent Chromium context is reused across all submissions
    within one run to preserve the login session. The browser profile is
    stored on disk (USER_DATA_DIR) so that the next run resumes the session
    without requiring a manual login.
    """

    def __init__(self, sheet_number: int = 1) -> None:
        # ---------- Config from .env ----------
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
        self.customer_id_selector = os.getenv("CUSTOMER_ID_SELECTOR", "").strip()
        self.customer_id_regex = os.getenv(
            "CUSTOMER_ID_REGEX",
            r"(?:Customer\s*ID\s*#?|Cust\s*#)\s*[:#-]?\s*(\d{10})",
        ).strip()

        # Each sheet instance gets its own browser profile so sessions don't clash
        base_dir = os.getenv("USER_DATA_DIR", "browser_profile").strip()
        self.user_data_dir = base_dir if sheet_number == 1 else f"{base_dir}_{sheet_number}"

        # ---------- Launch persistent Chromium context ----------
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=self.headless,
            channel="chrome",
            viewport={"width": 1366, "height": 700},
            args=["--window-position=0,0", "--window-size=1366,768"],
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_viewport_size({"width": 1366, "height": 700})

        # Auto-dismiss any browser-level alert/confirm/prompt dialogs
        self._page.on("dialog", lambda dialog: dialog.accept())

        # ---------- Wait for login ----------
        print("  Opening website, please login if required...")
        self._page.goto(self.website_url, wait_until="domcontentloaded", timeout=60000)
        self._page.locator(self.start_selector).first.wait_for(
            timeout=self.login_wait_seconds * 1000
        )
        print("  Website ready.")

    def close(self) -> None:
        """Gracefully shut down the Playwright context and process."""
        try:
            self._context.close()
        except Exception:
            pass
        try:
            self._playwright.stop()
        except Exception:
            pass

    # ----------------------------------------------------------
    # Form Submission
    # ----------------------------------------------------------

    def submit_row(self, row: Dict[str, str]) -> str:
        """Fill and submit the web form for one customer record.

        Steps:
            1. Navigate to the website home page.
            2. Click 'Submit App' → wait → click 'DBA Info'.
            3. Fill every field defined in FIELD_MAP.
            4. Capture the app_id from the URL (before submit).
            5. Click 'Submit Application'.
            6. Return the customer ID (from URL or page text).
        """
        prepared_row = self._prepare_row(row)
        page = self._page

        # Step 1 – Navigate
        page.goto(self.website_url, wait_until="domcontentloaded", timeout=60000)
        self._click(page, self.start_selector, "Submit App")
        page.wait_for_timeout(self.after_start_wait_seconds * 1000)
        self._click(page, self.dba_info_selector, "DBA Info")
        page.wait_for_load_state("networkidle", timeout=30000)

        # Zoom out so all fields are reachable without scrolling
        page.evaluate("document.body.style.zoom = '67%'")
        page.wait_for_selector("input[type='text']:visible", timeout=15000)

        # Step 2 – Fill fields
        for column_name, selector in self.field_map.items():
            value = prepared_row.get(column_name, "")
            if value:
                for target in selector.split("&&"):
                    try:
                        self._fill(page, target.strip(), value)
                        print(f"  OK: {column_name} -> {target.strip()}")
                    except Exception as e:
                        print(f"  FAIL: {column_name} -> {target.strip()} | Error: {e}")

        # Step 3 – Capture app_id from URL before submit (most reliable source)
        current_url = page.url
        url_match = re.search(r"app_id=(\d+)", current_url)
        if url_match:
            customer_id = url_match.group(1)
            print(f"  Customer ID from URL (pre-submit): {customer_id}")
        else:
            customer_id = None
            print("  Warning: app_id not found in URL, will search page after submit.")

        # Step 4 – Submit via JavaScript (more reliable than locator click)
        page.evaluate("""
            const el = Array.from(document.querySelectorAll('input[type=button],input[type=submit],button'))
                .find(e => (e.value || e.innerText || '').trim() === 'Submit Application');
            if (el) { el.scrollIntoView(); el.click(); }
            else { throw new Error('Submit Application button not found'); }
        """)

        # Step 5 – Wait for the confirmation page to fully load
        page.wait_for_load_state("domcontentloaded", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)

        # Dismiss any success popup
        page.wait_for_timeout(1000)
        try:
            page.mouse.click(200, 200)
            print("  Popup dismissed via click.")
        except Exception:
            pass

        # Step 6 – Return customer ID (URL value already captured, or search page)
        if customer_id:
            return customer_id

        print("  Searching page for customer ID...")
        for _ in range(15):
            customer_id = self._extract_customer_id(page, prepared_row)
            if customer_id:
                break
            page.wait_for_timeout(2000)

        if not customer_id:
            raise RuntimeError("Customer ID not found.")
        return customer_id

    # ----------------------------------------------------------
    # Row Preparation
    # ----------------------------------------------------------

    def _prepare_row(self, row: Dict[str, str]) -> Dict[str, str]:
        """Apply pre-submission transformations to a row's values.

        Currently: strips 'LLC' from DBA Name and falls back to Legal Name
        when DBA Name is blank.
        """
        prepared = dict(row)
        legal_name = prepared.get("Business Legal Name", "").strip()
        dba_name = prepared.get("Business DBA Name", "").strip() or legal_name
        prepared["Business DBA Name"] = strip_llc(dba_name)
        return prepared

    # ----------------------------------------------------------
    # Click Helper
    # ----------------------------------------------------------

    def _click(self, page, selector: str, name: str, required: bool = True) -> None:
        """Click an element by selector, falling back to a JavaScript click."""
        try:
            locator = page.locator(selector).first
            locator.scroll_into_view_if_needed(timeout=10000)
            locator.click(timeout=20000)
        except Exception:
            try:
                page.evaluate(f"""
                    const el = document.querySelector({repr(selector)}) ||
                        Array.from(document.querySelectorAll('input[type=button],input[type=submit],button,a'))
                        .find(e => e.value === 'Submit Application' || e.innerText.trim() === 'Submit Application');
                    if (el) {{ el.scrollIntoView(); el.click(); }}
                """)
            except Exception:
                if required:
                    raise RuntimeError(f"{name} could not be clicked. Check selector: {selector!r}")

    # ----------------------------------------------------------
    # Fill Helper  (handles label=, select-label=, select=,
    #               section-select=, section-label=, raw CSS)
    # ----------------------------------------------------------

    def _fill(self, page, target: str, value: str) -> None:
        """Fill a single form field identified by `target`.

        Supported target prefixes:
            label=<text>[|<n>]            — fill input by label text
            select-label=<text>[|<n>]     — select option by label text
            select=<css>[|<n>]            — select option by CSS selector
            section-select=<heading>|<field>  — select inside a named section
            section-label=<heading>|<field>   — fill input inside a named section
            <css>                         — raw Playwright locator
        """

        # ---- label= -----------------------------------------------
        if target.startswith("label="):
            label, index = self._split_target(target.removeprefix("label="))
            try:
                page.get_by_label(label, exact=True).nth(index).fill(value, timeout=3000)
                return
            except Exception:
                pass
            locator = page.locator(
                f"label:has-text('{label}') ~ input, label:has-text('{label}') + input"
            ).nth(index)
            try:
                locator.fill(value, timeout=3000)
                return
            except Exception:
                pass
            # Final fallback via JavaScript
            page.evaluate(f"""
                const labels = Array.from(document.querySelectorAll('label'));
                const lbl = labels.filter(l => l.innerText.trim() === {repr(label)})[{index}];
                if (lbl) {{
                    const input = lbl.querySelector('input') || document.getElementById(lbl.htmlFor);
                    if (input) {{
                        input.value = {repr(value)};
                        input.dispatchEvent(new Event('input', {{bubbles:true}}));
                        input.dispatchEvent(new Event('change', {{bubbles:true}}));
                    }}
                }}
            """)
            return

        # ---- select-label= ----------------------------------------
        if target.startswith("select-label="):
            label, index = self._split_target(target.removeprefix("select-label="))
            try:
                page.get_by_label(label, exact=True).nth(index).select_option(label=value, timeout=3000)
                return
            except Exception:
                pass
            try:
                page.get_by_label(label, exact=True).nth(index).select_option(value=value, timeout=3000)
                return
            except Exception:
                pass
            # Robust JS fallback — resolves the <select> by label, then picks the closest option
            result = page.evaluate(f"""
                () => {{
                    const labelText = {repr(label)};
                    const labelIdx  = {index};
                    const val       = {repr(value)};

                    const matched = Array.from(document.querySelectorAll('label'))
                        .filter(l => l.textContent.trim() === labelText);
                    const lbl = matched[labelIdx];

                    let sel = null;
                    if (lbl) {{
                        if (lbl.htmlFor) sel = document.getElementById(lbl.htmlFor);
                        if (!sel) sel = lbl.querySelector('select');
                        if (!sel) {{
                            let sib = lbl.nextElementSibling;
                            while (sib) {{
                                if (sib.tagName === 'SELECT') {{ sel = sib; break; }}
                                if (sib.querySelector) {{
                                    const s = sib.querySelector('select');
                                    if (s) {{ sel = s; break; }}
                                }}
                                sib = sib.nextElementSibling;
                            }}
                        }}
                    }}

                    // Last resort: pick select by index position on page
                    if (!sel) {{
                        const allSels = Array.from(document.querySelectorAll('select'));
                        sel = allSels[labelIdx] || null;
                    }}

                    if (!sel) return 'NO_SELECT';

                    const opts = Array.from(sel.options);
                    let opt = opts.find(o => o.text.trim() === val)
                           || opts.find(o => o.text.trim().toLowerCase() === val.toLowerCase())
                           || opts.find(o => o.text.trim().toLowerCase().includes(val.toLowerCase()))
                           || opts.find(o => o.value.trim().toLowerCase() === val.toLowerCase());
                    if (!opt) return 'NO_OPTION:' + opts.map(o => o.text).join('|');

                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return 'OK:' + opt.text;
                }}
            """)
            print(f"  select-JS result for '{label}': {result}")
            return

        # ---- select= ----------------------------------------------
        if target.startswith("select="):
            selector, index = self._split_target(target.removeprefix("select="))
            page.locator(selector).nth(index).select_option(label=value, timeout=15000)
            return

        # ---- section-select= --------------------------------------
        # Format: section-select=Section Heading|Field Label
        # Locates a <select> inside a named section block.
        if target.startswith("section-select="):
            rest = target.removeprefix("section-select=")
            section_heading, field_label = rest.split("|", 1)
            result = page.evaluate(f"""
                () => {{
                    const heading    = {repr(section_heading.strip())};
                    const fieldLabel = {repr(field_label.strip())};
                    const val        = {repr(value)};

                    const headings = Array.from(
                        document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,td,th,span,legend')
                    ).filter(el => el.textContent.trim() === heading);
                    if (!headings.length) return 'NO_HEADING:' + heading;

                    for (const hd of headings) {{
                        let container =
                            hd.closest('fieldset,section,div.panel,div.row,div.form-group,tbody,table')
                            || hd.parentElement;

                        for (let i = 0; i < 5 && container; i++) {{
                            const labels = Array.from(container.querySelectorAll('label'))
                                .filter(l => l.textContent.trim() === fieldLabel);
                            if (labels.length) {{
                                const lbl = labels[0];
                                let sel = null;
                                if (lbl.htmlFor) sel = document.getElementById(lbl.htmlFor);
                                if (!sel) sel = lbl.querySelector('select');
                                if (!sel) {{
                                    let sib = lbl.nextElementSibling;
                                    while (sib) {{
                                        if (sib.tagName === 'SELECT') {{ sel = sib; break; }}
                                        const s = sib.querySelector('select');
                                        if (s) {{ sel = s; break; }}
                                        sib = sib.nextElementSibling;
                                    }}
                                }}
                                if (!sel) {{
                                    let p = lbl.parentElement;
                                    for (let j = 0; j < 3 && p; j++) {{
                                        const s = p.querySelector('select');
                                        if (s) {{ sel = s; break; }}
                                        p = p.parentElement;
                                    }}
                                }}
                                if (sel) {{
                                    const opts = Array.from(sel.options);
                                    let opt = opts.find(o => o.text.trim() === val)
                                           || opts.find(o => o.text.trim().toLowerCase() === val.toLowerCase())
                                           || opts.find(o => o.text.trim().toLowerCase().includes(val.toLowerCase()));
                                    if (!opt) return 'NO_OPTION:' + opts.map(o => o.text).join('|');
                                    sel.value = opt.value;
                                    sel.dispatchEvent(new Event('change', {{bubbles:true}}));
                                    return 'OK:' + opt.text;
                                }}
                            }}
                            container = container.parentElement;
                        }}
                    }}
                    return 'NO_SELECT';
                }}
            """)
            print(f"  section-select result for '{section_heading}|{field_label}': {result}")
            return

        # ---- section-label= ---------------------------------------
        # Format: section-label=Section Heading|Field Label
        # Locates an <input> inside a named section block.
        if target.startswith("section-label="):
            rest = target.removeprefix("section-label=")
            section_heading, field_label = rest.split("|", 1)
            result = page.evaluate(f"""
                () => {{
                    const heading    = {repr(section_heading.strip())};
                    const fieldLabel = {repr(field_label.strip())};
                    const val        = {repr(value)};

                    const headings = Array.from(
                        document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,td,th,span,legend')
                    ).filter(el => el.textContent.trim() === heading);
                    if (!headings.length) return 'NO_HEADING:' + heading;

                    for (const hd of headings) {{
                        let container =
                            hd.closest('fieldset,section,div.panel,div.row,div.form-group,tbody,table')
                            || hd.parentElement;

                        for (let i = 0; i < 5 && container; i++) {{
                            const labels = Array.from(container.querySelectorAll('label'))
                                .filter(l => l.textContent.trim() === fieldLabel);
                            if (labels.length) {{
                                const lbl = labels[0];
                                let inp = null;
                                if (lbl.htmlFor) inp = document.getElementById(lbl.htmlFor);
                                if (!inp) inp = lbl.querySelector('input');
                                if (!inp) {{
                                    let sib = lbl.nextElementSibling;
                                    while (sib) {{
                                        if (sib.tagName === 'INPUT') {{ inp = sib; break; }}
                                        const s = sib.querySelector('input');
                                        if (s) {{ inp = s; break; }}
                                        sib = sib.nextElementSibling;
                                    }}
                                }}
                                if (inp) {{
                                    inp.value = val;
                                    inp.dispatchEvent(new Event('input', {{bubbles:true}}));
                                    inp.dispatchEvent(new Event('change', {{bubbles:true}}));
                                    return 'OK';
                                }}
                            }}
                            container = container.parentElement;
                        }}
                    }}
                    return 'NO_INPUT';
                }}
            """)
            print(f"  section-label result for '{section_heading}|{field_label}': {result}")
            return

        # ---- Raw CSS / Playwright selector ------------------------
        page.locator(target).fill(value, timeout=15000)

    # ----------------------------------------------------------
    # Target Parser
    # ----------------------------------------------------------

    def _split_target(self, raw: str) -> tuple[str, int]:
        """Split 'Field Label|2' into ('Field Label', 1) (0-based index).

        Returns index 0 when no pipe suffix is present.
        """
        text = raw.strip()
        if "|" not in text:
            return text, 0
        label, index_text = text.rsplit("|", 1)
        return label.strip(), max(int(index_text.strip()) - 1, 0)

    # ----------------------------------------------------------
    # Customer ID Extraction
    # ----------------------------------------------------------

    def _extract_customer_id(self, page, row: Dict[str, str]) -> Optional[str]:
        """Try to read the customer ID from a specific selector or the full page text."""
        if self.customer_id_selector:
            try:
                text = page.locator(self.customer_id_selector).first.inner_text(timeout=5000)
                customer_id = self._match_customer_id(text, row)
                if customer_id:
                    return customer_id
            except Exception:
                pass

        try:
            page_text = page.evaluate("document.body.innerText")
        except Exception:
            return None
        return self._match_customer_id(page_text, row)

    def _match_customer_id(self, text: str, row: Dict[str, str]) -> Optional[str]:
        """Search `text` for a customer ID that belongs to the current business.

        Strategy:
            1. Find the business name in the page text.
            2. Look for a 'Cust #' / 'Customer ID' pattern within ±500 chars.
            3. Fallback: if exactly one such pattern exists on the whole page, use it.
        """
        legal_name = row.get("Business Legal Name", "").strip()
        dba_name = row.get("Business DBA Name", "").strip()
        if not legal_name:
            return None

        id_pattern = re.compile(self.customer_id_regex, re.IGNORECASE)
        name_variants = [legal_name]
        if dba_name and dba_name.lower() != legal_name.lower():
            name_variants.append(dba_name)

        for name in name_variants:
            norm_name = normalize_match_text(name)
            norm_text = normalize_match_text(text)
            pos = norm_text.find(norm_name)
            if pos == -1:
                continue
            window_start = max(0, pos - 500)
            window_end = min(len(text), pos + len(name) + 500)
            window = text[window_start:window_end]
            match = id_pattern.search(window)
            if match:
                return match.group(1).strip()

        # If there is only one customer ID on the page it must be ours
        all_matches = id_pattern.findall(text)
        if len(all_matches) == 1:
            return all_matches[0].strip()

        return None


# ============================================================
# Main Processing Loop
# ============================================================

def process_pending(
    sheet: GoogleSheetClient,
    website: Optional[WebsiteBot],
    customer_id_column: str,
    skip_labels: Set[str],
    required_label: str,
    start_column: int,
    pd_label: str,
) -> int:
    """Process all pending columns from the sheet for one polling cycle.

    For each pending column:
        - Skip if data was removed between the scan and now.
        - Skip if customer ID appeared between the scan and now (another run).
        - Skip if P/D value is not 'Proceed' (write 'Declined' if declined).
        - Skip if Owner 1 Name is missing (writes a note to the sheet).
        - Submit via WebsiteBot and write the returned customer ID back.

    Returns the number of columns actually processed (success + skipped-with-write).
    """
    pending_records = sheet.get_pending_records(
        customer_id_column,
        skip_labels,
        required_label,
        start_column,
    )
    if not pending_records:
        return 0

    print(f"{len(pending_records)} pending customer column(s) found.")
    processed_count = 0

    for record in pending_records:
        print(f"Processing column {record.column_number}: {record.column_name}")

        # Re-read column data fresh to avoid acting on a stale snapshot
        try:
            fresh_sheet = GoogleSheetClient.__new__(GoogleSheetClient)
            fresh_sheet.worksheet = sheet.worksheet
            fresh_sheet.values = sheet.worksheet.get_all_values()
            fresh_sheet.label_rows = fresh_sheet._build_label_rows()
        except Exception:
            fresh_sheet = sheet

        fresh_data: Dict[str, str] = {}
        for row in fresh_sheet.values:
            if not row:
                continue
            label = row[0].strip()
            if not label or label.lower() in skip_labels:
                continue
            value = row[record.column_number - 1].strip() if len(row) >= record.column_number else ""
            fresh_data[label] = value

        # Guard: required field must still be present
        required_labels = [r.strip() for r in required_label.split(",") if r.strip()]
        required_value = next(
            (fresh_data.get(r, "").strip() for r in required_labels if fresh_data.get(r, "").strip()), ""
        )
        if not required_value:
            print(f"  Column {record.column_number} skipped — data removed from sheet.")
            continue

        # Guard: customer ID must still be empty
        customer_id_row = fresh_sheet.ensure_label_row(customer_id_column)
        existing_id = fresh_sheet._cell_value(customer_id_row, record.column_number)
        if existing_id:
            print(f"  Column {record.column_number} skipped — customer ID already exists: {existing_id}")
            continue

        # Guard: P/D flag
        pd_value = fresh_data.get(pd_label, "").strip()
        if pd_value.lower().startswith("declined"):
            print(f"  Column {record.column_number} declined — writing to sheet: {pd_value}")
            sheet.update_customer_id(record.column_number, customer_id_column, pd_value)
            processed_count += 1
            continue

        if pd_value and pd_value.lower() != "proceed":
            print(f"  Column {record.column_number} skipped — P/D value is '{pd_value}', not 'Proceed'.")
            continue

        # Guard: owner name is required by the web form
        owner_name = fresh_data.get("Owner 1 Name", "").strip()
        if not owner_name:
            print(f"  Column {record.column_number} — owner name is missing, writing to sheet.")
            sheet.update_customer_id(record.column_number, customer_id_column, "owner name is missing")
            processed_count += 1
            continue

        # Submit or dry-run
        try:
            if website is None:
                customer_id = f"DRY-RUN-{int(time.time())}-{record.column_number}"
                print("WEBSITE_ENABLED=false. Dry-run customer ID generated.")
            else:
                customer_id = website.submit_row(fresh_data)

            sheet.update_customer_id(record.column_number, customer_id_column, customer_id)
            print(f"Column {record.column_number} updated: {customer_id}")
            processed_count += 1
        except Exception as exc:
            print(f"Column {record.column_number} failed: {exc}")

    return processed_count


# ============================================================
# Entry Point
# ============================================================

def run() -> int:
    """Bootstrap the bot: load config, open browser, poll the sheet.

    Command-line arguments:
        --sheet <1|2|3>   Which sheet / browser profile to use (default: 1)

    Environment variables (all read from .env):
        See .env.example for the full list.
    """
    load_dotenv()

    # Parse --sheet flag
    sheet_number = 1
    args = sys.argv[1:]
    if "--sheet" in args:
        idx = args.index("--sheet")
        if idx + 1 < len(args):
            sheet_number = int(args[idx + 1])

    google_enabled = env_bool("GOOGLE_SHEETS_ENABLED", True)
    website_enabled = env_bool("WEBSITE_ENABLED", True)
    customer_id_column = os.getenv("CUSTOMER_ID_COLUMN", "customer id").strip()
    skip_labels = parse_label_list(os.getenv("SKIP_LABELS", ""))
    required_label = os.getenv("REQUIRED_LABEL", "Business Legal Name").strip()
    pd_label = os.getenv("PD_LABEL", "P/D").strip()
    start_column = ask_start_column(os.getenv("START_COLUMN", "B"))
    poll_seconds = int(os.getenv("POLL_SECONDS", "5"))

    if not google_enabled:
        print("GOOGLE_SHEETS_ENABLED=false. Bot will not read/write sheet.")
        return 0

    website = WebsiteBot(sheet_number) if website_enabled else None
    print(f"Bot {sheet_number} is running — checking sheet every {poll_seconds}s. Press Ctrl+C to stop.")

    last_activity_time = time.time()
    KEEPALIVE_SECONDS = 5 * 60  # Reload home page every 5 min when idle to prevent session timeout

    try:
        while True:
            try:
                sheet = GoogleSheetClient(sheet_number)
                processed = process_pending(
                    sheet, website, customer_id_column, skip_labels,
                    required_label, start_column, pd_label,
                )
                if processed == 0:
                    print(f"No pending records. Checking again in {poll_seconds}s...", end="\r")
                    # Keepalive ping — prevents the website session from timing out
                    if website and (time.time() - last_activity_time) >= KEEPALIVE_SECONDS:
                        try:
                            website._page.goto(
                                website.website_url,
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                            print("\n  Session keepalive — page refreshed.")
                        except Exception:
                            pass
                        last_activity_time = time.time()
                else:
                    last_activity_time = time.time()
            except Exception as exc:
                print(f"Error: {exc}")
            time.sleep(poll_seconds)
    finally:
        if website:
            website.close()


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        print("\nBot stopped manually.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)
