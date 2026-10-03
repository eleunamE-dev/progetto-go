import pytest
from alembic import command

from bookreviews.database import migrations_config

pytestmark = pytest.mark.integration


def test_the_models_match_the_migrations(database_url: str) -> None:
    command.check(migrations_config(database_url))


def test_the_migrations_can_be_rolled_back(database_url: str) -> None:
    config = migrations_config(database_url)

    command.downgrade(config, "base")
    command.upgrade(config, "head")
