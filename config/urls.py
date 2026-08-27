"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path

from billing import views


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/invoices/", views.create_invoice, name="invoice-create"),
    path("api/v1/invoices/<int:invoice_id>/", views.invoice_detail, name="invoice-detail"),
    path("api/v1/invoices/<int:invoice_id>/cancel/", views.cancel_invoice, name="invoice-cancel"),
    path("internal/payments/", views.internal_record_payment, name="internal-payments"),
    path("api/v1/merchants/<int:merchant_id>/balance/", views.merchant_balance, name="merchant-balance"),
    path("api/v1/merchants/<int:merchant_id>/report/", views.merchant_report, name="merchant-report"),
]