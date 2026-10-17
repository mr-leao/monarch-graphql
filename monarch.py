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
"""

from __future__ import annotations

import asyncio
import calendar
import getpass
import json
import os
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

import oathtool
from aiohttp import ClientSession
from monarchmoney import CaptchaRequiredException, LoginFailedException, MonarchMoney
from monarchmoney.monarchmoney import MonarchMoneyEndpoints

ROOT = Path(__file__).resolve().parent
SESSION_FILE = ROOT / ".mm" / "mm_session.pickle"
DEVICE_FILE = ROOT / ".mm" / "device_uuid"
ENV_FILE = ROOT / ".env"
# Captured from the Monarch web app. Login returns a structured error rather
# than "app update required" with this value.
CLIENT_VERSION = "v1.0.1668"
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


async def fetch(mm: MonarchMoney) -> tuple[dict, dict, dict]:
    start, end, _month = current_month_range()
    accounts = await mm.get_accounts()
    transactions = await mm.get_transactions(limit=10)
    budgets = await mm.get_budgets(start_date=start, end_date=end)
    return accounts, transactions, budgets


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


def print_transactions(payload: dict) -> None:
    results = (payload.get("allTransactions") or {}).get("results") or []
    print("\nRecent transactions")
    print("-------------------")
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


async def main() -> None:
    load_env_file(ENV_FILE)
    mm = await connect()
    try:
        accounts, transactions, budgets = await fetch(mm)
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
        accounts, transactions, budgets = await fetch(mm)

    print_accounts(accounts)
    print_budget(budgets)
    print_transactions(transactions)


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
