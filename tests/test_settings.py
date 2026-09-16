"""Configuration: what a deployment may set, and what is refused before anything runs.

Needs no host capability and no conftest fixture, so it runs on any workstation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.settings import Settings, UnknownSettingError, check_for_unknown_env_vars, load_settings

SERVICE_TOKEN = "environments-api-service-token-0123456789"
EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def make(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_settings_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVAPI_OPERATOR_ACCOUNTS", "a, b ,,c")
    monkeypatch.setenv("ENVAPI_MIN_SANDBOX_TIER", "USER")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.operator_accounts == ["a", "b", "c"] and settings.min_sandbox_tier == "user"
    monkeypatch.setenv("ENVAPI_MIN_SANDBOX_TIER", "bogus")
    with pytest.raises(ValueError, match="unknown sandbox tier"):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.delenv("ENVAPI_MIN_SANDBOX_TIER")
    assert Settings(_env_file=None, api_keys=["x"]).api_keys == ["x"]  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="comma-separated"):
        Settings(_env_file=None, api_keys=5)  # type: ignore[call-arg,arg-type]


def test_the_keyring_defaults_are_the_familys() -> None:
    settings = make()
    assert settings.keyring_base_url == settings.keyring_issuer == "http://127.0.0.1:8001"
    assert settings.keyring_service_name == "environments-api"
    assert settings.jwks_cache_seconds == 3600 and settings.jwks_min_refetch_seconds == 60


@pytest.mark.parametrize("name", ["jwks_cache_seconds", "jwks_min_refetch_seconds"])
@pytest.mark.parametrize("value", [0, -1, 10**9])
def test_key_fetch_timings_are_bounded(name: str, value: int) -> None:
    with pytest.raises(ValidationError):
        make(**{name: value})


# ----- unknown variables ---------------------------------------------------------------


def test_a_misspelled_variable_is_a_startup_error_naming_it_but_never_its_value() -> None:
    value = "a-value-that-was-meant-for-the-service-token"
    with pytest.raises(UnknownSettingError) as caught:
        check_for_unknown_env_vars(
            {
                "ENVAPI_KEYRING_SERVICE_TOKN": value,
                "envapi_jwks_cache_second": value,
                "ENVAPI_ROOT": "./data",
            }
        )
    message = str(caught.value)
    assert "ENVAPI_KEYRING_SERVICE_TOKN" in message and "envapi_jwks_cache_second" in message
    assert "ENVAPI_ROOT" not in message and value not in message


def test_recognised_and_unrelated_variables_pass() -> None:
    check_for_unknown_env_vars(
        {
            "ENVAPI_KEYRING_ISSUER": "http://127.0.0.1:8001",
            "envapi_jwks_min_refetch_seconds": "60",
            # Set by this service in every shell it starts, and read by scripts/smoke.sh.
            "ENVAPI_ENVIRONMENT_ID": "env_0123456789ab",
            "ENVAPI_URL": "http://127.0.0.1:8008",
            "PATH": "/usr/bin",
        }
    )


def test_the_settings_loader_checks_the_environment_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env to read
    assert load_settings().keyring_issuer == "http://127.0.0.1:8001"
    monkeypatch.setenv("ENVAPI_JWKS_CACHE_SECOND", "60")
    with pytest.raises(UnknownSettingError, match="ENVAPI_JWKS_CACHE_SECOND"):
        load_settings()


# ----- the keyring settings ------------------------------------------------------------


def test_an_empty_service_token_is_allowed() -> None:
    """Tokens still verify without one; only credential injection needs it."""
    assert make().keyring_service_token.get_secret_value() == ""


@pytest.mark.parametrize(
    "token", ["too-short-for-a-service-token", f" {SERVICE_TOKEN}", f"{SERVICE_TOKEN}\n"]
)
def test_a_service_token_keyring_would_refuse_is_refused_without_being_echoed(token: str) -> None:
    with pytest.raises(ValidationError) as caught:
        make(keyring_service_token=token)
    assert token.strip() not in f"{caught.value} {caught.value!r}"


def test_the_service_token_never_appears_in_a_repr_a_dump_or_an_error() -> None:
    settings = make(keyring_service_token=SERVICE_TOKEN)
    assert settings.keyring_service_token.get_secret_value() == SERVICE_TOKEN
    shown = f"{settings!r} {settings} {settings.model_dump()} {settings.model_dump_json()}"
    assert SERVICE_TOKEN not in shown
    with pytest.raises(ValidationError) as caught:
        make(keyring_service_token=SERVICE_TOKEN, jwks_cache_seconds=0)
    assert SERVICE_TOKEN not in f"{caught.value} {caught.value!r}"


@pytest.mark.parametrize("name", ["", " environments-api", "environments-api.shells"])
def test_an_audience_keyring_could_never_mint_is_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        make(keyring_service_name=name)


def test_the_example_file_is_valid_configuration() -> None:
    settings = Settings(_env_file=EXAMPLE)  # type: ignore[call-arg]
    assert settings.keyring_issuer == "http://127.0.0.1:8001"
    assert settings.jwks_cache_seconds == 3600 and settings.jwks_min_refetch_seconds == 60
    assert settings.settings_api is None


SETTINGS_API_TOKEN = "settings-api-token-for-environments-01"
SETTINGS_API_URL = "https://settings.test"


def test_settings_api_is_off_unless_configured() -> None:
    assert make().settings_api is None


def test_a_base_url_and_a_token_together_turn_settings_api_on() -> None:
    settings = make(settings_api_base_url=SETTINGS_API_URL, settings_api_token=SETTINGS_API_TOKEN)
    assert settings.settings_api is not None
    base_url, token = settings.settings_api
    assert base_url == SETTINGS_API_URL
    assert token.get_secret_value() == SETTINGS_API_TOKEN


@pytest.mark.parametrize(
    "half",
    [
        {"settings_api_base_url": SETTINGS_API_URL},
        {"settings_api_token": SETTINGS_API_TOKEN},
    ],
)
def test_half_a_settings_api_configuration_refuses_to_start(half: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="set together"):
        make(**half)


def test_a_blank_settings_api_url_means_off() -> None:
    assert make(settings_api_base_url="").settings_api_base_url is None


def test_an_empty_settings_api_token_object_means_off() -> None:
    from pydantic import SecretStr

    settings = make(settings_api_token=SecretStr(""))
    assert settings.settings_api_token is None


def test_a_short_settings_api_token_is_refused_without_being_echoed() -> None:
    with pytest.raises(ValidationError) as caught:
        make(settings_api_base_url=SETTINGS_API_URL, settings_api_token="short-token")
    messages = [error["msg"] for error in caught.value.errors()]
    assert messages
    assert all("short-token" not in message for message in messages)


def test_the_settings_api_token_never_appears_in_a_repr_a_dump_or_an_error() -> None:
    settings = make(settings_api_base_url=SETTINGS_API_URL, settings_api_token=SETTINGS_API_TOKEN)
    shown = f"{settings!r} {settings} {settings.model_dump()} {settings.model_dump_json()}"
    assert SETTINGS_API_TOKEN not in shown
    with pytest.raises(ValidationError) as caught:
        make(
            settings_api_base_url=SETTINGS_API_URL,
            settings_api_token=SETTINGS_API_TOKEN,
            jwks_cache_seconds=0,
        )
    assert SETTINGS_API_TOKEN not in f"{caught.value} {caught.value!r}"
