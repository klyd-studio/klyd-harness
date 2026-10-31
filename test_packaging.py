"""Guards the installed-package layout.

The 0.2.2 wheel shipped without the hook templates or the SQL schema because
both lived at the repository root, outside the `klyd` package. `pip install
klyd` worked, but `kl init` then failed with FileNotFoundError. These tests
fail if that layout ever comes back.
"""

from klyd.db import get_schema_path
from klyd.hooks import get_template_path


def test_schema_ships_inside_package():
    path = get_schema_path()
    assert path.is_file(), f"missing schema at {path}"
    assert path.read_text().strip().startswith("CREATE TABLE")


def test_hook_templates_ship_inside_package():
    for hook in ("post-commit", "pre-commit"):
        path = get_template_path(hook)
        assert path.is_file(), f"missing hook template at {path}"
        assert path.read_text().startswith("#!/bin/sh")
