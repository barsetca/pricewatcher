"""Production entrypoint PriceWatch M9.5."""
from __future__ import annotations

import asyncio
import logging
import sys

from pricewatch.config import load_settings, load_telegram_settings
from pricewatch.database import initialize_database
from pricewatch.instance_lock import DatabaseInstanceLock, SingleInstanceLockError
from pricewatch.runtime import create_runtime
from pricewatch.safe_logging import configure_logging, redact_secrets


def main() -> int:
    lock: DatabaseInstanceLock | None = None
    logging_configured = False
    try:
        settings = load_settings()
        lock = DatabaseInstanceLock(settings.database_path)
        lock.acquire()
        telegram = load_telegram_settings()
        configure_logging(telegram.log_level, telegram.token)
        logging_configured = True
        initialize_database(settings.database_path)
        runtime = create_runtime(settings, telegram)
        return asyncio.run(runtime.run())
    except SingleInstanceLockError as error:
        print(f"PriceWatch не запущен: {error}", file=sys.stderr)
        return 3
    except ValueError as error:
        # Configuration validation is normally deliberately non-secret, but
        # keep the stderr path safe if a dependency ever includes a value.
        print(
            f"Ошибка конфигурации PriceWatch: {redact_secrets(error)}",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        if logging_configured:
            logging.exception("Не удалось запустить PriceWatch")
        else:
            # Before the protected formatter exists, expose only the exception
            # class. Standard logging.handleError could otherwise echo raw data.
            print(
                f"Не удалось запустить PriceWatch: {type(error).__name__}",
                file=sys.stderr,
            )
        return 1
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
