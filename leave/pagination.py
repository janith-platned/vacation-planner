"""One small helper so every list that can grow pages the same way."""
from __future__ import annotations

from django.core.paginator import Paginator


def paginate(request, items, *, param: str = "page", size: int = 25):
    """Return (page_obj, base_query): the page of `items` named by `param`, and the other query
    parameters to keep in the Previous/Next links (filters, other lists' pages)."""
    page_obj = Paginator(items, size).get_page(request.GET.get(param))
    params = request.GET.copy()
    params.pop(param, None)
    return page_obj, params.urlencode()
