"""Tests for config.py - environment variable loading.

These tests never touch the real filesystem: `_load_env` resolves
`~/.auto-epub/.env` through `Path.home()` / `Path.exists()`, both of which are
patched to a virtual path, and `load_dotenv` is patched too. That keeps the
suite free of the temp-directory permission problems the real home directory
would introduce, and guarantees no real `API_KEY` is ever read.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_epub.config import _ModelProvider, _load_env, get_model_provider

ENV_KEYS = ("API_BASE_URL", "API_KEY", "API_MODEL")

VALID_ENV = {
    "API_BASE_URL": "https://api.example.com/v1",
    "API_KEY": "test-key",
    "API_MODEL": "test-model",
}


@pytest.fixture
def clean_env(monkeypatch):
    """Start every test from an unconfigured environment.

    monkeypatch records the pre-test value of every key it touches, so the
    mutations `_load_env` makes through `load_dotenv` are undone at teardown.
    """
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def virtual_home(monkeypatch):
    """Point `Path.home()` at a path that does not exist on disk."""
    home = Path("/nonexistent-home")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _dotenv(**values):
    """Build a `load_dotenv` side effect that writes `values` into os.environ."""
    values = {k: v for k, v in values.items() if v is not None}

    def _side_effect(dotenv_path=None, override=False, **_kwargs):
        assert override is True, "existing env vars must be overridden"
        for key, value in values.items():
            os.environ[key] = value

    return _side_effect


class TestModelProvider:
    """Tests for _ModelProvider dataclass."""

    def test_model_provider_creation(self):
        """Test creating a _ModelProvider instance."""
        provider = _ModelProvider(
            base_url="https://api.example.com/v1",
            api_key="test-key",
            model="test-model",
        )
        assert provider.base_url == "https://api.example.com/v1"
        assert provider.api_key == "test-key"
        assert provider.model == "test-model"

    def test_model_provider_empty_strings(self):
        """Test _ModelProvider with empty strings."""
        provider = _ModelProvider(base_url="", api_key="", model="")
        assert provider.base_url == ""
        assert provider.api_key == ""
        assert provider.model == ""


class TestLoadEnv:
    """Tests for _load_env function."""

    def test_load_env_no_file(self, clean_env, virtual_home):
        """Missing .env file: nothing is loaded, empty triple is returned."""
        with (
            patch.object(Path, "exists", return_value=False),
            patch("auto_epub.config.load_dotenv") as mock_load,
        ):
            assert _load_env() == ("", "", "")

        mock_load.assert_not_called()

    def test_load_env_with_file(self, clean_env, virtual_home):
        """Values from ~/.auto-epub/.env are returned and reach os.environ."""
        with (
            patch.object(Path, "exists", return_value=True),
            patch(
                "auto_epub.config.load_dotenv", side_effect=_dotenv(**VALID_ENV)
            ) as mock_load,
        ):
            assert _load_env() == (
                VALID_ENV["API_BASE_URL"],
                VALID_ENV["API_KEY"],
                VALID_ENV["API_MODEL"],
            )

        expected_path = virtual_home / ".auto-epub" / ".env"
        mock_load.assert_called_once_with(dotenv_path=expected_path, override=True)
        assert os.environ["API_KEY"] == VALID_ENV["API_KEY"]

    def test_load_env_override(self, clean_env, virtual_home, monkeypatch):
        """A pre-set env var is replaced by the .env value (override=True)."""
        monkeypatch.setenv("API_KEY", "stale-key")

        with (
            patch.object(Path, "exists", return_value=True),
            patch("auto_epub.config.load_dotenv", side_effect=_dotenv(**VALID_ENV)),
        ):
            assert _load_env()[1] == VALID_ENV["API_KEY"]

        assert os.environ["API_KEY"] == VALID_ENV["API_KEY"]

    def test_load_env_path_is_fixed_under_home(self, clean_env, monkeypatch):
        """The path is always ~/.auto-epub/.env, independent of cwd/env_file."""
        home = Path("/some-home")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.chdir(Path("/"))  # never resolve against the cwd

        with (
            patch.object(Path, "exists", return_value=True),
            patch("auto_epub.config.load_dotenv") as mock_load,
        ):
            _load_env()

        dotenv_path = mock_load.call_args.kwargs["dotenv_path"]
        assert dotenv_path == Path("/some-home/.auto-epub/.env")


class TestGetModelProvider:
    """Tests for get_model_provider function."""

    def test_get_model_provider_unconfigured(self, clean_env):
        """No .env at all is a hard error, not a silently empty provider."""
        with patch("auto_epub.config._load_env", return_value=("", "", "")):
            with pytest.raises(ValueError, match="auto-epub"):
                get_model_provider()

    def test_get_model_provider_from_env(self, clean_env):
        """Should read values from .env file."""
        with patch(
            "auto_epub.config._load_env",
            return_value=(
                VALID_ENV["API_BASE_URL"],
                VALID_ENV["API_KEY"],
                VALID_ENV["API_MODEL"],
            ),
        ):
            provider = get_model_provider()

        assert provider == _ModelProvider(
            base_url=VALID_ENV["API_BASE_URL"],
            api_key=VALID_ENV["API_KEY"],
            model=VALID_ENV["API_MODEL"],
        )

    def test_get_model_provider_partial_env(self, clean_env):
        """A partially configured .env passes through as empty strings."""
        with patch(
            "auto_epub.config._load_env",
            return_value=("https://api.example.com/v1", "", "test-model"),
        ):
            provider = get_model_provider()

        assert provider.base_url == "https://api.example.com/v1"
        assert provider.api_key == ""
        assert provider.model == "test-model"

    def test_get_model_provider_calls_load_env(self, clean_env):
        """Should call _load_env internally."""
        with patch(
            "auto_epub.config._load_env",
            return_value=(
                VALID_ENV["API_BASE_URL"],
                VALID_ENV["API_KEY"],
                VALID_ENV["API_MODEL"],
            ),
        ) as mock_load:
            get_model_provider()
            mock_load.assert_called_once()


class TestConfigIntegration:
    """Integration tests for config module."""

    def test_model_provider_immutability(self):
        """_ModelProvider should be a regular dataclass (mutable)."""
        provider = _ModelProvider(base_url="a", api_key="b", model="c")
        provider.base_url = "changed"
        assert provider.base_url == "changed"

    def test_env_vars_cleared_between_tests(self):
        """Environment variables should be cleaned up between tests."""
        # This test ensures our cleanup in other tests works
        assert "TEST_VAR_CLEANUP" not in os.environ

    def test_end_to_end_from_virtual_dotenv(self, clean_env, virtual_home):
        """_load_env -> get_model_provider wiring, without touching the real home."""
        with (
            patch.object(Path, "exists", return_value=True),
            patch("auto_epub.config.load_dotenv", side_effect=_dotenv(**VALID_ENV)),
        ):
            provider = get_model_provider()

        assert provider.base_url == VALID_ENV["API_BASE_URL"]
        assert provider.api_key == VALID_ENV["API_KEY"]
        assert provider.model == VALID_ENV["API_MODEL"]
