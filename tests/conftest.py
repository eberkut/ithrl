"""Live native tests are dormant unless explicitly selected outside CI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from native_support.smoke import PROFILES, Profile, SmokeOptions


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup(
        "native smoke", "Separately approved, budget-limited native smoke tests"
    )
    group.addoption("--native-smoke", choices=("preflight", "run"), default=None)
    group.addoption("--native-profile", choices=PROFILES, default=None)
    group.addoption("--native-model", default=None)
    group.addoption("--native-provider", default=None)
    group.addoption("--native-smoke-root", default=None)
    group.addoption("--native-capability-digest", default=None)


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("native_smoke") is not None:
        _options(config)


def _options(config: pytest.Config) -> SmokeOptions:
    try:
        options = SmokeOptions(
            mode=config.getoption("native_smoke"),
            profile=cast(Profile, config.getoption("native_profile")),
            model=config.getoption("native_model") or "",
            provider=config.getoption("native_provider") or "",
            root=Path(config.getoption("native_smoke_root") or "."),
            capability_digest=config.getoption("native_capability_digest"),
        )
        options.validate(ci=bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")))
    except (OSError, TypeError, ValueError) as error:
        raise pytest.UsageError(str(error)) from None
    return options


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    mode = config.getoption("native_smoke")
    profile = config.getoption("native_profile")
    for item in items:
        marker = item.get_closest_marker("native_smoke")
        if marker is not None and (mode is None or marker.args[0] != profile):
            item.add_marker(
                pytest.mark.skip(reason="explicit native smoke opt-in and exact profile required")
            )


@pytest.fixture
def native_smoke_options(request: pytest.FixtureRequest) -> SmokeOptions:
    # Defence in depth for a direct fixture request or changed collection policy.
    if request.config.getoption("native_smoke") is None:
        pytest.skip("explicit native smoke opt-in required")
    return _options(request.config)
