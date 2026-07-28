from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_and_scripts_contain_no_order_websocket_or_live_order_calls() -> None:
    forbidden = (
        "FyersOrderSocket",
        "order_ws",
        ".place_order(",
        ".place_basket_orders(",
        "live_order_adapter",
    )
    violations: list[str] = []
    for source_root in (ROOT / "src" / "stage1", ROOT / "scripts"):
        for path in source_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                if marker in text:
                    violations.append(f"{path.relative_to(ROOT)}: {marker}")
    assert not violations
