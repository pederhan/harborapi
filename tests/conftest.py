import os
from pathlib import Path

import pytest
from _pytest.logging import LogCaptureFixture
from hypothesis import Verbosity, settings
from loguru import logger

from harborapi.client import HarborAsyncClient

from .strategies import init_strategies

# Init custom hypothesis strategies
init_strategies()

# Hypothesis profiles
settings.register_profile("ci", settings(max_examples=1000))
settings.register_profile(
    "debug",
    settings(
        max_examples=10,
        verbosity=Verbosity.verbose,
    ),
)
settings.register_profile("dev", max_examples=10)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))


# must be set to "function" to make sure logging is enabled for each test
@pytest.fixture(scope="function")
def async_client() -> HarborAsyncClient:
    return HarborAsyncClient(
        username="username",
        secret="secret",
        url="http://localhost",
        logging=True,
    )


@pytest.fixture
def caplog(caplog: LogCaptureFixture):
    # https://loguru.readthedocs.io/en/stable/resources/migration.html#making-things-work-with-pytest-and-caplog
    handler_id = logger.add(caplog.handler, format="{message}")
    yield caplog
    logger.remove(handler_id)


@pytest.fixture(scope="function")
def credentials_file(tmp_path: Path) -> Path:
    """Create a credentials file for testing"""
    credentials_file = tmp_path / "credentials.json"
    credentials_file.write_text(
        """
{
    "creation_time": "2022-07-01T13:20:46.230Z",
    "description": "Some description",
    "disable": false,
    "duration": 30,
    "editable": true,
    "expires_at": 1659273646,
    "id": 1,
    "level": "system",
    "name": "robot$harborapi-test",
    "permissions": [
        {
            "access": [
                {
                    "action": "list",
                    "resource": "repository"
                },
                {
                    "action": "pull",
                    "resource": "repository"
                }
            ],
            "kind": "project",
            "namespace": "*"
        }
    ],
    "update_time": "2022-07-06T13:26:45.360Z",
    "permissionScope": {
        "coverAll": true,
        "access": [
            {
                "action": "list",
                "resource": "repository"
            },
            {
                "action": "pull",
                "resource": "repository"
            }
        ]
    },
    "secret": "bad-password"
}
        """
    )
    return credentials_file
