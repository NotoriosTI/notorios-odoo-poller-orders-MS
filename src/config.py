from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    db_path: str
    log_level: str
    encryption_key: str
    default_webhook_url: str

    @classmethod
    def load(cls) -> Settings:
        load_dotenv()

        encryption_key = os.environ.get("POLLER_ENCRYPTION_KEY", "")
        if not encryption_key:
            raise RuntimeError(
                "POLLER_ENCRYPTION_KEY es requerida. "
                "Genera una con: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )

        return cls(
            db_path=os.environ.get("POLLER_DB_PATH", "data/poller.db"),
            log_level=os.environ.get("POLLER_LOG_LEVEL", "INFO"),
            encryption_key=encryption_key,
            default_webhook_url=os.environ.get("POLLER_DEFAULT_WEBHOOK_URL", ""),
        )
