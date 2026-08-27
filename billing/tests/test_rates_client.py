import urllib.error
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from billing import rates_client


def _mocked_response(body: bytes, status: int = 200):
    response = MagicMock()
    response.status = status
    response.read.return_value = body
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


@patch("billing.rates_client.urllib.request.urlopen")
def test_fetch_rate_returns_decimal_from_valid_response(mock_urlopen, settings):
    settings.RATES_SERVICE_URL = "http://rates.example.com"
    mock_urlopen.return_value = _mocked_response(
        b'{"currency_from": "EUR", "currency_to": "USD", "rate": "1.08"}'
    )

    rate = rates_client.fetch_rate("EUR", "USD")

    assert rate == Decimal("1.08")
    called_url = mock_urlopen.call_args[0][0]
    assert called_url.startswith("http://rates.example.com/rates?")
    assert "currency_from=EUR" in called_url
    assert "currency_to=USD" in called_url


def test_fetch_rate_without_configured_url_raises():
    with pytest.raises(rates_client.RatesServiceUnavailable):
        rates_client.fetch_rate("EUR", "USD")


@patch("billing.rates_client.urllib.request.urlopen")
def test_fetch_rate_raises_on_connection_error(mock_urlopen, settings):
    settings.RATES_SERVICE_URL = "http://rates.example.com"
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")

    with pytest.raises(rates_client.RatesServiceUnavailable):
        rates_client.fetch_rate("EUR", "USD")


@patch("billing.rates_client.urllib.request.urlopen")
def test_fetch_rate_raises_on_timeout(mock_urlopen, settings):
    settings.RATES_SERVICE_URL = "http://rates.example.com"
    mock_urlopen.side_effect = TimeoutError()

    with pytest.raises(rates_client.RatesServiceUnavailable):
        rates_client.fetch_rate("EUR", "USD")


@patch("billing.rates_client.urllib.request.urlopen")
def test_fetch_rate_raises_on_malformed_response(mock_urlopen, settings):
    settings.RATES_SERVICE_URL = "http://rates.example.com"
    mock_urlopen.return_value = _mocked_response(b'{"unexpected": "shape"}')

    with pytest.raises(rates_client.RatesServiceUnavailable):
        rates_client.fetch_rate("EUR", "USD")


@patch("billing.rates_client.urllib.request.urlopen")
def test_fetch_rate_raises_on_non_200_status(mock_urlopen, settings):
    settings.RATES_SERVICE_URL = "http://rates.example.com"
    mock_urlopen.return_value = _mocked_response(b"{}", status=500)

    with pytest.raises(rates_client.RatesServiceUnavailable):
        rates_client.fetch_rate("EUR", "USD")