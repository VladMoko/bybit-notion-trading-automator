import unittest
from decimal import Decimal

from sync import Execution, account_execution, fetch_cashflow_records, normalize


class CashflowSession:
    def get_deposit_records(self, **kwargs):
        return {"retCode": 0, "result": {"rows": []}}

    def get_withdrawal_records(self, **kwargs):
        return {"retCode": 0, "result": {"rows": []}}

    def get_internal_transfer_records(self, **kwargs):
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "transferId": "p2p-to-trading",
                        "coin": "USDT",
                        "amount": "1000",
                        "fromAccountType": "FUND",
                        "toAccountType": "UNIFIED",
                        "timestamp": "1790064000000",
                        "status": "SUCCESS",
                    },
                    {
                        "transferId": "trading-to-funding",
                        "coin": "USDT",
                        "amount": "25",
                        "fromAccountType": "UNIFIED",
                        "toAccountType": "FUND",
                        "timestamp": "1790067600000",
                        "status": "SUCCESS",
                    },
                    {
                        "transferId": "ignored-direction",
                        "coin": "USDT",
                        "amount": "5",
                        "fromAccountType": "FUND",
                        "toAccountType": "SPOT",
                        "timestamp": "1790067600000",
                        "status": "SUCCESS",
                    },
                ]
            },
        }


class NormalizeTests(unittest.TestCase):
    def test_internal_transfers_are_owner_cashflow(self):
        records = fetch_cashflow_records(CashflowSession())
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["id"], "internal:p2p-to-trading")
        self.assertEqual(records[0]["kind"], "deposit")
        self.assertEqual(records[0]["amount"], Decimal("1000"))
        self.assertEqual(records[1]["kind"], "withdrawal")

    def test_spot_execution(self):
        item = normalize(
            {
                "execId": "example-exec-id",
                "orderId": "example-order-id",
                "symbol": "SOLUSDT",
                "side": "Buy",
                "execPrice": "111.50",
                "execQty": "17.4208",
                "execValue": "1942.4192",
                "execFee": "0.0174208",
                "feeCurrency": "SOL",
                "feeRate": "0.001",
                "isMaker": True,
                "execTime": "1789823573000",
            }
        )
        self.assertEqual(item.symbol, "SOLUSDT")
        self.assertEqual(item.side, "Buy")
        self.assertEqual(item.price, "111.50")
        self.assertEqual(item.quantity, "17.4208")
        self.assertEqual(item.fee_currency, "SOL")
        self.assertTrue(item.is_maker)

    def test_weighted_average_cycle_and_profit(self):
        state = {
            "current_cycle": 7,
            "position_qty": "0",
            "position_cost_usdt": "0",
        }
        buy = Execution("b1", "o1", "SOLUSDT", "Buy", "100", "10", "1000", "0.01", "SOL", "0.001", True, "2026-01-01T00:00:00+00:00")
        bought = account_execution(buy, state)
        self.assertEqual(bought.position_qty_after, Decimal("9.99"))
        self.assertEqual(bought.position_cost_after, Decimal("1000"))

        state["position_qty"] = str(bought.position_qty_after)
        state["position_cost_usdt"] = str(bought.position_cost_after)
        sell = Execution("s1", "o2", "SOLUSDT", "Sell", "110", "9.99", "1098.9", "1.0989", "USDT", "0.001", True, "2026-01-02T00:00:00+00:00")
        sold = account_execution(sell, state)
        self.assertEqual(sold.status, "CLOSED")
        self.assertEqual(sold.position_qty_after, Decimal("0"))
        self.assertEqual(sold.profit_usdt, Decimal("97.8011"))


if __name__ == "__main__":
    unittest.main()
