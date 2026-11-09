"""The package keeps the blueprint's twelve module boundaries."""

import importlib

import pytest

MODULES = [
    "domain",
    "market_data",
    "pricing",
    "volatility",
    "forecasts",
    "strategy",
    "selector",
    "risk",
    "simulator",
    "execution",
    "ledger",
    "evaluation",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    module = importlib.import_module(f"optlab.{name}")
    assert module.__doc__, f"optlab.{name} should say what it is for"
