## StudentAid servicer → Monarch student loan sync (automation)

This project automates:
- **Daily balance updates** for each loan group (AA/AB/…)
- **Payment-posted transactions** in Monarch (one per loan group allocation), categorized as **Transfer**

Target: run **unattended** in Docker/Unraid, with **email MFA** handled via **Gmail IMAP + App Password**.

### How it works (high level)
- Logs into your servicer portal (typically `https://{provider}.studentaid.gov`) with Playwright
- When the portal prompts for MFA, selects **Email**, then polls Gmail IMAP for the code
- Scrapes per-loan balances + payment allocation details
- Pushes updates into Monarch via the unofficial Monarch API client
- Stores a small SQLite state DB so runs are **idempotent** (no duplicate payment transactions)
- Includes an extra **duplicate guard** against Monarch itself: **date + amount + merchant** (so even if you reset SQLite, we won't spam duplicates)

### Prereqs
- **Python 3.11+** recommended
- A **Gmail account** that receives MFA emails
  - Enable Google 2‑Step Verification
  - Create an **App Password**
  - Ensure IMAP access is enabled for the mailbox
- Monarch auth:
  - If you log in with email/password: set `MONARCH_EMAIL` + `MONARCH_PASSWORD`
  - If you use **Sign in with Apple**: set `MONARCH_TOKEN` (preferred) and leave password blank

### Gmail IMAP setup (App Password + label/filter) — recommended
This makes MFA automation reliable and keeps old/stale codes out of your inbox.

- **Enable IMAP in Gmail**
  - Gmail → Settings (gear) → **See all settings** → **Forwarding and POP/IMAP** → **Enable IMAP** → Save.

- **Enable 2‑Step Verification**
  - Google Account → **Security** → **2‑Step Verification** → turn it on.

- **Create a Google App Password**
  - Google Account → **Security** → **App passwords**
  - Choose **Mail** (or “Other”) and generate an app password.
  - Put the generated 16‑character password into `.env` as `GMAIL_IMAP_APP_PASSWORD`.
  - If you don’t see “App passwords”, it usually means 2‑step isn’t enabled yet or your account/admin policy forbids it.

- **Create a Gmail label/folder (optional but strongly recommended)**
  - Gmail sidebar → **Labels** → **Create new label**
  - Example: `StudentAid MFA`
  - Set `.env` `GMAIL_IMAP_FOLDER` to that label name (Gmail exposes labels as IMAP folders).
    - Nested labels may look like `StudentAid/MFA`.

- **Create a Gmail filter to auto-label MFA emails**
  - Gmail search bar → **Show search options** → create filter.
  - Suggested filter (broad, works across servicers):
    - **From**: `studentaid.gov`
    - **Subject**: `code`
  - Actions:
    - **Apply the label**: `StudentAid MFA`
    - (Optional) **Never send it to Spam**

- **Recommended `.env` values**
  - **`GMAIL_IMAP_SENDER_HINT`**: `studentaid.gov` (or a specific one like `cri.studentaid.gov`)
  - **`GMAIL_IMAP_SUBJECT_HINT`**: `code` (broad; subjects vary by servicer)
  - Example sender we’ve seen: `CRINoReply@cri.studentaid.gov`
    - Other servicers are likely similar (`<something>@<provider>.studentaid.gov`), but we don’t rely on that—use the hints above.

- **Verify**
  - Run preflight (no Playwright):

```bash
.venv\Scripts\python -m studentaid_monarch_sync preflight --config config.yaml
```

### Setup (Windows dev)
1. Create a venv and install deps:

```bash
cd "%USERPROFILE%\Documents\Monarch_Loan_Script"
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install -e .
```

2. Install Playwright browser:

```bash
.venv\Scripts\python -m playwright install chromium
```

3. Create `.env` and config:
- Copy `env.example` → `.env` and fill values
- Start from `config.example.yaml` → `config.yaml`, then:
  - Set `servicer.provider` (e.g. `cri`, `nelnet`, `mohela`)
  - Adjust your loan group → Monarch account mappings

Tip: run `studentaid_monarch_sync list-servicers` to see common provider slugs (non-exhaustive).

#### Getting `MONARCH_TOKEN` (for Sign in with Apple)
1. Log into Monarch in your browser normally (using Apple).
2. Open DevTools → **Network** tab.
3. Click any request to Monarch’s API (often `graphql`).
4. In **Request Headers**, copy the value after `Authorization: Token ...`.
5. Put that into `.env` as `MONARCH_TOKEN=...` (keep it secret).

4. Run a dry-run sync:

```bash
.venv\Scripts\python -m studentaid_monarch_sync sync --config config.yaml --dry-run --headful
```

### Preflight checks (recommended before unattended runs)
Validate external dependencies **before** opening Playwright:

```bash
.venv\Scripts\python -m studentaid_monarch_sync preflight --config config.yaml
```

### Debug bundle (auto-created on failures)
If a `sync` run fails, the CLI will automatically create a zip under `data/` containing:
- `data/debug/*` (screenshots/HTML)
- your configured log file (default: `data/sync.log`)

You can attach that zip when asking for help.

### Dry-run that ALSO checks Monarch (recommended)
Standard dry-run **does not** call Monarch (it only prints what it would do based on the portal + local SQLite).
If you want to validate the real behavior end-to-end (including the duplicate guard), run:

```bash
.venv\Scripts\python -m studentaid_monarch_sync sync --config config.yaml --dry-run --dry-run-check-monarch --headful --payments-since 2025-01-01 --max-payments 1
```

This logs into Monarch in **read-only** mode and prints each payment allocation as:
- `SKIP (duplicate)` if Monarch already has a txn with the same **date + amount + merchant**
- `CREATE` if it would create a new txn

### Run for real (writes to Monarch)
Once you're happy with the dry-run output, run the same command **without** `--dry-run`:

```bash
.venv\Scripts\python -m studentaid_monarch_sync sync --config config.yaml --payments-since 2025-01-01 --max-payments 1
```

For the first real run, using `--headful` is a good idea so you can watch login/MFA:

```bash
.venv\Scripts\python -m studentaid_monarch_sync sync --config config.yaml --headful --payments-since 2025-01-01 --max-payments 1
```

### Run (Docker / Unraid)
This project is designed to run as a **run-once container** on a schedule (daily is typical).

1. Create config + env:
- Copy `env.example` → `.env`
- Create an empty `data/` folder (holds SQLite + session cookies + logs)

2. Run with docker compose:

```bash
cd /path/to/Monarch_Loan_Script
docker compose up --build --abort-on-container-exit
```

Or run one-shot directly:

```bash
docker compose run --rm studentaid-monarch-sync sync --config /app/config.yaml
```

3. On Unraid:
- Create a daily scheduled job (e.g., User Scripts plugin) that runs the one-shot command above.
- Keep `./data` on persistent storage so Monarch sessions and the SQLite idempotency DB survive restarts.

### Notes / stability
- StudentAid servicer portals are web portals; UI changes may break selectors. The code is structured so selectors live in one place, and failures should save screenshots/logs for debugging.
- Email MFA automation is sensitive—use a dedicated mailbox if possible.
- State self-heal:
  - `data/state.db` (SQLite idempotency DB) is backed up to `data/state.db.bak`. If `state.db` is corrupted/unreadable, the script will restore from the backup (or recreate it if no backup exists).
  - `data/servicer_storage_state_*.json` (Playwright cookies/localStorage) is backed up to `*.bak`. If it becomes invalid JSON, the script quarantines it and falls back to a fresh session.
  - Even if SQLite is lost, the Monarch-side duplicate guard (**date + amount + merchant**) prevents duplicate payment transactions.

### Troubleshooting (common friction)
- **Monarch preflight failed / token expired**
  - Re-login / refresh `MONARCH_TOKEN`.
  - If a stale session is stuck, delete `data/monarch_session.pickle` and retry preflight.
- **MFA email not found**
  - Confirm Gmail IMAP is enabled and `GMAIL_IMAP_FOLDER` matches the label name.
  - Set broad hints: `GMAIL_IMAP_SENDER_HINT=studentaid.gov` and `GMAIL_IMAP_SUBJECT_HINT=code`.
  - Make sure the filter applies the label and the email isn’t in Spam.

### Config notes (Monarch payments)
- `monarch.payment_merchant_name`: merchant name to use when creating payment transactions, and the value used by the **duplicate guard**.
  - If your existing loan-account payments show up as **US Department of Education**, set this to that (recommended).

### CLI flags (reference)
Run `-h` at any time to see the full help:

```bash
.venv\Scripts\python -m studentaid_monarch_sync -h
.venv\Scripts\python -m studentaid_monarch_sync sync -h
```

#### Global flags
- `--env-file`: Path to a dotenv file (default: `.env`). If present, it will be loaded before reading config/env vars.

#### `sync` flags
- `--config`: Path to YAML config (default: `config.yaml`).
- `--dry-run`: Do not write to Monarch. Prints intended balance updates + intended payment transactions.
- `--dry-run-check-monarch`: Only meaningful with `--dry-run`. Logs into Monarch **read-only** and prints `SKIP (duplicate)` vs `CREATE` using the duplicate guard (**date + amount + merchant**).
- `--headful`: Run Playwright with a visible browser window (useful for debugging / monitoring).
- `--fresh-session`: Do not reuse the stored browser session (cookies/localStorage). Useful if the portal gets into weird redirects (e.g. `dark.<servicer>.studentaid.gov`).
- `--manual-mfa`: In headful mode, pause and let you enter the MFA code manually in the browser (safer while debugging).
  - Requires `--headful`.
- `--print-mfa-code`: Print the full MFA code to stdout (debug).
  - Requires `--headful`. Avoid using this in unattended/logged environments (e.g., containers) since stdout is typically collected.
- `--slowmo-ms`: Playwright “slow motion” delay (ms) applied to browser actions (debug).
- `--step-debug`: Save step-by-step screenshots under `data/debug/` to tighten selectors and understand failures.
- `--step-delay-ms`: Extra delay (ms) after each step screenshot in step-debug mode (so you can watch the browser).
- `--max-payments`: Max payment detail entries to scan from Payment Activity (default: 10).
- `--payments-since`: Only consider payments on/after this date (`YYYY-MM-DD`). Also used to stop scanning older payment history early (faster runs).

#### `list-monarch-accounts` flags
- `--config`: Path to YAML config (default: `config.yaml`).


