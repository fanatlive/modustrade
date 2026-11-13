"""Numbered pagination: ‹ 1 … 4 5 6 … 12 ›."""
from django import template

register = template.Library()


def page_links(page, url):
    """Turn a Django Page into numbered links; `url(n)` builds the address of page n."""
    links = []
    for number in page.paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1):
        if number == page.paginator.ELLIPSIS:
            links.append({"gap": True})
        else:
            links.append({"number": number, "url": url(number), "current": number == page.number})
    return {"links": links,
            "previous": url(page.previous_page_number()) if page.has_previous() else None,
            "next": url(page.next_page_number()) if page.has_next() else None}


@register.inclusion_tag("partials/page_links.html")
def paginate(page_obj, querystring="", label="Страницы результатов"):
    prefix = f"?{querystring}&page=" if querystring else "?page="
    return {**page_links(page_obj, lambda n: f"{prefix}{n}"), "label": label}
