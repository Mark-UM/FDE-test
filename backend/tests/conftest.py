import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--sandbox-url",
        default=os.getenv("INTEGRATION_SANDBOX_URL"),
        help="Running DemoCommerce S0-S1 origin; enables real HTTP integration tests",
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
