# Agents

This repo is one script, `monarch.py`. It signs in to Monarch Money and prints accounts, net worth, the current month's budget, and transactions. `--recurring` adds the full recurring-stream catalog.

The Monarch client is `monarchmoneycommunity`, imported as `monarchmoney`. Keep the pin in `requirements.txt`. `oathtool`, `aiohttp`, and `gql` come in with that package. `CLIENT_VERSION` is the web app version the login request sends. Change it only when Monarch rejects the current value.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python monarch.py
```

The first successful login writes `.mm/mm_session.pickle` and `.mm/device_uuid`. Later runs reuse that session. A new device is asked for the email code before the authenticator code. `MONARCH_MFA_SECRET` covers the authenticator code. `MONARCH_EMAIL_OTP` is the one-time email code. If password login is blocked, set `MONARCH_COOKIE` to a browser `Cookie` header that includes `session_id` and `csrftoken`.

## Secrets

Never commit or print `.env`, `.mm/`, session cookies, or tokens. They are gitignored. `export/` is gitignored too. Do not put account numbers, balances, or transaction rows into commits, docs, or test output. Tests should print counts, dates, headers, and exit codes.

## Command

```bash
.venv/bin/python monarch.py
.venv/bin/python monarch.py --start 2026-07-17 --end 2026-10-16
.venv/bin/python monarch.py --format json
.venv/bin/python monarch.py --format csv --output-dir export --recurring
```

- `--start` and `--end` are a pair. Each value is `YYYY-MM-DD` or an ISO-8601 datetime. Monarch stores a calendar date on each transaction, so a clock time uses that value's date. The end date is included.
- With no dates, transactions cover the current calendar month, the same period as the budget. Fetch every match. The page size is 100. Do not cap the list at a fixed count.
- `--recurring` lists every stream from `recurringTransactionStreams` (`includeLiabilities` and `includePending`). It is not limited to the transaction dates. The library's `get_recurring_transactions` only returns occurrences inside a date window, so do not use it for this list.
- `--format` is `text` (default), `json`, or `csv`. `--output-dir` is only valid with `csv`. The default directory is `export/` next to the script. CSV paths go to stderr. JSON goes to stdout. Login and session messages stay on stderr.

## Outputs

Text, JSON, and CSV must describe the same rows. Hidden and deactivated accounts are omitted. Category and goal rows that are all zeros are omitted. Archived and completed goals are omitted. Amounts in JSON and CSV are numbers rounded to cents, not `$` text. Booleans are `true` and `false`.

JSON keys: `accounts`, `net_worth`, `budget` (`month`, `totals`, `categories`, `goals`), `transactions` (`start`, `end`, `items`). Include `recurring` only when `--recurring` was passed.

CSV files, always written except `recurring.csv`:

| File | Columns |
| --- | --- |
| `net_worth.csv` | `net_worth` |
| `accounts.csv` | `type`, `name`, `balance`, `subtype`, `institution`, `include_in_net_worth` |
| `budget_totals.csv` | `month`, `name`, `budget`, `actual`, `left` |
| `categories.csv` | `month`, `group`, `category`, `budget`, `actual`, `left` |
| `goals.csv` | `month`, `name`, `planned`, `contributed` |
| `transactions.csv` | `date`, `merchant`, `amount`, `pending`, `category`, `account` |
| `recurring.csv` | `name`, `frequency`, `amount`, `next_date`, `active`, `review_status`, `type`, `approximate`, `category`, `account` |

`goals.csv` is written with a header even when it has no rows. A new field belongs in the text printer, `build_report`, and `CSV_FILES` together.

## Checks

There is no unit-test suite. After a change, run the script with the saved session.

- A text change: capture stdout before and after for the same `--start` and `--end`, and diff it.
- A format change: parse the JSON, confirm the keys above, and confirm each CSV header and row count matches the JSON. Use a one-day transaction range so the run stays small.
- A recurring change: confirm the catalog count is at least as large as the current month's `get_recurring_transactions` stream ids, and that those ids are a subset of the catalog.

Do not build a standalone binary for the scheduled job. That job runs on Linux and can install this venv. A binary would have to be built on Linux for each version, and the script is the thing the job should run.
