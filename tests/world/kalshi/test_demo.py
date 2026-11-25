"""The demo executor, against responses constructed from Kalshi's OpenAPI schemas.

No demo credentials exist, so every private-endpoint response here is a
``constructed_*`` fixture: built by hand from the documented schemas and
labelled as such in the file.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pmfa.errors import ConfigError, ExchangeError
from pmfa.ports import Executor
from pmfa.records.decision import OrderIntent, OrderStatus
from pmfa.records.market import Side
from pmfa.world.kalshi.data import KalshiData
from pmfa.world.kalshi.demo import (
    DemoExecutor,
    demo_exchange,
    demo_executor,
    is_demo_url,
    order_payload,
)
from pmfa.world.kalshi.signing import SIGNATURE_HEADER
from tests.world.kalshi.fakes import DEMO, PROD, FakeKalshi, Sleeps, client, clock, config, fixture

TICKER = "KXFEDDECISION-26DEC-H0"
CLIENT_ID = "ord-5f1c2a9e0b7d3c41"
ORDER_ID = "3b23c1c7-f4ef-4f0d-8b9a-9e53c61f1a0d"
ORDERS = "/portfolio/events/orders"


class FakeSigner:
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {"KALSHI-ACCESS-KEY": "k", "KALSHI-ACCESS-TIMESTAMP": "1", SIGNATURE_HEADER: "s"}


def intent(side: Side = Side.NO, contracts: int = 10, limit: str = "0.96") -> OrderIntent:
    return OrderIntent(
        client_order_id=CLIENT_ID,
        episode_id="ep-1",
        proposal_id="prop-1",
        market_ticker=TICKER,
        side=side,
        contracts=contracts,
        limit_price=Decimal(limit),
    )


def executor(fake: FakeKalshi, sleeps: Sleeps | None = None) -> DemoExecutor:
    sleeps = sleeps or Sleeps()
    return DemoExecutor(
        client(fake, DEMO, signer=FakeSigner(), sleeps=sleeps),
        clock(),
        config(),
        sleep=sleeps.sleep,
    )


def trading_fake(*order_replies, groups: str = "constructed_order_groups_existing") -> FakeKalshi:
    fake = FakeKalshi()
    fake.add("GET", f"/markets/{TICKER}", "market_26DEC_H0")
    fake.add("GET", "/portfolio/order_groups", groups, subaccount=1)
    fake.add("POST", "/portfolio/order_groups/create", "constructed_order_group_created_201")
    if order_replies:
        fake.add("POST", ORDERS, *order_replies)
    fake.add(
        "GET", "/portfolio/fills", "constructed_fills_partial", order_id=ORDER_ID, subaccount=1
    )
    return fake


def sent_order(fake: FakeKalshi, index: int = 0) -> dict:
    return json.loads(fake.sent("POST", ORDERS)[index].content)


# -- construction


def test_it_refuses_any_host_but_the_demo_one() -> None:
    with pytest.raises(ConfigError):
        DemoExecutor(client(FakeKalshi(), PROD, signer=FakeSigner()), clock(), config())
    with pytest.raises(ConfigError):
        DemoExecutor(
            client(
                FakeKalshi(), "https://api.elections.kalshi.com/trade-api/v2", signer=FakeSigner()
            ),
            clock(),
            config(),
        )
    prod_data = KalshiData(client(FakeKalshi(), PROD), clock(), config())
    with pytest.raises(ConfigError):
        DemoExecutor(
            client(FakeKalshi(), DEMO, signer=FakeSigner()), clock(), config(), data=prod_data
        )
    assert is_demo_url(DEMO) and not is_demo_url(PROD)


def test_the_factory_refuses_a_production_url_in_the_configuration() -> None:
    cfg = config().model_copy(
        update={"kalshi": config().kalshi.model_copy(update={"demo_base_url": PROD})}
    )
    with pytest.raises(ConfigError):
        demo_exchange(cfg, clock())


def test_it_needs_a_signed_client() -> None:
    with pytest.raises(ConfigError):
        DemoExecutor(client(FakeKalshi(), DEMO), clock(), config())


def test_it_is_a_real_executor() -> None:
    ex = executor(FakeKalshi())
    assert isinstance(ex, Executor)
    assert ex.simulated is False


# -- order payloads


def test_buying_yes_is_a_bid_at_the_limit_price() -> None:
    body = order_payload(intent(Side.YES, 7, "0.05"), subaccount=1, order_group_id="og-1")
    assert body == {
        "ticker": TICKER,
        "client_order_id": CLIENT_ID,
        "side": "bid",
        "count": "7.00",
        "price": "0.0500",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "subaccount": 1,
        "order_group_id": "og-1",
    }


def test_buying_no_is_an_ask_on_the_yes_leg_at_one_minus_the_price() -> None:
    body = order_payload(intent(Side.NO, 10, "0.96"), subaccount=1, order_group_id=None)
    assert (body["side"], body["price"], body["count"]) == ("ask", "0.0400", "10.00")
    assert "order_group_id" not in body


# -- placing orders


def test_a_partial_fill_maps_to_an_order_result() -> None:
    fake = trading_fake("constructed_order_partial_201")
    result = executor(fake).place(intent())
    assert result.status is OrderStatus.PARTIALLY_FILLED
    assert (result.order_id, result.client_order_id) == (ORDER_ID, CLIENT_ID)
    assert result.requested_contracts == 10
    assert result.filled_contracts == Decimal("4")
    assert [f.price for f in result.fills] == [Decimal("0.9500"), Decimal("0.9600")]
    assert result.fees == Decimal("0.0200")
    assert all(f.side is Side.NO and not f.simulated for f in result.fills)
    assert result.error == ""
    order = sent_order(fake)
    assert (order["side"], order["price"], order["time_in_force"]) == (
        "ask",
        "0.0400",
        "immediate_or_cancel",
    )
    assert order["order_group_id"] == "og-7a1d"  # the existing group with the configured limit
    assert order["client_order_id"] == CLIENT_ID
    assert not fake.sent("POST", "/portfolio/order_groups/create")
    assert fake.sent("POST", ORDERS)[0].headers[SIGNATURE_HEADER] == "s"


def test_an_order_group_is_created_once_when_missing() -> None:
    fake = trading_fake("constructed_order_partial_201", groups="constructed_order_groups_empty")
    ex = executor(fake)
    ex.place(intent())
    ex.place(intent())
    (create,) = fake.sent("POST", "/portfolio/order_groups/create")
    assert json.loads(create.content) == {
        "subaccount": 1,
        "contracts_limit": 50,
        "exchange_index": 0,
    }
    assert {sent_order(fake, i)["order_group_id"] for i in (0, 1)} == {"og-new-1"}


def test_an_unfilled_order_reads_no_fills() -> None:
    unfilled = (201, {"order_id": ORDER_ID, "client_order_id": CLIENT_ID, "fill_count": "0.00",
                      "remaining_count": "0.00", "ts_ms": 1795705200123})  # fmt: skip
    fake = trading_fake(unfilled)
    result = executor(fake).place(intent(Side.YES, 5, "0.05"))
    assert result.status is OrderStatus.UNFILLED
    assert result.fills == []
    assert not fake.sent("GET", "/portfolio/fills")


def test_a_rejection_is_reported_not_raised() -> None:
    rejected = (400, {"error": {"code": "insufficient_balance", "message": "insufficient balance"}})
    result = executor(trading_fake(rejected)).place(intent())
    assert result.status is OrderStatus.REJECTED
    assert "insufficient_balance" in result.error
    assert result.fills == []


def test_a_duplicate_client_order_id_is_looked_up_not_resent() -> None:
    fake = trading_fake("constructed_order_already_exists_409")
    fake.add("GET", "/portfolio/orders", "constructed_orders_lookup", ticker=TICKER)
    result = executor(fake).place(intent())
    assert result.status is OrderStatus.PARTIALLY_FILLED
    assert result.order_id == ORDER_ID
    assert result.filled_contracts == Decimal("4")
    assert len(fake.sent("POST", ORDERS)) == 1


def test_an_unknown_outcome_is_never_retried_blindly() -> None:
    fake = trading_fake((500, {"error": {"code": "internal_server_error"}}))
    fake.add("GET", "/portfolio/orders", (200, {"orders": [], "cursor": ""}), ticker=TICKER)
    result = executor(fake).place(intent())
    assert result.status is OrderStatus.REJECTED
    assert "outcome unknown" in result.error
    assert len(fake.sent("POST", ORDERS)) == 1
    assert len(fake.sent("GET", "/portfolio/orders")) == 1


def test_lagging_fills_are_re_read_then_recorded_at_the_limit_price() -> None:
    fake = trading_fake("constructed_order_partial_201")
    fake.add(
        "GET",
        "/portfolio/fills",
        (200, {"fills": [], "cursor": ""}),
        order_id=ORDER_ID,
        subaccount=1,
    )
    sleeps = Sleeps()
    result = executor(fake, sleeps).place(intent())
    assert len(fake.sent("GET", "/portfolio/fills")) == 3
    assert sleeps.calls.count(0.5) == 2
    (fill,) = result.fills
    assert (fill.contracts, fill.price) == (Decimal("4.00"), Decimal("0.96"))
    assert fill.fee == Decimal("0.0050") * Decimal("4.00")
    assert "not yet reconciled" in result.error
    assert result.status is OrderStatus.PARTIALLY_FILLED


def test_invalid_intents_are_rejected_without_a_request() -> None:
    fake = FakeKalshi()
    result = executor(fake).place(intent(limit="1.00"))
    assert result.status is OrderStatus.REJECTED
    assert fake.requests == []


# -- holdings and cancels


def test_balance_and_positions_are_for_the_configured_subaccount() -> None:
    fake = FakeKalshi()
    fake.add("GET", "/portfolio/balance", "constructed_balance", subaccount=1)
    fake.add(
        "GET",
        "/portfolio/positions",
        "constructed_positions",
        subaccount=1,
        count_filter="position",
    )
    ex = executor(fake)
    balance = ex.balance()
    assert balance.cash == Decimal("995.2500")
    assert balance.equity == Decimal("999.5200")
    positions = ex.positions("KXFEDDECISION-26DEC")
    assert {p.market_ticker: p.signed for p in positions} == {
        "KXFEDDECISION-26DEC-H0": -4,
        "KXFEDDECISION-26DEC-C25": 2,
    }
    last = fake.sent("GET", "/portfolio/positions")[-1]
    assert last.url.params["event_ticker"] == "KXFEDDECISION-26DEC"


def test_cancel_all_counts_resting_orders_and_cancels_for_the_subaccount() -> None:
    fake = FakeKalshi()
    fake.add(
        "GET", "/portfolio/orders", "constructed_orders_resting", status="resting", subaccount=1
    )
    fake.add("DELETE", ORDERS, (204, None))
    assert executor(fake).cancel_all() == 2
    (delete,) = fake.sent("DELETE", ORDERS)
    assert delete.url.params["subaccount"] == "1"


def test_execution_book_comes_from_the_demo_host() -> None:
    fake = FakeKalshi().add("GET", f"/markets/{TICKER}/orderbook", "demo_orderbook_26DEC_H0")
    book = executor(fake).execution_book(TICKER)
    assert book.best_bid(Side.YES) is not None
    assert fake.requests[0].url.host == "external-api.demo.kalshi.co"


# -- factories


def test_demo_executor_needs_credentials() -> None:
    with pytest.raises(ConfigError):
        demo_executor(config(), clock(), environ={})


def test_demo_executor_signs_private_requests_and_reads_data_unsigned() -> None:
    cfg = config()
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    env = {cfg.kalshi.demo_key_id_env: "key-1", cfg.kalshi.demo_private_key_env: pem.decode()}
    fake = FakeKalshi()
    fake.add("GET", "/portfolio/balance", "constructed_balance")
    fake.add("GET", "/portfolio/positions", "constructed_positions")
    fake.add("GET", f"/markets/{TICKER}/orderbook", "demo_orderbook_26DEC_H0")
    sleeps = Sleeps()
    ex = demo_executor(
        cfg,
        clock(),
        environ=env,
        transport=fake.transport(),
        sleep=sleeps.sleep,
        monotonic=sleeps.monotonic,
    )
    ex.balance()
    ex.execution_book(TICKER)
    private = fake.sent("GET", "/portfolio/balance")[0]
    public = fake.sent("GET", f"/markets/{TICKER}/orderbook")[0]
    assert private.headers["KALSHI-ACCESS-KEY"] == "key-1"
    assert "KALSHI-ACCESS-SIGNATURE" not in public.headers
    assert all(r.url.host == "external-api.demo.kalshi.co" for r in fake.requests)


def test_demo_exchange_reads_the_demo_host() -> None:
    fake = FakeKalshi().add("GET", "/series/KXFEDDECISION", "series_KXFEDDECISION")
    sleeps = Sleeps()
    exchange = demo_exchange(
        config(),
        clock(),
        transport=fake.transport(),
        sleep=sleeps.sleep,
        monotonic=sleeps.monotonic,
    )
    assert exchange.get_series("KXFEDDECISION").fee_type == "quadratic_with_maker_fees"
    assert exchange.client.signer is None
    assert isinstance(fake.requests[0], httpx.Request)
    assert fake.requests[0].url.host == "external-api.demo.kalshi.co"
    assert fixture("demo_orderbook_26DEC_H0")[0] == 200


def test_cancel_all_still_cancels_when_the_count_cannot_be_read() -> None:
    fake = FakeKalshi()
    fake.add("GET", "/portfolio/orders", (400, {"error": {"code": "bad_request"}}))
    fake.add("DELETE", ORDERS, (204, None))
    with pytest.raises(ExchangeError):
        executor(fake).cancel_all()
    assert len(fake.sent("DELETE", ORDERS)) == 1


# -- review fixes: distinct rejection ids, a failed fills read, the halt flag


def test_rejected_orders_get_distinct_deterministic_order_ids() -> None:
    rejected = (400, {"error": {"code": "insufficient_balance", "message": "insufficient balance"}})
    ex = executor(trading_fake(rejected))
    first = ex.place(intent())
    second = ex.place(intent().model_copy(update={"client_order_id": "ord-other"}))
    again = ex.place(intent())
    assert first.status is second.status is OrderStatus.REJECTED
    assert first.order_id and second.order_id
    assert first.order_id != second.order_id
    assert again.order_id == first.order_id  # deterministic: the same intent, the same key
    invalid = ex.place(intent(limit="1.00").model_copy(update={"client_order_id": "ord-bad"}))
    assert invalid.order_id not in {"", first.order_id, second.order_id}


def test_a_failed_fills_read_after_an_accepted_order_still_records_the_order() -> None:
    fake = trading_fake("constructed_order_partial_201")
    fake.add(
        "GET",
        "/portfolio/fills",
        (503, {"error": {"code": "service_unavailable", "message": "unavailable"}}),
        order_id=ORDER_ID,
        subaccount=1,
    )
    result = executor(fake).place(intent())
    assert len(fake.sent("POST", ORDERS)) == 1
    assert (result.order_id, result.client_order_id) == (ORDER_ID, CLIENT_ID)
    assert result.status is OrderStatus.PARTIALLY_FILLED
    (fill,) = result.fills
    assert (fill.contracts, fill.price) == (Decimal("4.00"), Decimal("0.96"))
    assert "fills could not be read" in result.error


def test_a_halted_executor_sends_no_order() -> None:
    from pmfa.records.episode import HaltState

    flag = {"halted": False}

    def halt_check() -> HaltState:
        return HaltState(halted=flag["halted"], reason="manual" if flag["halted"] else "")

    fake = trading_fake("constructed_order_partial_201")
    ex = executor(fake)
    ex.halt_check = halt_check
    flag["halted"] = True
    result = ex.place(intent())
    assert result.status is OrderStatus.REJECTED
    assert result.error.startswith("halted")
    assert not fake.sent("POST", ORDERS)
    flag["halted"] = False
    assert ex.place(intent()).status is OrderStatus.PARTIALLY_FILLED
    assert len(fake.sent("POST", ORDERS)) == 1


def test_the_halt_flag_is_read_again_right_before_the_order_is_sent() -> None:
    from pmfa.records.episode import HaltState

    reads: list[int] = []

    def halt_check() -> HaltState:
        # Clear at the first read, set by the time the order would be sent (as `pmfa halt`
        # from another terminal would while the order group is being looked up).
        reads.append(1)
        return HaltState(halted=len(reads) > 1, reason="manual")

    fake = trading_fake("constructed_order_partial_201")
    ex = DemoExecutor(
        client(fake, DEMO, signer=FakeSigner(), sleeps=Sleeps()),
        clock(),
        config(),
        sleep=Sleeps().sleep,
        halt_check=halt_check,
    )
    result = ex.place(intent())
    assert result.status is OrderStatus.REJECTED and result.error.startswith("halted")
    assert not fake.sent("POST", ORDERS)


def test_repeated_demo_rejections_trip_the_fill_failure_halt() -> None:
    from pmfa.execution.halt import stop_reasons
    from pmfa.state.store import MEMORY, SqliteStore
    from pmfa.world.recorded import RecordedEvidence
    from tests.world.recorded.factories import world_file

    rejected = (400, {"error": {"code": "insufficient_balance", "message": "insufficient balance"}})
    ex = executor(trading_fake(rejected))
    store = SqliteStore(MEMORY)
    for i in range(10):
        # Saved as the pipeline saves every rejection that is not a halt.
        store.save_order(ex.place(intent().model_copy(update={"client_order_id": f"cid-{i}"})))
    assert len(store.list_orders()) == 10
    found = stop_reasons(store, config(), clock(), RecordedEvidence(world_file()))
    assert [r.split(":")[0] for r in found] == ["fill failures"]
