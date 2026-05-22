import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "db: mark test as requiring a real DuckDB connection (skip with -m 'not db')"
    )
