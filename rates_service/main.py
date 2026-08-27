import time
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Exchange Rates Service")

CACHE_TTL_SECONDS = 30

BASE_RATES: dict[tuple[str, str], Decimal] = {
    ("EUR", "USD"): Decimal("1.08"),
    ("USD", "EUR"): Decimal("0.93"),
    ("GBP", "USD"): Decimal("1.27"),
    ("USD", "GBP"): Decimal("0.79"),
    ("EUR", "GBP"): Decimal("0.85"),
    ("GBP", "EUR"): Decimal("1.18"),
}

_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def _lookup_rate(currency_from: str, currency_to: str) -> dict:
    key = (currency_from.upper(), currency_to.upper())

    if key[0] == key[1]:
        rate = Decimal("1.00")
    elif key in BASE_RATES:
        rate = BASE_RATES[key]
    else:
        raise HTTPException(status_code=404, detail=f"No rate for pair {key[0]}->{key[1]}")

    return {
        "currency_from": key[0],
        "currency_to": key[1],
        "rate": str(rate),
        "effective_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/rates")
def get_rate(
    currency_from: str = Query(..., min_length=3, max_length=3),
    currency_to: str = Query(..., min_length=3, max_length=3),
):
    key = (currency_from.upper(), currency_to.upper())
    now = time.monotonic()

    cached = _cache.get(key)
    if cached is not None and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    result = _lookup_rate(currency_from, currency_to)
    _cache[key] = (now, result)
    return result


@app.get("/health")
def health():
    return {"status": "ok"}