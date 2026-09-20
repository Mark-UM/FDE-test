from app.core.config import Settings


def test_environment_overrides_dotenv(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=development\nDATABASE_URL=postgresql://file/example\n")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://environment/example")

    settings = Settings(_env_file=env_file)

    assert settings.app_env == "test"
    assert settings.database_url.get_secret_value() == "postgresql://environment/example"
    assert "postgresql://environment/example" not in repr(settings)


def test_database_configuration_is_optional(monkeypatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.database_url is None
