from dataclasses import dataclass
import os
from dotenv import load_dotenv
from pathlib import Path


@dataclass
class _ModelProvider:
    base_url: str
    api_key: str
    model: str


def _load_env(env_file: str = ".env") -> None:
    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)


def get_model_provider():
    _load_env()
    api_base_url = os.getenv("API_BASE_URL", "")
    api_key = os.getenv("API_KEY", "")
    api_model = os.getenv("API_MODEL", "")
    return _ModelProvider(base_url=api_base_url, api_key=api_key, model=api_model)
