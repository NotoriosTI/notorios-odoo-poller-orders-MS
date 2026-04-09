from __future__ import annotations

from dataclasses import dataclass

from env_manager import get_config, require_config


@dataclass(frozen=True)
class Settings:
    db_path: str
    log_level: str
    encryption_key: str
    default_webhook_url: str

    @classmethod
    def load(cls) -> Settings:
        encryption_key = require_config("POLLER_ENCRYPTION_KEY")
        if not encryption_key:
            raise RuntimeError(
                "POLLER_ENCRYPTION_KEY es requerida. "
                "Genera una con: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )

        return cls(
            db_path=get_config("POLLER_DB_PATH") or "data/poller.db",
            log_level=get_config("POLLER_LOG_LEVEL") or "INFO",
            encryption_key=encryption_key,
            default_webhook_url=get_config("POLLER_DEFAULT_WEBHOOK_URL") or "",
        )
