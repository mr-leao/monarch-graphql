# Monarch Money Graphql

One-shot report of a Monarch Money account: accounts, net worth, the current month's budget, and transactions. `--recurring` adds every recurring stream.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python monarch.py
```

Put `MONARCH_EMAIL`, `MONARCH_PASSWORD`, and `MONARCH_MFA_SECRET` in `.env` or in the environment. The first login saves a session in `.mm/`. Later runs reuse it.

```bash
.venv/bin/python monarch.py --start 2026-07-17 --end 2026-10-16
.venv/bin/python monarch.py --format json
.venv/bin/python monarch.py --format csv --recurring
```

`--format json` prints one document. `--format csv` writes one file per section under `export/`. With no dates, transactions cover the current month.