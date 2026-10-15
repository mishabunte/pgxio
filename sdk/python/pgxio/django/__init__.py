"""Request-scoped Django integration. Pass the caller's token explicitly."""
from django.conf import settings
from pgxio import Client, AsyncClient

def client(token):
    return Client(settings.PGXIO_URL, token)

def async_client(token):
    return AsyncClient(settings.PGXIO_URL, token)
