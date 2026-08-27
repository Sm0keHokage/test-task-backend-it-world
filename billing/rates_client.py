import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

from django.conf import settings

RATES_SERVICE_TIMEOUT_SECONDS = 2


class RatesServiceUnavailable(Exception):
    pass


def fetch_rate(currency_from: str, currency_to: str) -> Decimal:
    base_url = getattr(settings, "RATES_SERVICE_URL", "")
    if not base_url:
        raise RatesServiceUnavailable("RATES_SERVICE_URL is not configured")

    query = urllib.parse.urlencode({"currency_from": currency_from, "currency_to": currency_to})
    url = f"{base_url.rstrip('/')}/rates?{query}"

    try:
        with urllib.request.urlopen(url, timeout=RATES_SERVICE_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise RatesServiceUnavailable(f"rates service returned HTTP {response.status}")
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RatesServiceUnavailable(f"rates service HTTP error: {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RatesServiceUnavailable(f"rates service unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RatesServiceUnavailable("rates service timed out") from exc

    try:
        return Decimal(str(data["rate"]))
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise RatesServiceUnavailable(f"invalid rates service response: {data!r}") from exc