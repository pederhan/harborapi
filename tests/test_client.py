import asyncio
from pathlib import Path

import pytest
from httpx import HTTPStatusError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from pytest_httpserver import HTTPServer

from harborapi.client import HarborAsyncClient, construct_model
from harborapi.exceptions import (
    BadRequest,
    Forbidden,
    InternalServerError,
    NotFound,
    PreconditionFailed,
    StatusError,
    Unauthorized,
)
from harborapi.models import Error, Errors, UserResp
from harborapi.utils import get_credentials

from .strategies import errors_strategy


# TODO: parametrize this to test both clients
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://harbor.example.com", "https://harbor.example.com/api/v2.0"),
        ("https://harbor.example.com/api", "https://harbor.example.com/api/v2.0"),
        ("https://harbor.example.com/api/", "https://harbor.example.com/api/v2.0"),
        ("https://harbor.example.com/api/v2.0/", "https://harbor.example.com/api/v2.0"),
        # should have regex to check for valid URL, as this likely isn't a valid URL vvvv
        ("https://harbor.example.com/api/v", "https://harbor.example.com/api/v"),
    ],
)
def test_client_init_url(url: str, expected: str):
    # manually set version to v2.0 for this test
    client = HarborAsyncClient(
        username="username", secret="secret", url=url, version="v2.0"
    )
    assert client.url == expected


def test_client_init_username():
    client = HarborAsyncClient(
        username="username", secret="secret", url="https://harbor.example.com"
    )
    assert client.url == "https://harbor.example.com/api/v2.0"
    assert client.credentials == get_credentials("username", "secret")


def test_client_init_token():
    client = HarborAsyncClient(
        credentials="dXNlcm5hbWU6c2VjcmV0", url="https://harbor.example.com"
    )
    assert client.url == "https://harbor.example.com/api/v2.0"
    assert client.credentials == get_credentials("username", "secret")


def test_client_init_credentials_file(credentials_file: Path):
    client = HarborAsyncClient(
        credentials_file=credentials_file, url="https://harbor.example.com"
    )
    assert client.url == "https://harbor.example.com/api/v2.0"
    assert client.credentials == get_credentials("robot$harborapi-test", "bad-password")


def test_client_init_no_credentials():
    with pytest.raises(ValueError):
        HarborAsyncClient(url="https://harbor.example.com")


@pytest.mark.asyncio
async def test_get_pagination_mock(
    async_client: HarborAsyncClient, httpserver: HTTPServer
):
    """Test pagination by mocking paginated /users results."""
    httpserver.expect_oneshot_request("/api/v2.0/users").respond_with_json(
        [{"username": "user1"}, {"username": "user2"}],
        headers={"link": '</api/v2.0/users?page=2>; rel="next"'},
    )
    httpserver.expect_oneshot_request(
        "/api/v2.0/users", query_string="page=2"
    ).respond_with_json([{"username": "user3"}, {"username": "user4"}])
    async_client.url = httpserver.url_for("/api/v2.0")
    users = await async_client.get("/users")  # type: ignore
    assert isinstance(users, list)
    assert len(users) == 4
    assert users[0]["username"] == "user1"
    assert users[1]["username"] == "user2"
    assert users[2]["username"] == "user3"
    assert users[3]["username"] == "user4"


@pytest.mark.asyncio
async def test_get_pagination_large_mock(
    async_client: HarborAsyncClient, httpserver: HTTPServer
):
    """Test pagination with a large number of pages (100)."""
    httpserver.expect_oneshot_request("/api/v2.0/users").respond_with_json(
        [{"username": "user1"}],
        headers={"link": '</api/v2.0/users?page=2>; rel="next"'},
    )
    N_PAGES = 100
    for page in range(2, N_PAGES + 1):
        if page == N_PAGES:
            headers = None
        else:
            headers = {"link": f'</api/v2.0/users?page={page+1}>; rel="next"'}
        httpserver.expect_oneshot_request(
            "/api/v2.0/users", query_string=f"page={page}"
        ).respond_with_json([{"username": f"user{page}"}], headers=headers)

    async_client.url = httpserver.url_for("/api/v2.0")
    users = await async_client.get("/users")  # type: ignore
    assert isinstance(users, list)
    assert len(users) == 100


@pytest.mark.asyncio
async def test_get_pagination_no_follow(
    async_client: HarborAsyncClient, httpserver: HTTPServer
):
    """Test pagination by mocking paginated /users results."""
    httpserver.expect_oneshot_request("/api/v2.0/users").respond_with_json(
        [{"username": "user1"}, {"username": "user2"}],
        headers={"link": "/users?page=2"},
    )
    async_client.url = httpserver.url_for("/api/v2.0")
    users = await async_client.get("/users", follow_links=False)  # type: ignore
    assert isinstance(users, list)
    assert len(users) == 2
    assert users[0]["username"] == "user1"
    assert users[1]["username"] == "user2"


@pytest.mark.asyncio
async def test_get_pagination_invalid_mock(
    async_client: HarborAsyncClient,
    httpserver: HTTPServer,
    caplog: pytest.LogCaptureFixture,
):
    """Test pagination where subsequent page returns non-list results."""
    httpserver.expect_oneshot_request("/api/v2.0/users").respond_with_json(
        [{"username": "user1"}, {"username": "user2"}],
        headers={"link": '</api/v2.0/users?page=2>; rel="next"'},
    )
    # Next page does not return a list, so we ignore it
    httpserver.expect_oneshot_request(
        "/api/v2.0/users", query_string="page=2"
    ).respond_with_json({"username": "user3"})
    async_client.url = httpserver.url_for("/api/v2.0")
    users = await async_client.get("/users")  # type: ignore
    assert isinstance(users, list)
    assert len(users) == 2
    assert users[0]["username"] == "user1"
    assert users[1]["username"] == "user2"
    assert "Unable to handle paginated results" in caplog.text


@pytest.mark.asyncio
async def test_get_retry_mock(async_client: HarborAsyncClient, httpserver: HTTPServer):
    """Test retry by mocking a server that is initially down, then comes up."""
    httpserver.stop()

    # this is a little hacky for now:
    # we schedule the server to start after a few seconds
    async def start_server():
        await asyncio.sleep(2)  # can be increased, but wastes CI run time
        httpserver.start()

    asyncio.create_task(start_server())

    httpserver.expect_oneshot_request("/api/v2.0/users").respond_with_json(
        [{"username": "user1"}, {"username": "user2"}],
    )

    async_client.url = httpserver.url_for("/api/v2.0")
    users = await async_client.get("/users")
    assert isinstance(users, list)
    assert len(users) == 2


@pytest.mark.asyncio
async def test_construct_model():
    # TODO: test create_model with all models
    m = construct_model(UserResp, {"username": "user1"})
    assert isinstance(m, UserResp)
    assert m.username == "user1"

    # Invalid value for "username"
    with pytest.raises(ValidationError) as e:
        construct_model(UserResp, {"username": {}})
    assert e.value.errors()[0]["loc"] == ("username",)
    assert len(e.value.errors()) == 1


@pytest.mark.asyncio
async def test_get_invalid_data(
    async_client: HarborAsyncClient, httpserver: HTTPServer
):
    """Tests handling of data from the server that does not match the schema."""
    httpserver.expect_oneshot_request("/api/v2.0/users").respond_with_json(
        [{"username": {}}, {"username": "user2"}],
    )

    async_client.url = httpserver.url_for("/api/v2.0")
    with pytest.raises(ValidationError) as e:
        await async_client.get_users()
    assert e.value.errors()[0]["loc"] == ("username",)
    assert len(e.value.errors()) == 1


@pytest.mark.asyncio
@given(errors_strategy)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
async def test_get_errors(
    async_client: HarborAsyncClient, httpserver: HTTPServer, errors: Errors
):
    """Tests handling of data from the server that does not match the schema."""
    httpserver.expect_oneshot_request("/api/v2.0/errorpath").respond_with_json(
        errors.dict(), status=500
    )

    async_client.url = httpserver.url_for("/api/v2.0")
    with pytest.raises(StatusError) as exc_info:
        await async_client.get("/errorpath")
    assert exc_info is not None

    e = exc_info.value
    assert isinstance(e, StatusError)
    assert isinstance(e.errors, list)
    if e.errors:
        for error in e.errors:
            assert isinstance(error, Error)

    assert isinstance(e.__cause__, HTTPStatusError)
    assert e.__cause__.request.url == async_client.url + "/errorpath"
    assert e.__cause__.response.status_code == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        # "HEAD"
        # "OPTIONS"
    ],
)
@pytest.mark.parametrize(
    "username,secret,credentials",
    [
        ("user", "secret", ""),
        ("", "", "credentials"),
        ("user", "secret", "credentials"),
        # ("", "", ""), # TODO: handle empty
    ],
)
async def test_authentication(
    httpserver: HTTPServer, method: str, username: str, secret: str, credentials: str
):
    """Tests handling of data from the server that does not match the schema."""

    client = HarborAsyncClient(
        url=httpserver.url_for("/api/v2.0"),
        username=username,
        secret=secret,
        credentials=credentials,
    )

    # username/password takes precedence over credentials
    if username and secret:
        expect_credentials = get_credentials(username, secret)
    else:
        expect_credentials = credentials
    assert client.credentials == expect_credentials

    # Set up HTTP server to expect a certain set of headers and a method
    httpserver.expect_oneshot_request(
        "/api/v2.0/foo",
        headers={
            "Authorization": f"Basic {expect_credentials}",
            "Accept": "application/json",
        },
        method=method,
    ).respond_with_data()

    if method == "GET":
        await client.get("/foo")
    elif method == "POST":
        await client.post("/foo", json={"foo": "bar"})
    elif method == "PUT":
        await client.put("/foo", json={"foo": "bar"})
    elif method == "PATCH":
        await client.patch("/foo", json={"foo": "bar"})
    elif method == "DELETE":
        await client.delete("/foo")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code",
    [400, 401, 403, 404, 412, 500],
)
@pytest.mark.parametrize(
    "method",
    [
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        # "HEAD"
        # "OPTIONS"
    ],
)
async def test_exceptions(
    async_client: HarborAsyncClient,
    httpserver: HTTPServer,
    status_code: int,
    method: str,
):
    httpserver.expect_oneshot_request(
        "/api/v2.0/exceptions", method=method
    ).respond_with_data(status=status_code)

    async_client.url = httpserver.url_for("/api/v2.0")
    exceptions = {
        400: BadRequest,
        401: Unauthorized,
        403: Forbidden,
        404: NotFound,
        412: PreconditionFailed,
        500: InternalServerError,
    }
    with pytest.raises(exceptions[status_code]) as exc_info:
        if method == "GET":
            await async_client.get("/exceptions")
        elif method == "POST":
            await async_client.post("/exceptions", json={"foo": "bar"})
        elif method == "PUT":
            await async_client.put("/exceptions", json={"foo": "bar"})
        elif method == "PATCH":
            await async_client.patch("/exceptions", json={"foo": "bar"})
        elif method == "DELETE":
            await async_client.delete("/exceptions")
    assert exc_info.value.status_code == status_code
