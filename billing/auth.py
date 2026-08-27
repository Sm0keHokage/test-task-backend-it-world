from functools import wraps

from django.conf import settings
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt

from billing.errors import error_response
from billing.models import Merchant, Project


def require_project_api_key(view_func):
    """Аутентификация публичных эндпоинтов"""
    @csrf_exempt
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        api_key = request.headers.get("X-Api-Key")
        if not api_key:
            return error_response(401, "missing_api_key", "X-Api-Key header is required")

        project = Project.objects.select_related("merchant").filter(api_key=api_key).first()
        if project is None:
            return error_response(401, "invalid_api_key", "Unknown API key")

        if project.merchant.status == Merchant.Status.BLOCKED:
            return error_response(403, "merchant_blocked", "Merchant is blocked")

        request.project = project
        request.merchant = project.merchant
        return view_func(request, *args, **kwargs)

    return wrapped


def require_internal_token(view_func):
    """Аутентификация внутреннего эндпоинта"""
    @csrf_exempt
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()

        if not token or not constant_time_compare(token, settings.INTERNAL_PAYMENTS_TOKEN):
            return error_response(401, "invalid_internal_token", "Invalid or missing internal token")

        return view_func(request, *args, **kwargs)

    return wrapped