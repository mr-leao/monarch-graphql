#!/usr/bin/env python3
"""Print Monarch Money accounts, net worth, the current budget, and recent transactions.

https://github.com/sanghviharshit/ha-monarchmoney is a Home Assistant
integration. It talks to Monarch through the monarchmoneycommunity package,
which is imported here as `monarchmoney`.

Put credentials in a .env file next to this script (see .env.example), or
export them in the shell:

    MONARCH_EMAIL
    MONARCH_PASSWORD
    MONARCH_MFA_SECRET   optional TOTP secret from Settings > Security
    MONARCH_EMAIL_OTP    optional one-time code from the login email
    MONARCH_COOKIE       optional browser Cookie header (session_id + csrftoken)

A new device is asked for the email code before the authenticator code.
The first successful login is saved to .mm/mm_session.pickle and reused.
If you sign in to Monarch with Google, set a password under Settings > Security
before running this.

With no date arguments, transactions cover the current calendar month, the
same period as the budget. Pass both bounds for a different range:

    python monarch.py --start 2026-07-17 --end 2026-10-16
    python monarch.py --start 2026-07-17T00:00:00 --end 2026-10-16T23:59:59

Monarch stores a calendar date on each transaction. A time in either bound
is accepted and the calendar date of that value is what the query uses.

Pass --recurring to also list every recurring stream, including bills with
no charge in the transaction window:

    python monarch.py --recurring

    python monarch.py --format json
    python monarch.py --format csv --output-dir export --recurring

text is the default. json prints one document. csv writes one file per
section: accounts, net worth, budget totals, categories, goals, and
transactions. recurring.csv is written only with --recurring.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import csv
import getpass
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import oathtool
from aiohttp import ClientSession
from gql import gql
from monarchmoney import CaptchaRequiredException, LoginFailedException, MonarchMoney
from monarchmoney.monarchmoney import MonarchMoneyEndpoints

ROOT = Path(__file__).resolve().parent
SESSION_FILE = ROOT / ".mm" / "mm_session.pickle"
DEVICE_FILE = ROOT / ".mm" / "device_uuid"
ENV_FILE = ROOT / ".env"
# Captured from the Monarch web app. Login returns a structured error rather
# than "app update required" with this value.
CLIENT_VERSION = "v1.0.1668"
TRANSACTION_PAGE_SIZE = 100
RECURRING_QUERY = gql(
    """
    query Recurring($includeLiabilities: Boolean, $includePending: Boolean) {
      recurringTransactionStreams(
        includeLiabilities: $includeLiabilities
        includePending: $includePending
      ) {
        stream {
          id
          name
          frequency
          amount
          isActive
          isApproximate
          recurringType
          reviewStatus
          baseDate
          merchant { id name }
          creditReportLiabilityAccount { id name liabilityType }
        }
        account { id displayName }
        category { id name }
        nextForecastedTransaction { date amount }
      }
    }
    """
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into the environment without overriding existing vars."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def money(amount: float | int | None) -> str:
    if amount is None:
        return "—"
    return f"${amount:,.2f}"


def balance_of(account: dict) -> float:
    value = account.get("displayBalance")
    if value is None:
        value = account.get("currentBalance")
    return float(value or 0)


def device_uuid() -> str:
    """Return a stable device id. Monarch binds a saved token to this value."""
    if DEVICE_FILE.exists():
        value = DEVICE_FILE.read_text().strip()
        if value:
            return value
    DEVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    value = str(uuid4())
    DEVICE_FILE.write_text(value + "\n")
    DEVICE_FILE.chmod(0o600)
    return value


def apply_headers(mm: MonarchMoney, device_id: str) -> None:
    mm._headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Client-Platform": "web",
            "Origin": "https://app.monarch.com",
            "Referer": "https://app.monarch.com/",
            "device-uuid": device_id,
            "monarch-client": "monarch-core-web-app-graphql",
            "monarch-client-version": CLIENT_VERSION,
            "User-Agent": USER_AGENT,
        }
    )


def new_client() -> MonarchMoney:
    mm = MonarchMoney(session_file=str(SESSION_FILE))
    apply_headers(mm, device_uuid())
    return mm


def challenge_kind(status: int, body: dict) -> str | None:
    if status != 403:
        return None
    code = str(body.get("error_code") or "")
    detail = str(body.get("detail") or "")
    blob = f"{code} {detail}".lower()
    if code == "CAPTCHA_REQUIRED" or "captcha" in blob:
        return "captcha"
    if code == "EMAIL_OTP_REQUIRED" or "email" in blob:
        return "email"
    if code == "MFA_REQUIRED" or any(
        word in blob for word in ("mfa", "totp", "authenticator", "two-factor", "two factor")
    ):
        return "mfa"
    return "other"


def accept_token(mm: MonarchMoney, body: dict) -> None:
    token = body.get("token")
    expiration = body.get("tokenExpiration")
    if not token:
        raise LoginFailedException("Login succeeded but no token was returned.")
    if str(token).count(".") == 2:
        raise LoginFailedException("Received a short-lived token. Refusing to save it.")
    if expiration not in (None, "null"):
        raise LoginFailedException(
            f"Short-lived token returned (tokenExpiration={expiration})."
        )
    mm.set_token(token)
    mm._headers["Authorization"] = f"Token {token}"
    mm.save_session()


async def post_login(
    mm: MonarchMoney,
    email: str,
    password: str,
    email_otp: str | None,
    totp: str | None,
) -> tuple[int, dict]:
    payload: dict = {
        "username": email,
        "password": password,
        "supports_mfa": True,
        "supports_email_otp": True,
        "supports_recaptcha": True,
        "trusted_device": True,
    }
    if email_otp:
        payload["email_otp"] = email_otp
    if totp:
        payload["totp"] = totp
    async with ClientSession(headers=mm._headers) as session:
        async with session.post(
            MonarchMoneyEndpoints.getLoginEndpoint(), json=payload
        ) as response:
            text = await response.text()
            try:
                body = json.loads(text) if text else {}
            except json.JSONDecodeError:
                body = {"detail": text[:300]}
            if not isinstance(body, dict):
                body = {"detail": str(body)[:300]}
            return response.status, body


def prompt_email_code() -> str:
    print(
        "Monarch sent a one-time code to your email. "
        "Enter that code, not the authenticator app code.",
        file=sys.stderr,
    )
    code = input("Email code: ").strip()
    if not code:
        raise LoginFailedException("No email code provided.")
    return code


def totp_from_secret(secret: str) -> str:
    try:
        return oathtool.generate_otp(secret)
    except Exception as exc:
        raise LoginFailedException(f"Could not generate an authenticator code: {exc}") from exc


async def login(mm: MonarchMoney) -> None:
    cookie = os.environ.get("MONARCH_COOKIE", "").strip()
    if cookie:
        await mm.login_with_cookies(cookie)
        return

    email = os.environ.get("MONARCH_EMAIL", "").strip()
    password = os.environ.get("MONARCH_PASSWORD", "").strip()
    mfa_secret = os.environ.get("MONARCH_MFA_SECRET", "").strip() or None
    email_otp = os.environ.get("MONARCH_EMAIL_OTP", "").strip() or None
    if not email or not password:
        print("No credentials in the environment. Enter them to log in.", file=sys.stderr)
        email = email or input("Email: ").strip()
        password = password or getpass.getpass("Password: ")

    totp: str | None = None
    used_secret = False
    for _ in range(4):
        status, body = await post_login(mm, email, password, email_otp, totp)
        if status == 200:
            accept_token(mm, body)
            return

        kind = challenge_kind(status, body)
        detail = str(body.get("detail") or body.get("error_code") or f"HTTP {status}")
        if kind == "captcha":
            raise CaptchaRequiredException(
                "Programmatic login is blocked by CAPTCHA. "
                "Use login_with_cookies() to authenticate with browser cookies instead."
            )
        if kind == "email":
            email_otp = prompt_email_code()
            if mfa_secret and not used_secret:
                totp = totp_from_secret(mfa_secret)
                used_secret = True
                print("Using MONARCH_MFA_SECRET for the authenticator code.", file=sys.stderr)
            continue
        if kind == "mfa":
            if mfa_secret and not used_secret:
                totp = totp_from_secret(mfa_secret)
                used_secret = True
                print("Using MONARCH_MFA_SECRET for the authenticator code.", file=sys.stderr)
                continue
            print(detail, file=sys.stderr)
            totp = input("Authenticator code: ").strip()
            if not totp:
                raise LoginFailedException("No authenticator code provided.")
            continue
        raise LoginFailedException(detail)

    raise LoginFailedException("Login did not finish after the email and authenticator codes.")


async def connect() -> MonarchMoney:
    mm = new_client()
    if SESSION_FILE.exists():
        print(f"Using saved session at {SESSION_FILE}", file=sys.stderr)
        mm.load_session()
        return mm
    await login(mm)
    if SESSION_FILE.exists():
        SESSION_FILE.chmod(0o600)
    return mm


def current_month_range() -> tuple[str, str, str]:
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    start = today.replace(day=1).isoformat()
    end = today.replace(day=last_day).isoformat()
    return start, end, today.strftime("%Y-%m")


def parse_range_bound(value: str) -> datetime:
    """Parse a calendar date or an ISO-8601 datetime."""
    text = value.strip()
    date_only = "T" not in text and " " not in text
    try:
        if date_only:
            day = date.fromisoformat(text)
            return datetime(day.year, day.month, day.day)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "use YYYY-MM-DD or an ISO-8601 datetime, for example 2026-07-17T15:30:00"
        ) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print Monarch Money accounts, net worth, the current budget, and transactions. "
            "Transactions default to the current calendar month."
        )
    )
    parser.add_argument(
        "--start",
        type=parse_range_bound,
        metavar="DATETIME",
        help=(
            "Include transactions on or after this date. "
            "YYYY-MM-DD or an ISO-8601 datetime. Must be passed with --end. "
            "Defaults to the first day of the current month."
        ),
    )
    parser.add_argument(
        "--end",
        type=parse_range_bound,
        metavar="DATETIME",
        help=(
            "Include transactions on or before this date. "
            "YYYY-MM-DD or an ISO-8601 datetime. Must be passed with --start. "
            "Defaults to the last day of the current month."
        ),
    )
    parser.add_argument(
        "--recurring",
        action="store_true",
        help=(
            "Also list every recurring stream Monarch has on file, "
            "including pending ones and credit-report liabilities. "
            "This is not limited to the transaction date range."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "csv"),
        default="text",
        help=(
            "text prints the report. json prints one document on standard output. "
            "csv writes one file per section."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for CSV files. Used with --format csv. Default: export/ next to this script.",
    )
    args = parser.parse_args(argv)
    if args.output_dir is not None and args.format != "csv":
        parser.error("--output-dir is only used with --format csv")
    if (args.start is None) != (args.end is None):
        parser.error("pass both --start and --end")
    if args.start is None:
        start, end, _month = current_month_range()
        args.start = parse_range_bound(start)
        args.end = parse_range_bound(end)
    elif args.start.date() > args.end.date():
        parser.error("--start must be on or before --end")
    return args


def transaction_day(txn: dict) -> date | None:
    raw = txn.get("date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


async def get_transactions_in_range(mm: MonarchMoney, start: date, end: date) -> dict:
    """Return every transaction whose calendar date falls in start..end, inclusive."""
    results: list[dict] = []
    offset = 0
    total: int | None = None
    while True:
        page = await mm.get_transactions(
            limit=TRANSACTION_PAGE_SIZE,
            offset=offset,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
        bucket = page.get("allTransactions") or {}
        chunk = bucket.get("results") or []
        if total is None and bucket.get("totalCount") is not None:
            total = int(bucket["totalCount"])
        results.extend(chunk)
        offset += len(chunk)
        if not chunk or len(chunk) < TRANSACTION_PAGE_SIZE or (total is not None and offset >= total):
            break

    kept = [txn for txn in results if (day := transaction_day(txn)) is not None and start <= day <= end]
    kept.sort(key=lambda txn: (txn.get("date") or "", txn.get("createdAt") or ""), reverse=True)
    return {"allTransactions": {"results": kept, "totalCount": len(kept)}}


async def get_recurring_streams(mm: MonarchMoney) -> list[dict]:
    """Return every recurring stream, not only occurrences inside a date window."""
    payload = await mm.gql_call(
        operation="Recurring",
        graphql_query=RECURRING_QUERY,
        variables={"includeLiabilities": True, "includePending": True},
    )
    rows = list(payload.get("recurringTransactionStreams") or [])
    rows.sort(key=lambda row: (0 if (row.get("stream") or {}).get("isActive") else 1, recurring_name(row).lower()))
    return rows


def recurring_name(row: dict) -> str:
    stream = row.get("stream") or {}
    merchant = (stream.get("merchant") or {}).get("name")
    liability = (stream.get("creditReportLiabilityAccount") or {}).get("name")
    return stream.get("name") or merchant or liability or "Recurring"


async def fetch(
    mm: MonarchMoney,
    start: datetime,
    end: datetime,
    include_recurring: bool = False,
) -> tuple[dict, dict, dict, list[dict] | None]:
    month_start, month_end, _month = current_month_range()
    accounts = await mm.get_accounts()
    transactions = await get_transactions_in_range(mm, start.date(), end.date())
    budgets = await mm.get_budgets(start_date=month_start, end_date=month_end)
    recurring = await get_recurring_streams(mm) if include_recurring else None
    return accounts, transactions, budgets, recurring


def number(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def account_report(payload: dict) -> tuple[list[dict], float]:
    accounts = [
        account
        for account in payload.get("accounts", [])
        if not account.get("isHidden") and not account.get("deactivatedAt")
    ]
    accounts.sort(
        key=lambda account: (
            ((account.get("type") or {}).get("display") or ""),
            account.get("displayName") or "",
        )
    )
    rows = []
    net_worth = 0.0
    for account in accounts:
        amount = balance_of(account)
        included = bool(account.get("includeInNetWorth", True))
        if included:
            net_worth += amount
        rows.append(
            {
                "type": (account.get("type") or {}).get("display") or "Other",
                "name": account.get("displayName") or "Unnamed",
                "balance": number(amount),
                "subtype": (account.get("subtype") or {}).get("display") or "",
                "institution": (account.get("institution") or {}).get("name") or "",
                "include_in_net_worth": included,
            }
        )
    return rows, number(net_worth) or 0.0


def budget_report(payload: dict) -> dict:
    _start, _end, month = current_month_range()
    budget_data = payload.get("budgetData") or {}
    groups = payload.get("categoryGroups") or []
    totals_row = month_row(budget_data.get("totalsByMonth"), month) or {}
    totals = []
    for label, key in (
        ("Income", "totalIncome"),
        ("Expenses", "totalExpenses"),
        ("Fixed", "totalFixedExpenses"),
        ("Flexible", "totalFlexibleExpenses"),
        ("Non-monthly", "totalNonMonthlyExpenses"),
    ):
        block = totals_row.get(key) or {}
        totals.append(
            {
                "month": month,
                "name": label,
                "budget": number(block.get("plannedAmount")),
                "actual": number(block.get("actualAmount")),
                "left": number(block.get("remainingAmount")),
            }
        )

    amounts_by_category = {
        (row.get("category") or {}).get("id"): month_row(row.get("monthlyAmounts"), month)
        for row in budget_data.get("monthlyAmountsByCategory") or []
    }
    categories = []
    ordered_groups = sorted(groups, key=lambda group: (group.get("type") != "income", group.get("order") or 0))
    for group in ordered_groups:
        group_name = group.get("name") or "Other"
        for category in sorted(group.get("categories") or [], key=lambda item: item.get("order") or 0):
            amounts = amounts_by_category.get(category["id"]) or {}
            planned = signed(amounts.get("plannedCashFlowAmount")) + signed(amounts.get("plannedSetAsideAmount"))
            actual = signed(amounts.get("actualAmount"))
            remaining = signed(amounts.get("remainingAmount"))
            if planned == 0 and actual == 0 and remaining == 0:
                continue
            categories.append(
                {
                    "month": month,
                    "group": group_name,
                    "category": category.get("name") or "Unnamed",
                    "budget": number(planned),
                    "actual": number(actual),
                    "left": number(remaining),
                }
            )

    goals = []
    for goal in payload.get("goalsV2") or []:
        if goal.get("archivedAt") or goal.get("completedAt"):
            continue
        planned = sum(
            signed(row.get("amount"))
            for row in goal.get("plannedContributions") or []
            if str(row.get("month") or "").startswith(month)
        )
        contributed = sum(
            signed(row.get("sum"))
            for row in goal.get("monthlyContributionSummaries") or []
            if str(row.get("month") or "").startswith(month)
        )
        if planned == 0 and contributed == 0:
            continue
        goals.append(
            {
                "month": month,
                "name": goal.get("name") or "Goal",
                "planned": number(planned),
                "contributed": number(contributed),
            }
        )
    return {"month": month, "totals": totals, "categories": categories, "goals": goals}


def transaction_rows(payload: dict) -> list[dict]:
    results = (payload.get("allTransactions") or {}).get("results") or []
    rows = []
    for txn in results:
        merchant = (txn.get("merchant") or {}).get("name") or txn.get("plaidName") or None
        rows.append(
            {
                "date": txn.get("date") or "",
                "merchant": merchant,
                "amount": number(txn.get("amount")),
                "pending": bool(txn.get("pending")),
                "category": (txn.get("category") or {}).get("name") or "",
                "account": (txn.get("account") or {}).get("displayName") or "",
            }
        )
    return rows


def recurring_rows(rows: list[dict]) -> list[dict]:
    exported = []
    for row in rows:
        stream = row.get("stream") or {}
        amount = stream.get("amount")
        next_txn = row.get("nextForecastedTransaction") or {}
        if amount is None:
            amount = next_txn.get("amount")
        exported.append(
            {
                "name": recurring_name(row),
                "frequency": stream.get("frequency") or "",
                "amount": number(amount),
                "next_date": next_txn.get("date") or "",
                "active": bool(stream.get("isActive")),
                "review_status": stream.get("reviewStatus") or "",
                "type": stream.get("recurringType") or "",
                "approximate": bool(stream.get("isApproximate")),
                "category": (row.get("category") or {}).get("name") or "",
                "account": (row.get("account") or {}).get("displayName") or "",
            }
        )
    return exported


def build_report(
    accounts: dict,
    budgets: dict,
    transactions: dict,
    start: datetime,
    end: datetime,
    recurring: list[dict] | None,
) -> dict:
    account_rows, net_worth = account_report(accounts)
    report = {
        "accounts": account_rows,
        "net_worth": net_worth,
        "budget": budget_report(budgets),
        "transactions": {
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
            "items": transaction_rows(transactions),
        },
    }
    if recurring is not None:
        report["recurring"] = recurring_rows(recurring)
    return report


def csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in fieldnames})


CSV_FILES = (
    ("accounts.csv", "accounts", ("type", "name", "balance", "subtype", "institution", "include_in_net_worth")),
    ("budget_totals.csv", ("budget", "totals"), ("month", "name", "budget", "actual", "left")),
    ("categories.csv", ("budget", "categories"), ("month", "group", "category", "budget", "actual", "left")),
    ("goals.csv", ("budget", "goals"), ("month", "name", "planned", "contributed")),
    ("transactions.csv", ("transactions", "items"), ("date", "merchant", "amount", "pending", "category", "account")),
    (
        "recurring.csv",
        "recurring",
        (
            "name",
            "frequency",
            "amount",
            "next_date",
            "active",
            "review_status",
            "type",
            "approximate",
            "category",
            "account",
        ),
    ),
)


def report_section(report: dict, path: str | tuple[str, str]) -> list[dict] | None:
    if isinstance(path, str):
        if path not in report:
            return None
        return report[path]
    parent, child = path
    return report[parent][child]


def write_csv_report(directory: Path, report: dict) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    net_worth_path = directory / "net_worth.csv"
    write_csv(net_worth_path, [{"net_worth": report["net_worth"]}], ["net_worth"])
    written.append(net_worth_path)
    for filename, section, fieldnames in CSV_FILES:
        rows = report_section(report, section)
        if rows is None:
            continue
        path = directory / filename
        write_csv(path, rows, list(fieldnames))
        written.append(path)
    return written


def emit_json(report: dict) -> None:
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def emit_csv(report: dict, directory: Path) -> None:
    for path in write_csv_report(directory, report):
        print(path, file=sys.stderr)


def print_accounts(payload: dict) -> None:
    accounts = [
        account
        for account in payload.get("accounts", [])
        if not account.get("isHidden") and not account.get("deactivatedAt")
    ]
    accounts.sort(
        key=lambda account: (
            ((account.get("type") or {}).get("display") or ""),
            account.get("displayName") or "",
        )
    )

    print("Accounts")
    print("--------")
    current_type = None
    net_worth = 0.0
    for account in accounts:
        account_type = (account.get("type") or {}).get("display") or "Other"
        if account_type != current_type:
            current_type = account_type
            print(f"\n{account_type}")
        subtype = (account.get("subtype") or {}).get("display") or ""
        institution = (account.get("institution") or {}).get("name") or ""
        label = account.get("displayName") or "Unnamed"
        detail = " · ".join(part for part in (subtype, institution) if part)
        suffix = f"  ({detail})" if detail else ""
        amount = balance_of(account)
        print(f"  {label:<32} {money(amount):>14}{suffix}")
        if account.get("includeInNetWorth", True):
            net_worth += amount

    print(f"\nNet worth (included balances): {money(net_worth)}")


def month_row(rows: list | None, month: str) -> dict | None:
    for row in rows or []:
        if str(row.get("month") or "").startswith(month):
            return row
    return None


def signed(value: float | int | None) -> float:
    return float(value or 0)


def print_budget(payload: dict) -> None:
    _start, _end, month = current_month_range()
    budget_data = payload.get("budgetData") or {}
    groups = payload.get("categoryGroups") or []

    print(f"\nBudget {month}")
    print("-----------")
    totals = month_row(budget_data.get("totalsByMonth"), month) or {}
    for label, key in (
        ("Income", "totalIncome"),
        ("Expenses", "totalExpenses"),
        ("Fixed", "totalFixedExpenses"),
        ("Flexible", "totalFlexibleExpenses"),
        ("Non-monthly", "totalNonMonthlyExpenses"),
    ):
        block = totals.get(key) or {}
        print(
            f"  {label:<16} "
            f"budget {money(block.get('plannedAmount')):>12}  "
            f"actual {money(block.get('actualAmount')):>12}  "
            f"left {money(block.get('remainingAmount')):>12}"
        )

    amounts_by_category = {
        (row.get("category") or {}).get("id"): month_row(row.get("monthlyAmounts"), month)
        for row in budget_data.get("monthlyAmountsByCategory") or []
    }
    print("\nCategories")
    current_group = None
    shown = 0
    ordered_groups = sorted(groups, key=lambda group: (group.get("type") != "income", group.get("order") or 0))
    for group in ordered_groups:
        lines = []
        for category in sorted(group.get("categories") or [], key=lambda item: item.get("order") or 0):
            amounts = amounts_by_category.get(category["id"]) or {}
            planned = signed(amounts.get("plannedCashFlowAmount")) + signed(
                amounts.get("plannedSetAsideAmount")
            )
            actual = signed(amounts.get("actualAmount"))
            remaining = signed(amounts.get("remainingAmount"))
            if planned == 0 and actual == 0 and remaining == 0:
                continue
            lines.append(
                f"    {category.get('name') or 'Unnamed':<28} "
                f"{money(planned):>12}  {money(actual):>12}  {money(remaining):>12}"
            )
        if not lines:
            continue
        group_name = group.get("name") or "Other"
        if group_name != current_group:
            current_group = group_name
            print(f"\n  {group_name}")
            print(f"    {'':<28} {'budget':>12}  {'actual':>12}  {'left':>12}")
        print("\n".join(lines))
        shown += len(lines)
    if shown == 0:
        print("  no budgeted or spent categories this month")

    goals = []
    for goal in payload.get("goalsV2") or []:
        if goal.get("archivedAt") or goal.get("completedAt"):
            continue
        planned = sum(
            signed(row.get("amount"))
            for row in goal.get("plannedContributions") or []
            if str(row.get("month") or "").startswith(month)
        )
        contributed = sum(
            signed(row.get("sum"))
            for row in goal.get("monthlyContributionSummaries") or []
            if str(row.get("month") or "").startswith(month)
        )
        if planned == 0 and contributed == 0:
            continue
        goals.append((goal.get("name") or "Goal", planned, contributed))
    if goals:
        print("\nGoals")
        for name, planned, contributed in goals:
            print(f"  {name:<32} planned {money(planned):>12}  contributed {money(contributed):>12}")


def print_transactions(payload: dict, start: datetime, end: datetime) -> None:
    results = (payload.get("allTransactions") or {}).get("results") or []
    title = f"Transactions {start.date().isoformat()} to {end.date().isoformat()}"
    print(f"\n{title}")
    print("-" * len(title))
    if not results:
        print("  none")
        return
    for txn in results:
        merchant = (txn.get("merchant") or {}).get("name") or txn.get("plaidName") or "—"
        category = (txn.get("category") or {}).get("name") or ""
        account = (txn.get("account") or {}).get("displayName") or ""
        extra = " · ".join(part for part in (category, account) if part)
        suffix = f"  ({extra})" if extra else ""
        pending = " pending" if txn.get("pending") else ""
        print(
            f"  {txn.get('date', ''):<12} {merchant:<28} "
            f"{money(txn.get('amount')):>14}{pending}{suffix}"
        )


def print_recurring(rows: list[dict]) -> None:
    print("\nRecurring")
    print("---------")
    if not rows:
        print("  none")
        return
    for row in rows:
        stream = row.get("stream") or {}
        amount = stream.get("amount")
        next_txn = row.get("nextForecastedTransaction") or {}
        if amount is None:
            amount = next_txn.get("amount")
        category = (row.get("category") or {}).get("name") or ""
        account = (row.get("account") or {}).get("displayName") or ""
        extra = " · ".join(part for part in (category, account) if part)
        suffix = f"  ({extra})" if extra else ""
        flags: list[str] = []
        if not stream.get("isActive"):
            flags.append("inactive")
        review = stream.get("reviewStatus")
        if review and review != "approved":
            flags.append(str(review))
        kind = stream.get("recurringType")
        if kind and kind != "expense":
            flags.append(str(kind))
        if stream.get("isApproximate"):
            flags.append("approximate")
        flag_text = f"  [{', '.join(flags)}]" if flags else ""
        next_date = next_txn.get("date") or ""
        next_text = f"  next {next_date}" if next_date else ""
        frequency = stream.get("frequency") or ""
        print(
            f"  {recurring_name(row):<32} {frequency:<10} "
            f"{money(amount):>14}{next_text}{flag_text}{suffix}"
        )


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    load_env_file(ENV_FILE)
    mm = await connect()
    try:
        accounts, transactions, budgets, recurring = await fetch(
            mm, args.start, args.end, args.recurring
        )
    except (LoginFailedException, CaptchaRequiredException):
        raise
    except Exception:
        if not SESSION_FILE.exists():
            raise
        print("Saved session was rejected. Logging in again.", file=sys.stderr)
        SESSION_FILE.unlink()
        mm = new_client()
        await login(mm)
        if SESSION_FILE.exists():
            SESSION_FILE.chmod(0o600)
        accounts, transactions, budgets, recurring = await fetch(
            mm, args.start, args.end, args.recurring
        )

    if args.format == "text":
        print_accounts(accounts)
        print_budget(budgets)
        print_transactions(transactions, args.start, args.end)
        if recurring is not None:
            print_recurring(recurring)
        return

    report = build_report(accounts, budgets, transactions, args.start, args.end, recurring)
    if args.format == "json":
        emit_json(report)
        return
    emit_csv(report, args.output_dir or (ROOT / "export"))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except CaptchaRequiredException as exc:
        print(
            f"{exc}\nSet MONARCH_COOKIE to a browser Cookie header that includes "
            "session_id and csrftoken, then run this again.",
            file=sys.stderr,
        )
        sys.exit(1)
    except LoginFailedException as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
