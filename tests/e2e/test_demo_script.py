"""The guided demo (``scripts/demo.py``) runs green against a real API and fails otherwise."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from tests.conftest import DEMO_PASSWORD, ROOT

spec = importlib.util.spec_from_file_location("demo_script", ROOT / "scripts" / "demo.py")
assert spec is not None and spec.loader is not None
demo = importlib.util.module_from_spec(spec)
sys.modules["demo_script"] = demo  # dataclass/annotation lookups need the module registered
spec.loader.exec_module(demo)


def test_demo_passes_end_to_end(live_api_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    with httpx.Client(base_url=live_api_url, timeout=30) as http:
        demo.Demo(http, DEMO_PASSWORD).run()
    out = capsys.readouterr().out
    assert "Demo finished" in out
    assert "cannot approve their own request, even as admin" in out
    assert "nothing ran before approval" in out


def test_main_reports_wrong_password_and_unreachable_api(
    live_api_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("API_BASE_URL", live_api_url)
    monkeypatch.setenv("SEED_DEMO_PASSWORD", "not-the-password-1")
    assert demo.main() == 1
    assert "make seed" in capsys.readouterr().err
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    assert demo.main() == 1
    assert "DEMO FAILED" in capsys.readouterr().err


def test_main_succeeds_with_the_right_password(
    live_api_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_BASE_URL", live_api_url)
    monkeypatch.setenv("SEED_DEMO_PASSWORD", DEMO_PASSWORD)
    assert demo.main() == 0


def test_demo_script_file_exists_where_the_makefile_expects_it() -> None:
    assert Path(ROOT / "scripts" / "demo.py").is_file()
