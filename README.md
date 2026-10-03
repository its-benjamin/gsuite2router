# gsuite2router

Bulk-add Google Workspace (GSuite) accounts to **Antigravity**, **Cline**, and **Kilo Code** providers on a [9Router](https://9router.ai) instance.

## How It Works

```
9Router API                          Google OAuth (browser)
───────────                          ─────────────────────
POST /api/auth/login ──► auth cookie
GET  /api/oauth/authorize ──► authUrl ──► Chrome opens ──► email/password ──► consent ──► auth code
POST /api/oauth/exchange  ◄── code ◄──────────────────────────────────────────────────────┘
```

- **9Router** interaction is done entirely via REST API (fast, no fragile CSS selectors).
- **Google OAuth** login is done via browser (DrissionPage + Chrome) because Google blocks programmatic login.
- Each account gets a **fresh browser profile** (temp directory) — your existing Chrome profile is never touched.
- Google Workspace Terms of Service consent pages are handled automatically when encountered.

## Requirements

- Python 3.8+
- Google Chrome installed
- A running 9Router instance (local or remote)

## Installation

```bash
git clone https://github.com/mhiqrambg/gsuite2router.git
cd gsuite2router

python3 -m venv venv
source venv/bin/activate   # Linux/macOS
# venv\Scripts\activate    # Windows

pip install -e .
```

## Quick Start

### 1. Initialize config

Save your 9Router URL and password so you don't have to type them every time.

```bash
# Interactive
gsuite2router init

# Or pass directly
gsuite2router init --url http://localhost:20128 --password 'yourpassword'
```

Config is saved to `.gs2router.json` in the current directory (gitignored).

> **Note:** If your password contains special characters (`&`, `%`, `@`, `!`), wrap it in **single quotes**.

### 2. Create account file

Create `akun.txt` in the current directory with one Google Workspace account per line:

```
user1@yourdomain.com|password123
user2@yourdomain.com|password456
user3@yourdomain.com|password789
```

Format: `email|password`

### 3. Add accounts

```bash
# Default: adds to Antigravity
gsuite2router add

# Add to all three providers: Antigravity, Cline, and Kilo Code
gsuite2router add --provider all

# Add to a specific provider only
gsuite2router add --provider kilocode
```

The tool will:
1. Log in to 9Router via API
2. For each account, open Chrome, log in to Google, handle consent pages
3. Exchange the OAuth authorization code via API to create the connection
4. Remove successfully added accounts from `akun.txt`

### 4. Delete exhausted accounts (optional)

Scan for and remove Antigravity connections that have hit quota limits:

```bash
# Scan and delete
gsuite2router delete

# Scan only (dry run)
gsuite2router delete --dry-run
```

## Commands

### `gsuite2router init`

Initialize or update the saved config with your 9Router URL and password.

```
Options:
  --url URL            9Router URL (default: http://localhost:20128)
  --password PASSWORD  9Router password (default: 123456)
```

### `gsuite2router add`

Add accounts from `akun.txt` to the selected providers on 9Router.

```
Options:
  --provider {antigravity,both,all,cline,kilocode}
                        Target provider(s):
                          antigravity  Antigravity only (default)
                          both         Antigravity & Cline
                          all          Antigravity, Cline, & Kilo Code
                          cline        Cline only
                          kilocode     Kilo Code only
  --url URL             9Router URL (overrides config)
  --password PASSWORD   9Router password (overrides config)
  --file FILE           Path to account file (default: akun.txt in CWD)
  --fast                Fast mode — reduced delays, suited for stable connections
  --delay DELAY         Delay between accounts in seconds (default: 3)
  --redirect-uri URI    OAuth redirect URI (default: http://localhost:20128/callback)
```

### `gsuite2router delete`

Delete exhausted / quota-exceeded Antigravity connections.

```
Options:
  --url URL             9Router URL (overrides config)
  --password PASSWORD   9Router password (overrides config)
  --dry-run             Scan only, do not delete
```

## Examples

```bash
# Add accounts to Antigravity (default)
gsuite2router add

# Add to all providers (Antigravity, Cline, Kilo Code)
gsuite2router add --provider all

# Add to Kilo Code only
gsuite2router add --provider kilocode

# Fast mode with a custom inter-account delay
gsuite2router add --provider all --fast --delay 1

# Use a different account file
gsuite2router add --file /path/to/accounts.txt

# One-off run overriding saved config
gsuite2router add --url http://192.168.1.100:20128 --password 'otherpass'

# Dry-run delete (scan only, nothing removed)
gsuite2router delete --dry-run
```

## Config Priority

Values are resolved in this order:

1. **CLI argument** (e.g. `--url`, `--password`)
2. **Config file** (`.gs2router.json` in the current directory)
3. **Built-in default** (`http://localhost:20128`, password `123456`)

## Project Structure

```
gsuite2router/
├── gsuite2router/
│   ├── __init__.py       # Package version
│   ├── __main__.py       # python -m gsuite2router entry point
│   ├── cli.py            # CLI: init / add / delete subcommands
│   ├── config.py         # Constants, timing profiles, config file I/O
│   ├── router_api.py     # 9Router REST API client
│   ├── google_auth.py    # Google OAuth automation via DrissionPage
│   ├── accounts.py       # Account file read/write helpers
│   └── delete.py         # Quota scan + delete exhausted connections
├── requirements.txt
├── setup.py
├── akun.txt              # Account list (gitignored): email|password per line
└── .gs2router.json       # Saved config (gitignored)
```

## Troubleshooting

**"Cannot connect to … make sure 9Router is running"**
- 9Router is not running, or the URL is wrong. Verify with:
  ```bash
  curl <url>/api/auth/login
  ```

**"redirect_uri_mismatch"**
- The OAuth redirect URI doesn't match what is registered in Google Cloud Console. Use `--redirect-uri` to specify the correct URI.

**Permission denied on akun.txt**
- The file is owned by root or another user. Fix with:
  ```bash
  sudo chown $USER akun.txt
  ```

**Password with special characters**
- Always wrap passwords in single quotes to prevent shell interpretation:
  ```bash
  gsuite2router init --password 'my&pass%word'
  ```

**Google security challenge (phone / 2FA checkpoint)**
- Google occasionally requires phone verification on sign-in, especially for new or suspicious sessions. Try a different account or use a different network/IP address.
