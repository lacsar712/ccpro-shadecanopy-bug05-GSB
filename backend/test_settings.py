"""SQLite settings for running the backend test-suite without PostgreSQL."""
import os

from config.settings import *  # noqa: F401,F403

# File-based (not :memory:) so concurrent connections/threads in the race
# tests share the same database and its locks.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("TEST_SQLITE_PATH", "/tmp/shadecanopy_test.sqlite3"),
        # File-based test DB (not the default in-memory one) so concurrent
        # connections/threads in the race tests share the same database/locks.
        "TEST": {"NAME": os.environ.get("TEST_SQLITE_PATH", "/tmp/shadecanopy_test.sqlite3")},
        # Wait out a concurrent writer's short transaction instead of failing
        # immediately with "database is locked" in the parallel-submit test.
        "OPTIONS": {"timeout": 10},
    }
}
