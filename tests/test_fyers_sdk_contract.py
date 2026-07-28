import inspect
import json
from pathlib import Path

from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws


def test_installed_fyers_data_socket_contract_matches_adapter() -> None:
    subscribe_parameters = inspect.signature(
        data_ws.FyersDataSocket.subscribe
    ).parameters
    assert "symbols" in subscribe_parameters
    assert subscribe_parameters["data_type"].default == "SymbolUpdate"
    assert hasattr(data_ws.FyersDataSocket, "close_connection")

    map_path = Path(data_ws.__file__).with_name("map.json")
    sdk_mapping = json.loads(map_path.read_text(encoding="utf-8"))
    required_equity_fields = {
        "symbol",
        "ltp",
        "exch_feed_time",
        "vol_traded_today",
        "bid_price",
        "ask_price",
        "bid_size",
        "ask_size",
        "last_traded_qty",
    }
    required_index_fields = {"symbol", "ltp", "exch_feed_time"}
    assert required_equity_fields <= set(sdk_mapping["data_val"])
    assert required_index_fields <= set(sdk_mapping["index_val"])


def test_installed_fyers_manual_oauth_contract_matches_helper() -> None:
    parameters = inspect.signature(fyersModel.SessionModel).parameters
    assert {
        "client_id",
        "redirect_uri",
        "response_type",
        "state",
        "secret_key",
        "grant_type",
    } <= set(parameters)
    assert hasattr(fyersModel.SessionModel, "generate_authcode")
    assert hasattr(fyersModel.SessionModel, "set_token")
    assert hasattr(fyersModel.SessionModel, "generate_token")

