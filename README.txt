# Google Sheet Website Bot

Setup:

1. Python must be installed.
2. Run:
   python -m pip install -r requirements.txt
   python -m playwright install chromium
3. Download the service account JSON from Google Cloud and place it in the project folder with the name:
   service_account.json
4. Share the Google Sheet with the service account email address.
5. Copy `.env.example` to `.env` and update the required values.
6. Double-click `Launcher.bat` to start the bot.

Sheet Rules:

* Labels should be placed in Column A.
* Customer data should be stored in Columns B, C, D, and so on.
* When the bot starts, it will ask for the starting column. You can enter `B`, `C`, `D`, or a column number.
* If `ASK_START_COLUMN=false`, the bot will start directly from the value defined in `START_COLUMN`.
* Add the orange labels to `SKIP_LABELS`. These labels will not be processed on the website.
* `CUSTOMER_ID_COLUMN` is set to `"customer id"` by default.
* The bot will process the first customer column where the Customer ID field is blank.
* In `FIELD_MAP`, the left side represents the Google Sheet label name and the right side represents the website field selector.

Example FIELD_MAP:
Google Sheet Label Name := Website Field Name

Mapping Tips:

* If one sheet label needs to fill multiple website fields, use `&&`.
* If the same label appears multiple times on the page, use `|2` for the second occurrence, for example: `label=City|2`.

Website Flow:

* The first page should open after login.
* If the login page appears, log in manually through the browser. The bot will wait for the "Submit App" button for the duration specified in `LOGIN_WAIT_SECONDS`.
* Browser session data will be saved in `USER_DATA_DIR`, so login is usually retained for future runs.
* The bot will click "Submit App".
* It will wait for 10 seconds.
* It will click the "DBA Info" tab.
* It will fill fields according to the `FIELD_MAP`.
* It will click "Submit Application".
* It will click the logo to return to the home page.

True/False Switches:

* If `GOOGLE_SHEETS_ENABLED=false`, Google Sheet reading and writing will be disabled.
* If `WEBSITE_ENABLED=false`, website automation will be disabled and the bot will behave like a dry-run mode.

Important:
Exact field selectors vary from one website to another. You must inspect the website elements and update the `FIELD_MAP`, `SUBMIT_SELECTOR`, and regular expressions (`REGEX`) accordingly.
