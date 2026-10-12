"""The fork's version is upstream's version plus a deploy counter, in one place.

Upstream (v0.6.0+) keeps the package version in ``tradingagents.__version__``, and
its own test rejects a ``+hf.N`` suffix there. The fork's ``+hf.N`` lives in the
web backend, which ``/api/health`` reports. The two must name the same base, or a
deployed build claims a version it is not. See docs/RELEASING.md.
"""

import re
from pathlib import Path

import pytest

import tradingagents

WEB_INIT = Path(__file__).resolve().parents[1] / "web" / "backend" / "app" / "__init__.py"
FORK_VERSION = re.compile(r"(?P<base>\d+\.\d+\.\d+)\+hf\.(?P<n>[1-9]\d*)")


def _web_version() -> str:
    # Read the text: the root suite must run without the web backend installed.
    found = re.search(r'^__version__\s*=\s*"([^"]+)"', WEB_INIT.read_text(), re.M)
    assert found, f"no __version__ in {WEB_INIT}"
    return found.group(1)


@pytest.mark.unit
def test_the_web_version_is_the_upstream_version_plus_a_fork_counter():
    assert WEB_INIT.is_file(), "the fixture this test reads is missing"
    match = FORK_VERSION.fullmatch(_web_version())
    assert match, f"{_web_version()!r} is not '<upstream>+hf.<N>'"
    assert match["base"] == tradingagents.__version__


@pytest.mark.unit
def test_the_fork_counter_is_not_in_the_package_version():
    # Upstream's tests/test_version.py enforces this too; it is the reason the
    # counter lives in the web backend, so the fork pins it from its own side.
    assert "+" not in tradingagents.__version__
