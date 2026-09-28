import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class _ModelProvider:
    base_url: str
    api_key: str
    model: str


def _load_env() -> tuple[str, str, str]:
    env_path = Path.home() / ".auto-epub/.env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)
    api_base_url = os.getenv("API_BASE_URL", "")
    api_key = os.getenv("API_KEY", "")
    api_model = os.getenv("API_MODEL", "")
    return api_base_url, api_key, api_model


def get_model_provider():
    api_base_url, api_key, api_model = _load_env()
    if not any([api_base_url, api_key, api_model]):
        raise ValueError("API provider not configured in ~/.auto-epub/.env")

    return _ModelProvider(base_url=api_base_url, api_key=api_key, model=api_model)


if __name__ == "__main__":
    print(get_model_provider())
