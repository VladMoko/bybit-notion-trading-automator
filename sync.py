from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from pybit.unified_trading import HTTP


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
NOTION_DATABASE_ENV = "NOTION_JOURNAL_DATABASE_ID"
NOTION_CASHFLOW_DATABASE_ENV = "NOTION_CASHFLOW_DATABASE_ID"
NOTION_CYCLES_DATABASE_ENV = "NOTION_CYCLES_DATABASE_ID"
NOTION_WEEKLY_DATABASE_ENV = "NOTION_WEEKLY_DATABASE_ID"
NOTION_VERSION = "2022-06-28"
DASHBOARD_BLOCKS = {
    "balance": ("NOTION_BALANCE_BLOCK_ID", "callout"),
    "balance_label": ("NOTION_BALANCE_LABEL_BLOCK_ID", "paragraph"),
    "contributions": ("NOTION_CONTRIBUTIONS_BLOCK_ID", "callout"),
    "profit": ("NOTION_PROFIT_BLOCK_ID", "callout"),
    "roi": ("NOTION_ROI_BLOCK_ID", "callout"),
}


@dataclass(frozen=True)
class Execution:
    execution_id: str
    order_id: str
    symbol: str
    side: str
    price: str
    quantity: str
    value: str
    fee: str
    fee_currency: str
    fee_rate: str
    is_maker: bool
    executed_at_utc: str


@dataclass(frozen=True)
class AccountedExecution:
    execution: Execution
    cycle: int
    profit_usdt: Decimal
    status: str
    position_qty_after: Decimal
    position_cost_after: Decimal


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value == "replace_me":
        raise RuntimeError(f"Заповніть {name} у файлі .env")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def decimal_text(value: Any) -> str:
    try:
        return format(Decimal(str(value or "0")), "f")
    except InvalidOperation as exc:
        raise ValueError(f"Некоректне числове значення Bybit: {value!r}") from exc


def normalize(raw: dict[str, Any]) -> Execution:
    timestamp_ms = int(raw["execTime"])
    executed_at = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return Execution(
        execution_id=str(raw["execId"]),
        order_id=str(raw.get("orderId", "")),
        symbol=str(raw.get("symbol", "")),
        side=str(raw.get("side", "")),
        price=decimal_text(raw.get("execPrice")),
        quantity=decimal_text(raw.get("execQty")),
        value=decimal_text(raw.get("execValue")),
        fee=decimal_text(raw.get("execFee")),
        fee_currency=str(raw.get("feeCurrency", "")),
        fee_rate=decimal_text(raw.get("feeRate")),
        is_maker=bool(raw.get("isMaker", False)),
        executed_at_utc=executed_at.isoformat(),
    )


def load_state() -> dict[str, Any]:
    data: dict[str, Any] = {}
    if STATE_PATH.exists():
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    data.setdefault("seen_execution_ids", [])
    data.setdefault("current_cycle", int(os.getenv("START_CYCLE", "7")))
    data.setdefault("position_qty", "0")
    data.setdefault("position_cost_usdt", "0")
    data.setdefault("open_notion_page_ids", [])
    data.setdefault("realized_profit_since_automation", "0")
    data.setdefault("seen_cashflow_ids", [])
    data.setdefault("cashflow_initialized", False)
    data.setdefault("cycle_opened_at", None)
    data.setdefault("cycle_buy_qty", "0")
    data.setdefault("cycle_buy_value", "0")
    data.setdefault("cycle_sell_qty", "0")
    data.setdefault("cycle_sell_value", "0")
    data.setdefault("cycle_fees_usdt", "0")
    data.setdefault("cycle_profit_usdt", "0")
    return data


def save_state(state: dict[str, Any]) -> None:
    payload = dict(state)
    payload["seen_execution_ids"] = sorted(set(payload["seen_execution_ids"]))
    payload["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {required_env('NOTION_TOKEN')}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fee_usdt(item: Execution) -> Decimal:
    fee = Decimal(item.fee)
    if item.fee_currency.upper() == "USDT":
        return fee
    base_asset = item.symbol.upper().removesuffix("USDT")
    if item.fee_currency.upper() == base_asset:
        return fee * Decimal(item.price)
    return Decimal("0")


def account_execution(item: Execution, state: dict[str, Any]) -> AccountedExecution:
    qty = Decimal(state["position_qty"])
    cost = Decimal(state["position_cost_usdt"])
    trade_qty = Decimal(item.quantity)
    trade_value = Decimal(item.value)
    fee = Decimal(item.fee)
    base_asset = item.symbol.upper().removesuffix("USDT")
    cycle = int(state["current_cycle"])
    tolerance = Decimal(os.getenv("POSITION_TOLERANCE", "0.000001"))

    if item.side.lower() == "buy":
        received_qty = trade_qty - fee if item.fee_currency.upper() == base_asset else trade_qty
        added_cost = trade_value + (fee if item.fee_currency.upper() == "USDT" else Decimal("0"))
        return AccountedExecution(item, cycle, Decimal("0"), "OPEN", qty + received_qty, cost + added_cost)

    inventory_reduction = trade_qty + (fee if item.fee_currency.upper() == base_asset else Decimal("0"))
    if qty <= 0 or inventory_reduction > qty + tolerance:
        raise RuntimeError(
            f"Неможливо порахувати SELL {item.execution_id}: у стані лише {qty} SOL. "
            "Перевірте початкову позицію."
        )
    inventory_reduction = min(inventory_reduction, qty)
    allocated_cost = (cost / qty) * inventory_reduction
    net_proceeds = trade_value - (fee if item.fee_currency.upper() == "USDT" else Decimal("0"))
    remaining_qty = qty - inventory_reduction
    remaining_cost = cost - allocated_cost
    closed = remaining_qty <= tolerance
    if closed:
        remaining_qty = Decimal("0")
        remaining_cost = Decimal("0")
    return AccountedExecution(
        item, cycle, net_proceeds - allocated_cost, "CLOSED" if closed else "OPEN",
        remaining_qty, remaining_cost,
    )


def notion_properties(accounted: AccountedExecution) -> dict[str, Any]:
    item = accounted.execution
    action = "Купівля" if item.side.lower() == "buy" else "Продаж"
    asset = item.symbol.upper().removesuffix("USDT")
    notes = (
        f"Автоматично імпортовано з Bybit. "
        f"Комісія: {item.fee} {item.fee_currency}; maker={item.is_maker}."
    )
    return {
        "Угода": {"title": [{"text": {"content": f"Bybit — {item.side.upper()} {item.symbol} @ {item.price}"}}]},
        "Дата": {"date": {"start": item.executed_at_utc}},
        "Дія": {"select": {"name": action}},
        "Актив": {"select": {"name": asset}},
        "Ціна USDT": {"number": float(Decimal(item.price))},
        "Кількість": {"number": float(Decimal(item.quantity))},
        "Сума USDT": {"number": float(Decimal(item.value))},
        "Комісія USDT": {"number": float(fee_usdt(item))},
        "Прибуток USDT": {"number": float(accounted.profit_usdt)},
        "Цикл": {"number": accounted.cycle},
        "Статус": {"select": {"name": accounted.status}},
        "Нотатки": {"rich_text": [{"text": {"content": notes}}]},
        "Bybit Execution ID": {"rich_text": [{"text": {"content": item.execution_id}}]},
        "Bybit Order ID": {"rich_text": [{"text": {"content": item.order_id}}]},
        "Валюта комісії": {"rich_text": [{"text": {"content": item.fee_currency}}]},
    }


def notion_has_execution(execution_id: str) -> bool:
    response = requests.post(
        f"https://api.notion.com/v1/databases/{required_env(NOTION_DATABASE_ENV)}/query",
        headers=notion_headers(),
        json={
            "page_size": 1,
            "filter": {
                "property": "Bybit Execution ID",
                "rich_text": {"equals": execution_id},
            },
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Notion API {response.status_code}: {response.text}")
    return bool(response.json().get("results"))


def write_to_notion(accounted: AccountedExecution) -> str | None:
    item = accounted.execution
    if notion_has_execution(item.execution_id):
        return None
    response = requests.post(
        "https://api.notion.com/v1/pages",
        headers=notion_headers(),
        json={
            "parent": {"database_id": required_env(NOTION_DATABASE_ENV)},
            "properties": notion_properties(accounted),
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Notion API {response.status_code}: {response.text}")
    return str(response.json()["id"])


def set_notion_status(page_ids: list[str], status: str) -> None:
    for page_id in page_ids:
        response = requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=notion_headers(),
            json={"properties": {"Статус": {"select": {"name": status}}}},
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(f"Notion API {response.status_code}: {response.text}")


def notion_create(database_id: str, properties: dict[str, Any]) -> str:
    response = requests.post(
        "https://api.notion.com/v1/pages",
        headers=notion_headers(),
        json={"parent": {"database_id": database_id}, "properties": properties},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Notion create API {response.status_code}: {response.text}")
    return str(response.json()["id"])


def create_cycle_summary(state: dict[str, Any], accounted: AccountedExecution) -> str:
    cycle = accounted.cycle
    buy_qty = Decimal(state["cycle_buy_qty"])
    buy_value = Decimal(state["cycle_buy_value"])
    sell_qty = Decimal(state["cycle_sell_qty"])
    sell_value = Decimal(state["cycle_sell_value"])
    fees = Decimal(state["cycle_fees_usdt"])
    cycle_profit = Decimal(state["cycle_profit_usdt"])
    contributions = confirmed_cashflow_total()
    total_profit = Decimal(os.getenv("BASE_REALIZED_PROFIT_USDT", "0")) + Decimal(
        state["realized_profit_since_automation"]
    )
    balance = contributions + total_profit
    roi = cycle_profit / buy_value * Decimal("100") if buy_value else Decimal("0")
    opened = state.get("cycle_opened_at") or accounted.execution.executed_at_utc
    closed = accounted.execution.executed_at_utc
    properties = {
        "Коло": {"title": [{"text": {"content": f"Коло {cycle}"}}]},
        "Номер кола": {"number": cycle},
        "Статус": {"select": {"name": "Закрито"}},
        "Актив": {"select": {"name": accounted.execution.symbol.removesuffix("USDT")}},
        "Дата відкриття": {"date": {"start": opened}},
        "Дата закриття": {"date": {"start": closed}},
        "Кількість активу": {"number": float(buy_qty)},
        "Сума входу USDT": {"number": float(buy_value)},
        "Середня входу USDT": {"number": float(buy_value / buy_qty) if buy_qty else 0},
        "Сума виходу USDT": {"number": float(sell_value)},
        "Ціна виходу USDT": {"number": float(sell_value / sell_qty) if sell_qty else 0},
        "Комісії USDT": {"number": float(fees)},
        "Чистий профіт USDT": {"number": float(cycle_profit)},
        "ROI %": {"number": float(roi)},
        "Накопичені внески USDT": {"number": float(contributions)},
        "Накопичений торговий профіт USDT": {"number": float(total_profit)},
        "Баланс після кола USDT": {"number": float(balance)},
        "Операції": {"relation": [{"id": page_id} for page_id in state["open_notion_page_ids"]]},
        "Примітка": {"rich_text": [{"text": {"content": "Автоматично сформовано після повного закриття позиції."}}]},
    }
    return notion_create(required_env(NOTION_CYCLES_DATABASE_ENV), properties)


def upsert_weekly_report(cycle_page_id: str, closed_at: str, cycle: int, profit: Decimal, cumulative: Decimal) -> None:
    closed_date = datetime.fromisoformat(closed_at).date()
    monday = closed_date - timedelta(days=closed_date.weekday())
    sunday = monday + timedelta(days=6)
    iso_year, iso_week, _ = closed_date.isocalendar()
    title = f"Звіт за {iso_week} тиждень {iso_year}"
    response = requests.post(
        f"https://api.notion.com/v1/databases/{required_env(NOTION_WEEKLY_DATABASE_ENV)}/query",
        headers=notion_headers(),
        json={"page_size": 1, "filter": {"property": "Тиждень", "title": {"equals": title}}},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Notion weekly query {response.status_code}: {response.text}")
    rows = response.json().get("results", [])
    if rows:
        row = rows[0]
        current_rel = row["properties"]["Торгові кола"].get("relation", [])
        current_profit = row["properties"]["Профіт USDT"].get("number") or 0
        properties = {
            "Торгові кола": {"relation": current_rel + [{"id": cycle_page_id}]},
            "Профіт USDT": {"number": float(Decimal(str(current_profit)) + profit)},
            "Накопичений профіт USDT": {"number": float(cumulative)},
            "Закриті кола": {"rich_text": [{"text": {"content": f"Додано коло {cycle}"}}]},
        }
        patch = requests.patch(
            f"https://api.notion.com/v1/pages/{row['id']}", headers=notion_headers(),
            json={"properties": properties}, timeout=30,
        )
        if not patch.ok:
            raise RuntimeError(f"Notion weekly update {patch.status_code}: {patch.text}")
        return
    notion_create(required_env(NOTION_WEEKLY_DATABASE_ENV), {
        "Тиждень": {"title": [{"text": {"content": title}}]},
        "Період": {"date": {"start": monday.isoformat(), "end": sunday.isoformat()}},
        "Статус": {"select": {"name": "Підтверджено"}},
        "Профіт USDT": {"number": float(profit)},
        "Накопичений профіт USDT": {"number": float(cumulative)},
        "Торгові кола": {"relation": [{"id": cycle_page_id}]},
        "Закриті кола": {"rich_text": [{"text": {"content": f"Коло {cycle}"}}]},
        "Примітка": {"rich_text": [{"text": {"content": "Автоматично сформовано програмою Bybit → Notion."}}]},
    })


def reset_cycle_metrics(state: dict[str, Any]) -> None:
    state["cycle_opened_at"] = None
    for key in ("cycle_buy_qty", "cycle_buy_value", "cycle_sell_qty", "cycle_sell_value", "cycle_fees_usdt", "cycle_profit_usdt"):
        state[key] = "0"


def update_cycle_metrics(state: dict[str, Any], accounted: AccountedExecution) -> None:
    item = accounted.execution
    if item.side.lower() == "buy" and not state.get("cycle_opened_at"):
        reset_cycle_metrics(state)
        state["cycle_opened_at"] = item.executed_at_utc
    prefix = "cycle_buy" if item.side.lower() == "buy" else "cycle_sell"
    state[f"{prefix}_qty"] = str(Decimal(state[f"{prefix}_qty"]) + Decimal(item.quantity))
    state[f"{prefix}_value"] = str(Decimal(state[f"{prefix}_value"]) + Decimal(item.value))
    state["cycle_fees_usdt"] = str(Decimal(state["cycle_fees_usdt"]) + fee_usdt(item))
    state["cycle_profit_usdt"] = str(Decimal(state.get("cycle_profit_usdt", "0")) + accounted.profit_usdt)


def format_ua(value: Decimal, places: int) -> str:
    return f"{value:.{places}f}".replace(".", ",")


def confirmed_cashflow_total() -> Decimal:
    """Return the net confirmed owner cash flow recorded in Notion."""
    total = Decimal("0")
    cursor: str | None = None

    while True:
        payload: dict[str, Any] = {
            "page_size": 100,
            "filter": {
                "property": "Статус",
                "select": {"equals": "Підтверджено"},
            },
        }
        if cursor:
            payload["start_cursor"] = cursor

        response = requests.post(
            f"https://api.notion.com/v1/databases/{required_env(NOTION_CASHFLOW_DATABASE_ENV)}/query",
            headers=notion_headers(),
            json=payload,
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                f"Notion cash-flow API {response.status_code}: {response.text}. "
                "Перевірте, чи база «Рух коштів — Малий Кит» підключена до інтеграції."
            )

        data = response.json()
        for page in data.get("results", []):
            prop = page.get("properties", {}).get("Вплив на баланс USDT", {})
            value = prop.get("number")
            if value is not None:
                total += Decimal(str(value))

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break

    return total


def dashboard_values(state: dict[str, Any], contributions: Decimal | None = None) -> dict[str, str]:
    if contributions is None:
        contributions = Decimal(os.getenv("BASE_CONTRIBUTIONS_USDT", "0"))
    base_profit = Decimal(os.getenv("BASE_REALIZED_PROFIT_USDT", "0"))
    new_profit = Decimal(state["realized_profit_since_automation"])
    profit = base_profit + new_profit
    balance = contributions + profit
    roi = profit / contributions * Decimal("100") if contributions else Decimal("0")
    last_closed_cycle = int(state["current_cycle"]) - 1
    return {
        "balance": f"{format_ua(balance, 6)} USDT",
        "balance_label": f"Розрахунковий баланс після {last_closed_cycle}-го кола",
        "contributions": f"{format_ua(contributions, 2)} USDT",
        "profit": f"+{format_ua(profit, 6)} USDT" if profit >= 0 else f"{format_ua(profit, 6)} USDT",
        "roi": f"+{format_ua(roi, 2)}%" if roi >= 0 else f"{format_ua(roi, 2)}%",
    }


def update_dashboard(state: dict[str, Any]) -> None:
    if env_bool("CASHFLOW_ENABLED", True):
        contributions = confirmed_cashflow_total()
        print(f"Підтверджений рух власних коштів: {format_ua(contributions, 2)} USDT")
    else:
        contributions = Decimal(os.getenv("BASE_CONTRIBUTIONS_USDT", "0"))
        print("CASHFLOW_ENABLED=false: використано BASE_CONTRIBUTIONS_USDT.")
    values = dashboard_values(state, contributions)
    print("Панель:", json.dumps(values, ensure_ascii=False))
    if not env_bool("DASHBOARD_ENABLED", False):
        print("DASHBOARD_ENABLED=false: цифри панелі не змінено.")
        return
    for key, content in values.items():
        block_env, block_type = DASHBOARD_BLOCKS[key]
        block_id = required_env(block_env)
        response = requests.patch(
            f"https://api.notion.com/v1/blocks/{block_id}",
            headers=notion_headers(),
            json={block_type: {"rich_text": [{"type": "text", "text": {"content": content}}]}},
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(f"Notion dashboard API {response.status_code}: {response.text}")
    print("Головну панель оновлено.")


def fetch_cashflow_records(session: HTTP) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    calls = (("deposit", session.get_deposit_records), ("withdrawal", session.get_withdrawal_records))
    for kind, method in calls:
        response = method(coin="USDT", limit=50)
        if response.get("retCode") != 0:
            raise RuntimeError(f"Bybit {kind} API: {response.get('retMsg')}")
        for row in response.get("result", {}).get("rows", []):
            status = str(row.get("status", row.get("depositStatus", ""))).lower()
            successful = status in {"3", "success", "completed", "withdrawsuccess"}
            if not successful:
                continue
            record_id = str(row.get("withdrawId") or row.get("txID") or row.get("txId") or "")
            if not record_id:
                continue
            amount = Decimal(str(row.get("amount", "0")))
            fee = Decimal(str(row.get("withdrawFee", row.get("fee", "0")) or "0"))
            timestamp = row.get("successAt") or row.get("updateTime") or row.get("createTime")
            when = datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc).isoformat() if timestamp else datetime.now(timezone.utc).isoformat()
            records.append({"id": f"{kind}:{record_id}", "kind": kind, "amount": amount, "fee": fee, "date": when})

    # P2P purchases normally arrive in Funding first. Moving USDT into Unified
    # is the point at which it becomes trading capital. Track both directions
    # so moving the same money back to Funding reverses the contribution.
    if env_bool("BYBIT_INTERNAL_TRANSFERS_ENABLED", True):
        response = session.get_internal_transfer_records(coin="USDT", limit=50)
        if response.get("retCode") != 0:
            raise RuntimeError(f"Bybit internal transfer API: {response.get('retMsg')}")
        for row in response.get("result", {}).get("list", []):
            if str(row.get("status", "")).upper() != "SUCCESS":
                continue
            from_account = str(row.get("fromAccountType", "")).upper()
            to_account = str(row.get("toAccountType", "")).upper()
            if (from_account, to_account) == ("FUND", "UNIFIED"):
                kind = "deposit"
            elif (from_account, to_account) == ("UNIFIED", "FUND"):
                kind = "withdrawal"
            else:
                continue
            transfer_id = str(row.get("transferId", ""))
            if not transfer_id:
                continue
            timestamp = row.get("timestamp")
            when = (
                datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc).isoformat()
                if timestamp else datetime.now(timezone.utc).isoformat()
            )
            records.append({
                "id": f"internal:{transfer_id}",
                "kind": kind,
                "amount": Decimal(str(row.get("amount", "0"))),
                "fee": Decimal("0"),
                "date": when,
                "note": f"Bybit {from_account} → {to_account}",
            })
    return records


def write_cashflow(record: dict[str, Any]) -> None:
    deposit = record["kind"] == "deposit"
    amount = record["amount"]
    impact = amount if deposit else -(amount + record["fee"])
    label = "Поповнення" if deposit else "Виведення"
    short_id = record["id"].split(":", 1)[1][:12]
    notion_create(required_env(NOTION_CASHFLOW_DATABASE_ENV), {
        "Операція": {"title": [{"text": {"content": f"{label} {amount} USDT — {short_id}"}}]},
        "Дата": {"date": {"start": record["date"]}},
        "Тип": {"select": {"name": label}},
        "Сума USDT": {"number": float(amount)},
        "Статус": {"select": {"name": "Підтверджено"}},
        "Вплив на баланс USDT": {"number": float(impact)},
        "Примітка": {"rich_text": [{"text": {"content": f"{record.get('note', 'Автоматично імпортовано з Bybit')}. ID: {record['id']}"}}]},
    })


def sync_cashflow(session: HTTP, state: dict[str, Any]) -> int:
    if not env_bool("BYBIT_CASHFLOW_ENABLED", True):
        return 0
    records = fetch_cashflow_records(session)
    seen = set(state["seen_cashflow_ids"])
    if not state["cashflow_initialized"] and not env_bool("IMPORT_CASHFLOW_HISTORY", False):
        seen.update(record["id"] for record in records)
        state["seen_cashflow_ids"] = sorted(seen)
        state["cashflow_initialized"] = True
        save_state(state)
        print("Рух коштів: створено початкову точку, історію не дубльовано.")
        return 0
    added = 0
    for record in records:
        if record["id"] in seen:
            continue
        write_cashflow(record)
        seen.add(record["id"])
        added += 1
    state["seen_cashflow_ids"] = sorted(seen)
    state["cashflow_initialized"] = True
    save_state(state)
    print(f"Нових рухів коштів: {added}")
    return added


def bybit_session() -> HTTP:
    return HTTP(testnet=env_bool("BYBIT_TESTNET"), api_key=required_env("BYBIT_API_KEY"), api_secret=required_env("BYBIT_API_SECRET"))


def fetch_executions(session: HTTP | None = None) -> list[Execution]:
    category = os.getenv("BYBIT_CATEGORY", "spot").strip()
    symbol = os.getenv("BYBIT_SYMBOL", "SOLUSDT").strip().upper()
    hours = int(os.getenv("LOOKBACK_HOURS", "168"))
    start_ms = int((time.time() - hours * 3600) * 1000)

    session = session or bybit_session()
    response = session.get_executions(
        category=category,
        symbol=symbol,
        startTime=start_ms,
        limit=100,
    )
    if response.get("retCode") != 0:
        raise RuntimeError(
            f"Bybit API error {response.get('retCode')}: {response.get('retMsg')}"
        )

    rows = response.get("result", {}).get("list", [])
    return sorted((normalize(row) for row in rows), key=lambda item: item.executed_at_utc)


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)
    dry_run = env_bool("DRY_RUN", True)
    state = load_state()
    seen = set(state["seen_execution_ids"])
    session = bybit_session()
    executions = fetch_executions(session)
    new_items = [item for item in executions if item.execution_id not in seen]

    print(f"Знайдено виконань: {len(executions)}; нових: {len(new_items)}")
    for item in new_items:
        print(json.dumps(asdict(item), ensure_ascii=False, indent=2))

    if dry_run:
        if new_items:
            print("\nПопередній перегляд першого запису Notion:")
            preview = account_execution(new_items[0], state)
            print(json.dumps(notion_properties(preview), ensure_ascii=False, indent=2))
        print("DRY_RUN=true: стан не змінено, запис у Notion вимкнений.")
    else:
        if not STATE_PATH.exists() and not env_bool("IMPORT_HISTORY", False):
            seen.update(item.execution_id for item in executions)
            state["seen_execution_ids"] = sorted(seen)
            save_state(state)
            print(
                "Створено початкову точку: старі виконання позначено як оброблені, "
                "у Notion нічого не додано."
            )
            return 0

        written = 0
        for item in new_items:
            accounted = account_execution(item, state)
            page_id = write_to_notion(accounted)
            if page_id:
                written += 1
                state["open_notion_page_ids"].append(page_id)
            update_cycle_metrics(state, accounted)
            state["position_qty"] = str(accounted.position_qty_after)
            state["position_cost_usdt"] = str(accounted.position_cost_after)
            state["realized_profit_since_automation"] = str(
                Decimal(state["realized_profit_since_automation"]) + accounted.profit_usdt
            )
            if accounted.status == "CLOSED":
                set_notion_status(state["open_notion_page_ids"], "CLOSED")
                cycle_page_id = create_cycle_summary(state, accounted)
                cumulative = Decimal(os.getenv("BASE_REALIZED_PROFIT_USDT", "0")) + Decimal(
                    state["realized_profit_since_automation"]
                )
                upsert_weekly_report(
                    cycle_page_id, item.executed_at_utc, accounted.cycle,
                    Decimal(state["cycle_profit_usdt"]), cumulative,
                )
                state["open_notion_page_ids"] = []
                state["current_cycle"] = accounted.cycle + 1
                reset_cycle_metrics(state)
            seen.add(item.execution_id)
            state["seen_execution_ids"] = sorted(seen)
            save_state(state)
        print(f"Додано в Notion: {written}; дублі пропущено: {len(new_items) - written}")
        sync_cashflow(session, state)
        update_dashboard(state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Помилка: {exc}", file=sys.stderr)
        raise SystemExit(1)
