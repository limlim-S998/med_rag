"""Optional configuration stays absent until a client needs it."""

from contextlib import AsyncExitStack

import pytest
from pydantic import ValidationError

from medw_core import azure
from medw_core.composition import build
from medw_core.settings import Settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    import os

    for key in os.environ:
        if key.startswith("MEDW_"):
            monkeypatch.delenv(key)


@pytest.mark.parametrize("blank", [None, "", " \t "])
def test_optional_values_normalize_legacy_blanks(blank):
    fields = ("aoai_endpoint", "aoai_resource_id", "search_endpoint", "cosmos_endpoint",
              "blob_account_url", "sql_server", "docintel_endpoint", "language_endpoint",
              "auth_jwks_url", "appinsights_connection_string")
    settings = Settings(_env_file=None, **dict.fromkeys(fields, blank))
    assert all(getattr(settings, name) is None for name in fields)


def test_existing_environment_names_and_precedence_work(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("MEDW_AOAI_ENDPOINT=https://dotenv.openai.azure.com\nMEDW_SEARCH_ENDPOINT=\n")
    monkeypatch.setenv("MEDW_AOAI_ENDPOINT", "https://environment.openai.azure.com")
    monkeypatch.setenv("MEDW_READINESS_TIMEOUT", "4")
    settings = Settings(_env_file=dotenv)
    assert settings.aoai_endpoint == "https://environment.openai.azure.com"
    assert settings.search_endpoint is None
    assert settings.readiness_timeout == 4
    assert Settings(_env_file=dotenv, readiness_timeout=6).readiness_timeout == 6
    monkeypatch.setenv("MEDW_READINESS_TIMEOUT", "31")
    with pytest.raises(ValidationError, match="readiness_timeout"):
        Settings(_env_file=dotenv)


@pytest.mark.parametrize("factory,field", [
    (azure.openai_client, "aoai_endpoint"),
    (azure.blob_client, "blob_account_url"),
    (azure.search_client, "search_endpoint"),
    (azure.cosmos_client, "cosmos_endpoint"),
    (azure.docintel_client, "docintel_endpoint"),
    (azure.language_client, "language_endpoint"),
])
def test_missing_endpoint_fails_with_its_environment_variable_name(factory, field):
    settings = Settings(_env_file=None)
    # No usable credential: configuration must fail before authentication work.
    with pytest.raises(ValueError, match="MEDW_" + field.upper()):
        factory(settings, object())


@pytest.mark.parametrize("endpoint", ["not-a-url", "http://example.test"])
def test_invalid_endpoint_fails_before_sdk_construction(monkeypatch, endpoint):
    import openai

    def forbidden(**kwargs):
        pytest.fail("SDK constructor must not receive an invalid endpoint")

    monkeypatch.setattr(openai, "AsyncAzureOpenAI", forbidden)
    settings = Settings(_env_file=None, aoai_endpoint=endpoint)
    with pytest.raises(ValueError, match="MEDW_AOAI_ENDPOINT"):
        azure.openai_client(settings, object())


async def test_gateway_does_not_use_openai_settings(monkeypatch):
    class CosmosBoundaryReached(Exception):
        pass

    def cosmos_client(settings, credential):
        raise CosmosBoundaryReached

    def forbidden(*args):
        pytest.fail("gateway must not construct an OpenAI client")

    monkeypatch.setattr(azure, "cosmos_client", cosmos_client)
    monkeypatch.setattr(azure, "openai_client", forbidden)
    settings = Settings(_env_file=None, aoai_endpoint="unused-invalid-url",
                        cosmos_endpoint="https://test.documents.azure.com",
                        sql_server="test.database.windows.net")
    async with AsyncExitStack() as stack:
        with pytest.raises(CosmosBoundaryReached):
            await build(settings, stack, service="gateway", credential=object())


async def test_generation_needs_real_storage_but_no_azure_model_client(monkeypatch):
    def forbidden(*args):
        pytest.fail("the installed placeholder must not construct an OpenAI client")

    monkeypatch.setattr(azure, "openai_client", forbidden)
    settings = Settings(_env_file=None,
                        aoai_endpoint="https://test.openai.azure.com")
    async with AsyncExitStack() as stack:
        with pytest.raises(ValueError, match="MEDW_COSMOS_ENDPOINT"):
            await build(settings, stack, service="generation", credential=object())


@pytest.mark.parametrize("field", ["sql_server", "sql_database"])
def test_sql_requires_server_and_database_before_creating_engine(monkeypatch, field):
    from medw_core import sql

    def forbidden(*args, **kwargs):
        pytest.fail("SQL engine must not receive missing connection settings")

    monkeypatch.setattr(sql, "create_async_engine", forbidden)
    settings = Settings(_env_file=None, **{
        "sql_server": "test.database.windows.net", field: "",
    })
    with pytest.raises(ValueError, match="MEDW_" + field.upper()):
        sql.engine(settings, credential=object())


async def test_placeholder_reranker_ignores_unused_azure_model_configuration():
    settings = Settings(_env_file=None, env="test",
                        aoai_endpoint="unused-invalid-url", cosmos_database="")
    async with AsyncExitStack() as stack:
        services = await build(settings, stack, service="reranker")
        await services.require("health").check()
