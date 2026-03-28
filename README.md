## Monarch Student Loan Sync 🧾✨

This project automates (so you don’t have to click through portals every day):
- **Daily balance updates** for each loan group (AA/AB/1-01/…)
- **Payment-posted transactions** in Monarch (one per loan group allocation), categorized as **Transfer**

It’s designed to run **unattended** (Docker/Unraid), with **email MFA** handled via **Gmail IMAP + App Password**. 🤖📬

Shoutout to the maintainers of the unofficial **Monarch Money Python library** — their work made the Monarch-side integration possible. 🙌

### Quick start (most users) 🚀
This guide is linear: **Prereqs → Configure → Choose a runtime (Python or Docker) → Preflight → Dry-run → Run**.

### What happens on a run? (end-to-end) 🧠➡️🏦
At a high level, each scheduled run:
- Logs into your StudentAid servicer portal with Playwright 🔐
- Handles MFA via email (Gmail IMAP) when prompted 📬
- Scrapes current **balances** per loan group (AA/AB/1-01/…) and recent **payment allocations**
- Updates Monarch:
  - **Balances**: updates your mapped manual accounts
  - **Payments**: creates transactions (one per allocation) ✅
- Records a small local history DB (`data/state.db`) so it won’t duplicate payments across runs 🧾

#### Prereqs (set these up once) ✅
- **Monarch auth** (choose one):
  - **Email/password**: set `MONARCH_EMAIL` + `MONARCH_PASSWORD`
    - If you currently use **Google** / “**Continue with Google**” to access Monarch, you’ll need to **set a password** to use the API client (Monarch → **Settings → Security**).
  - **Sign in with Apple / Google (token-based)**: set `MONARCH_TOKEN` (recommended for SSO). If you need help extracting it, see [Getting `MONARCH_TOKEN`](#monarch-token).
- **Gmail IMAP for MFA**
  - You’ll need **IMAP enabled** + **2‑Step Verification** + a **Google App Password**.
  - If you want the step-by-step Gmail label/filter setup (recommended), see [Gmail IMAP setup](#gmail-imap-setup) 🏷️

#### Configure 🛠️
Copy `env.example` → `.env` and fill in values (Monarch + Gmail IMAP + your loan groups).

✅ For most users, **`.env` is the only file you need to edit**.

Required:
- `SERVICER_PROVIDER`, `SERVICER_USERNAME`, `SERVICER_PASSWORD`
- `LOAN_GROUPS` (comma-separated: `AA,AB,...` or `1-01,1-02,...`)
- Monarch auth (`MONARCH_TOKEN` or `MONARCH_EMAIL` + `MONARCH_PASSWORD`)
- Gmail IMAP (`GMAIL_IMAP_USER` + `GMAIL_IMAP_APP_PASSWORD`)

Advanced (optional):
- `config.example.yaml` is an advanced override file. You can pass `--config config.example.yaml`, but most users don’t need YAML at all.

Tip: If you’re not sure what to put in `LOAN_GROUPS`, you can have the tool log into your servicer portal and list what it discovers:

```bash
docker compose run --rm --build studentaid-monarch-sync list-loan-groups
```

or (Python):

```bash
.venv/bin/python -m studentaid_monarch_sync list-loan-groups --headful
```

Tip: list common provider slugs (non-exhaustive):

```bash
docker compose run --rm --build studentaid-monarch-sync list-servicers
```

#### One-time setup (required): map your loan groups to Monarch accounts 🧾
Before the first real sync, run this **once** to create/map **one Monarch manual account per loan group** and save a stable mapping under `data/` (so later account renames won’t break anything).

- **Docker (recommended)**:

```bash
docker compose run --rm --build studentaid-monarch-sync setup-monarch-accounts --apply
```

- **Python**:

```bash
.venv\Scripts\python -m studentaid_monarch_sync setup-monarch-accounts --apply
```

After this, your scheduled runs can just execute `sync`.


#### Choose your runtime 🧭
Pick **one** of the following and run it end-to-end. For most people (especially NAS/Unraid), **Docker is the recommended option** 🐳✅

#### Runtime A: Docker (recommended) 🐳
This repo includes a `docker-compose.yml` service that runs the sync as a **run-once** container.
The Docker image now installs a real **Google Chrome** channel and uses a Linux-specific browser-compat profile, which reduces `HeadlessChrome`-style fingerprinting that can trigger portal `403 Access Denied` responses.

##### Docker Desktop (Windows/macOS)
1. Install Docker Desktop and make sure `docker compose` works in a terminal.
2. From the repo folder, create the persistent `data/` folder (stores logs, SQLite state, and Playwright session):

```bash
mkdir data
```

3. Preflight (inside the container):

```bash
docker compose run --rm --build studentaid-monarch-sync preflight
```

4. Dry-run:

```bash
docker compose run --rm studentaid-monarch-sync sync --dry-run --payments-since 2025-01-01
```

5. Run for real (writes to Monarch):

```bash
docker compose run --rm studentaid-monarch-sync sync --payments-since 2025-01-01
```

##### Scheduling on Docker Desktop 🗓️
Docker Desktop doesn’t include a built-in scheduler. The usual pattern is: **use your host OS scheduler** to run the sync command.
To keep the scheduled command simple (and easy to update later), you can schedule a small wrapper script from this repo.

- **Windows (Task Scheduler)**:
  - Create a task that runs daily or weekly.
  - **Program/script**: `C:\Program Files\PowerShell\7\pwsh.exe` (or `powershell.exe`)
  - **Add arguments** (example):

```text
-NoProfile -File .\scripts\docker_sync.ps1 update-run --payments-since 2025-01-01
```

  - **Start in**: `C:\path\to\repo` (the folder that contains `docker-compose.yml`)

- **macOS (launchd)**:
  - Create a LaunchAgent that runs:

```bash
cd /path/to/repo && bash ./scripts/docker_sync.sh update-run --payments-since 2025-01-01
```

- **Linux (cron/systemd)**:
  - Use cron or a systemd timer to run the same command on your desired timeframe:

```bash
cd /path/to/repo && bash ./scripts/docker_sync.sh update-run --payments-since 2025-01-01
```

##### Unraid (NAS)
1. Put the repo on persistent storage (or copy just `docker-compose.yml`, `.env`, and create `data/`).
2. Create the persistent `data/` folder:

```bash
mkdir -p data
```

3. Test-run once (recommended):

```bash
bash ./scripts/docker_sync.sh setup-accounts
bash ./scripts/docker_sync.sh preflight
bash ./scripts/docker_sync.sh dry-run --payments-since 2025-01-01
```

4. Schedule it (e.g., Unraid **User Scripts** plugin) with a daily command like:

```bash
cd /path/to/repo && bash ./scripts/docker_sync.sh update-run --payments-since 2025-01-01
```

Keep `./data` persistent so sessions and the SQLite idempotency DB survive restarts.

#### Runtime B: Python (desktop/server) 🐍
**Install**

- **Windows**:

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m playwright install chromium
```

- **Linux / macOS**:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .
.venv/bin/python -m playwright install chromium
```

**(Optional) Run unit tests 🧪**

```bash
.venv/bin/python -m pytest -q
```

**Preflight (fast fail, no Playwright) ⚡**

```bash
.venv\Scripts\python -m studentaid_monarch_sync preflight
```

**Dry-run (recommended first run) 🧪**

```bash
.venv\Scripts\python -m studentaid_monarch_sync sync --dry-run --headful
```

**Run for real (writes to Monarch) ✅**

```bash
.venv\Scripts\python -m studentaid_monarch_sync sync --payments-since 2025-01-01 --max-payments 10
```

**(Optional) Schedule it on Windows 🗓️**
If you want this to run daily on Windows, use **Task Scheduler**:

1. Open **Task Scheduler** → **Create Task…**
2. **General**:
   - Name: `Monarch Student Loan Sync`
   - Select **Run whether user is logged on or not**
   - Check **Run with highest privileges** (helps with browser automation)
3. **Triggers**:
   - New… → Daily (pick your time)
4. **Actions**:
   - New… → **Start a program**
   - **Program/script**: `C:\path\to\repo\.venv\Scripts\python.exe`
   - **Add arguments**:

```text
-m studentaid_monarch_sync sync --payments-since 2025-01-01
```

   - **Start in**: `C:\path\to\repo`
5. **Settings** (recommended):
   - Allow task to be run on demand
   - If the task fails, restart every: 5 minutes (up to a few times)

Tip: run the exact command once in PowerShell first to confirm it works before scheduling.

## Advanced (details / Docker / troubleshooting)

### How it works (high level)
- Logs into your servicer portal (typically `https://{provider}.studentaid.gov`) with Playwright
- Browser sessions use **browser-compatibility defaults** (automation flags disabled, realistic viewport/locale/user-agent, randomized interaction delays) to reduce anti-automation detection
- When the portal prompts for MFA, selects **Email**, then polls Gmail IMAP for the code
- Scrapes per-loan balances + payment allocation details
  - **Non-posted payments** (pending, scheduled, processing, cancelled) are automatically detected and skipped — only fully posted payments are synced
- Pushes updates into Monarch via the unofficial Monarch API client
- Stores a small SQLite state DB so runs are **idempotent** (no duplicate payment transactions)
- Includes an extra **duplicate guard** against Monarch itself: **date + amount + merchant** (so even if you reset SQLite, we won’t spam duplicates).
  - Optional: you can enable a more specific duplicate check that also uses the portal’s **payment confirmation/reference** as a search term (see [Config notes (Monarch payments)](#config-notes-monarch-payments)).

<a id="gmail-imap-setup"></a>
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
  - **`GMAIL_IMAP_SENDER_HINT`**: `studentaid.gov` (or a specific one like `<provider>.studentaid.gov`)
  - **`GMAIL_IMAP_SUBJECT_HINT`**: `code` (broad; subjects vary by servicer)
  - Example sender we’ve seen: `NoReply@<provider>.studentaid.gov`
    - Other servicers are likely similar (`<something>@<provider>.studentaid.gov`), but we don’t rely on that—use the hints above.

### Dry-run that ALSO checks Monarch (recommended)
Standard dry-run **does not** call Monarch (it only prints what it would do based on the portal + local SQLite).
If you want to validate the real behavior end-to-end (including the duplicate guard), run:

```bash
.venv\Scripts\python -m studentaid_monarch_sync sync --dry-run --dry-run-check-monarch --headful --payments-since 2025-01-01 --max-payments 1
```

This logs into Monarch in **read-only** mode and prints each payment allocation as:
- `SKIP (duplicate)` if Monarch already has a txn with the same **date + amount + merchant**
- `CREATE` if it would create a new txn

### Debug bundle (auto-created on failures)
If a `sync` run fails, the CLI will automatically create a zip under `data/` containing:
- `data/debug/*` (screenshots/HTML/text snapshots)
- your configured log file (default: `data/sync.log`)

The log file now includes the **full exception traceback** for run failures, so you can see the exact line and error without needing to reproduce the issue interactively.

You can attach that zip when asking for help.

#### Parsing debug text snapshots (offline)
Debug bundles include `*.txt` snapshots of the portal’s rendered page text. You can parse these offline into JSON:

```bash
python3 scripts/parse_portal_text_snapshot.py loans --groups AA,AB --file data/debug/loan_details_not_loaded.txt
python3 scripts/parse_portal_text_snapshot.py payments --file data/debug/payment_detail_0_error.txt
```

### Docker scheduling (Unraid/NAS)
See **Quick start → Runtime A: Docker (recommended)** for the Unraid scheduling command and persistence notes.

### Notes / stability
- StudentAid servicer portals are web portals; UI changes may break selectors. The code is structured so selectors live in one place, and failures should save screenshots/logs for debugging.
- Email MFA automation is sensitive—use a dedicated mailbox if possible.
- **Non-posted payment handling**: payments with status _pending_, _scheduled_, _processing_, or _cancelled_ are automatically skipped. Only fully posted (electronic/regular) payments produce Monarch transactions. If a single row fails to parse, it is skipped with a warning rather than aborting the whole run.
- **Anti-automation / 403 detection**: if the portal returns an HTTP 403 Access Denied (common in headless runs on some servicers), the tool detects it immediately, saves a debug snapshot, and retries once with a fresh session. If it persists, see the [403 troubleshooting entry](#403-access-denied) below.
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

<a id="403-access-denied"></a>
- **HTTP 403 Access Denied / portal blocks the headless browser**
  - Some servicers (notably Nelnet) occasionally return a bare `HTTP 403 Access Denied` page to headless browsers that look like automation. The tool detects this and retries once with a fresh session automatically.
  - Docker builds now install a real Chrome channel and use a Linux-aligned browser fingerprint. If you updated from an older image, rebuild it first with `docker compose build --no-cache`.
  - If it keeps failing, try the following in order:
    1. Run `sync --headful --manual-mfa` once to establish a fresh, trusted browser session stored under `data/servicer_storage_state_*.json`. Subsequent headless runs reuse that session.
    2. Add `--fresh-session` to force discarding any stale stored session before retrying.
    3. If running in Docker on a cloud/datacenter host, try from a residential IP address — some portal WAFs block datacenter IP ranges.
  - A `data/debug/access_denied_403_*.png` screenshot is saved automatically so you can confirm what the portal returned.
  - See also [GitHub issue #9](https://github.com/mattebad/monarch-studentaid-sync/issues/9) for community discussion.

### Config notes (Monarch payments)
- `monarch.payment_merchant_name`: merchant name to use when creating payment transactions, and the value used by the **duplicate guard**.
  - If your existing loan-account payments show up as **US Department of Education**, set this to that (recommended).
- `monarch.duplicate_guard_use_reference` / `MONARCH_DUPLICATE_GUARD_USE_REFERENCE` (optional): when the portal provides a payment confirmation/reference, use it as a Monarch search term during duplicate detection to reduce false positives for same-day identical payments.

<a id="monarch-token"></a>
### Getting `MONARCH_TOKEN` (token-based auth for SSO)
This works well for **Sign in with Apple** and **Continue with Google** flows, where password-based API login can be confusing.

1. Log into Monarch in your browser normally (Apple/Google/etc).
2. Open DevTools → **Network** tab.
3. Click any request to Monarch’s API (often `graphql`).
4. In **Request Headers**, copy the value after `Authorization: Token ...`.
5. Put that into `.env` as `MONARCH_TOKEN=...` (keep it secret).

### CLI flags (reference)
Run `-h` at any time to see the full help:

```bash
.venv\Scripts\python -m studentaid_monarch_sync -h
.venv\Scripts\python -m studentaid_monarch_sync sync -h
```

#### Global flags
- `--env-file`: Path to a dotenv file (default: `.env`). If present, it will be loaded before reading config/env vars.

#### `sync` flags
- `--config`: Path to YAML config (optional; default: `config.yaml` if you use one).
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
- `--config`: Path to YAML config (optional; default: `config.yaml` if you use one).

