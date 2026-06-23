# CR Bot — Google Sheet → Website Form Automation

A Python bot that reads pending customer records from a **Google Sheet** and automatically fills and submits them to a web application form using **Playwright**. Once a submission succeeds, the returned customer ID is written back to the sheet.

---

## Features

- Reads customer data column-by-column from any Google Sheet layout
- Fills text inputs, dropdowns, and section-scoped fields on any web form
- Captures customer IDs from the URL or confirmation page text
- Writes results back to the sheet in real time
- Supports up to 3 independent bots with separate browser sessions
- Survives session timeouts via automatic keepalive page reloads
- Dry-run mode (`WEBSITE_ENABLED=false`) for safe testing

---

## Project Structure

```
cr-bot/
├── bot.py               # Core automation engine
├── .env.example         # Configuration template (copy → .env)
├── requirements.txt     # Python dependencies
├── launch_bot1.bat      # Start Bot 1 (sheet 1)
├── launch_bot2.bat      # Start Bot 2 (sheet 2)
├── launch_bot3.bat      # Start Bot 3 (sheet 3)
├── launch_all.bat       # Start all 3 bots in separate windows
└── .gitignore
```

---

## Quick Start

### 1. Prerequisites

- Python 3.10+ installed and on your PATH (`py --version`)
- A Google Cloud project with the Sheets + Drive APIs enabled
- Either a **service account JSON** key or an **OAuth 2.0 client ID** (Desktop app)

### 2. Clone and configure

```bash
git clone https://github.com/your-username/cr-bot.git
cd cr-bot
copy .env.example .env
```

Open `.env` in a text editor and fill in the required values (see [Configuration](#configuration)).

### 3. Add credentials

**Option A — Service account (recommended for unattended use)**
1. Download the service account JSON from Google Cloud Console.
2. Place it in the project folder and set `GOOGLE_SERVICE_ACCOUNT_JSON=service_account.json` in `.env`.
3. Share your Google Sheet with the service account email (`edit` access).

**Option B — OAuth (personal account)**
1. Download the OAuth client secret JSON from Google Cloud Console.
2. Rename it to `oauth_credentials.json` and place it in the project folder.
3. On first run, a browser window opens for you to authorise access. The token is saved automatically.

### 4. Run

```
Double-click launch_bot1.bat
```

Or from a terminal:

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
python bot.py --sheet 1
```

---

## Configuration

All settings live in `.env`. The table below covers the most important keys. See `.env.example` for the full list with descriptions.

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_SHEET_URL` | *(required)* | Full Google Sheets URL for Bot 1 |
| `GOOGLE_SHEET_URL_2` | — | URL for Bot 2 |
| `GOOGLE_SHEET_URL_3` | — | URL for Bot 3 |
| `WEBSITE_URL` | *(required)* | URL of the web form to submit to |
| `FIELD_MAP` | *(required)* | Sheet label → form selector mappings |
| `CUSTOMER_ID_COLUMN` | `customer id` | Row label where IDs are written back |
| `REQUIRED_LABEL` | `Business Legal Name` | Column must have this row filled to be processed |
| `PD_LABEL` | `P/D` | Proceed / Decline flag row label |
| `SKIP_LABELS` | — | Comma-separated row labels to exclude from form submission |
| `START_COLUMN` | `B` | First customer column letter |
| `HEADLESS` | `false` | Run browser without a visible window |
| `POLL_SECONDS` | `5` | Sheet polling interval |
| `WEBSITE_ENABLED` | `true` | Set `false` for dry-run mode |

---

## FIELD_MAP Reference

The `FIELD_MAP` variable maps Google Sheet row labels to web form field selectors.

**Format:**
```
Sheet Label:selector, Another Label:selector2
```

**Selector prefixes:**

| Prefix | Targets |
|---|---|
| `label=Business Name` | `<input>` associated with a `<label>` by text |
| `label=City\|2` | Second occurrence of that label |
| `select-label=State` | `<select>` associated with a `<label>` by text |
| `select=#state-dropdown` | `<select>` by CSS selector |
| `section-select=Ownership\|Entity Type` | `<select>` inside a named section |
| `section-label=Owner Info\|SSN` | `<input>` inside a named section |
| `#my-input` | Any raw CSS / Playwright selector |

Use `&&` to fill the same value into multiple fields:
```
Business Phone:label=Location Phone #&&label=Billing Phone #
```

---

## Sheet Layout

```
Column A  │  Column B      │  Column C      │ …
──────────┼────────────────┼────────────────┼──
customer id│               │ CUST123        │
Business Legal Name │ Acme Inc. │ Beta LLC │
Business Phone │ 555-0100 │ 555-0200      │
P/D       │               │ Proceed        │
Owner 1 Name │            │ Jane Doe       │
```

- **Column A** holds row labels.
- **Columns B onward** hold one customer per column.
- Bot fills in the `customer id` row after a successful submission.

---

## Running Multiple Bots

Each `--sheet` instance uses its own browser profile directory (`browser_profile`, `browser_profile_2`, `browser_profile_3`) so login sessions are kept separate. Use `launch_all.bat` to start all three at once.

---

## License

MIT
