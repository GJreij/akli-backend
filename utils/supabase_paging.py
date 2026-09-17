# utils/supabase_paging.py
#
# PostgREST caps rows-per-request (this project's default is 1000) regardless
# of how many rows actually match a query. A plain .in_(...).execute() over a
# wide date range can come back short with no error at all — the admin panel
# just silently shows fewer rows than actually exist. fetch_all() pages
# through with .range() so nothing gets truncated.

PAGE_SIZE = 1000


def fetch_all(build_query, page_size=PAGE_SIZE):
    """build_query(lo, hi) -> a Supabase query with .range(lo, hi) applied.
    Loops until a page comes back shorter than page_size."""
    rows = []
    offset = 0
    while True:
        page = build_query(offset, offset + page_size - 1).execute().data or []
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return rows
