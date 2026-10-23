"""Совместимый entrypoint: python -m pricewatch.telegram."""
from pricewatch.main import main

if __name__ == "__main__":
    raise SystemExit(main())
