import pytest

from config.dburl import database_from_url


def test_render_style_url_is_parsed():
    config = database_from_url("postgresql://app_user:s3cret@dpg-abc123-a/vacation_planner")
    assert config["ENGINE"] == "django.db.backends.postgresql"
    assert (config["NAME"], config["USER"], config["PASSWORD"]) == ("vacation_planner", "app_user", "s3cret")
    assert config["HOST"] == "dpg-abc123-a" and config["PORT"] == ""
    assert "OPTIONS" not in config


def test_port_special_characters_and_ssl_options():
    config = database_from_url("postgres://u:p%40ss%2Fword@db.example.com:5433/app?sslmode=require")
    assert config["PASSWORD"] == "p@ss/word"
    assert config["HOST"] == "db.example.com" and config["PORT"] == "5433"
    assert config["OPTIONS"] == {"sslmode": "require"}


def test_other_databases_are_refused():
    with pytest.raises(ValueError):
        database_from_url("mysql://u:p@host/db")
