import time
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from elice_knowledge.corpus import ORIGIN, CorpusError, article_url, korean_page

USER_AGENT = "elice-techblog-rag/0.1 (corpus research)"


class Crawler:
    def __init__(self, client: httpx.Client, interval: float = 0.5, retries: int = 2):
        self.client = client
        self.interval = interval
        self.retries = retries
        self.last_request = 0.0
        self.robots: RobotFileParser | None = None

    def fetch(self, url: str, headers: dict | None = None) -> httpx.Response:
        for attempt in range(self.retries + 1):
            try:
                current = url
                for _ in range(6):
                    parsed = urlsplit(current)
                    if (
                        parsed.scheme != "https"
                        or parsed.netloc != "elice.io"
                        or not (
                            parsed.path == "/robots.txt"
                            or parsed.path.startswith("/ko/resources/blog")
                        )
                    ):
                        raise CorpusError("Request or redirect left the allowed Korean source")
                    if self.robots and not self.robots.can_fetch(USER_AGENT, current):
                        raise CorpusError("robots.txt disallows this URL")
                    time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
                    self.last_request = time.monotonic()
                    response = self.client.get(
                        current,
                        headers={
                            "User-Agent": USER_AGENT,
                            "Accept-Language": "ko-KR,ko;q=0.9",
                            **(headers or {}),
                        },
                        follow_redirects=False,
                    )
                    if response.status_code in {301, 302, 303, 307, 308}:
                        current = urljoin(current, response.headers.get("location", ""))
                        continue
                    break
                else:
                    raise CorpusError("Too many redirects")
                if response.status_code not in {200, 304}:
                    response.raise_for_status()
                    raise CorpusError("Unexpected HTTP status")
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code in {429, 500, 502, 503, 504}
                )
                if not retryable or attempt == self.retries:
                    raise
                # Bounded backoff; do not hammer the site if it throttles requests.
                delay = 2**attempt
                if isinstance(exc, httpx.HTTPStatusError):
                    value = exc.response.headers.get("retry-after", "")
                    if value.isdigit():
                        delay = min(max(delay, int(value)), 60)
                time.sleep(delay)
        raise AssertionError("unreachable")

    def load_robots(self):
        response = self.fetch(ORIGIN + "/robots.txt")
        if response.status_code != 200:
            raise CorpusError("Unable to load robots.txt")
        self.robots = RobotFileParser()
        self.robots.parse(response.text.splitlines())

    def discover(self, max_pages: int = 100) -> list[str]:
        pending, seen, urls, fingerprints = {1}, set(), set(), set()
        while pending:
            page = min(pending)
            pending.remove(page)
            if len(seen) >= max_pages:
                raise CorpusError("Listing exceeded the configured page limit")
            response = self.fetch(f"{ORIGIN}/ko/resources/blog?filter=tech_blog&page={page}")
            soup = BeautifulSoup(response.text, "html.parser")
            korean_page(soup)
            found = set()
            for anchor in soup.select("a[href]"):
                value = urlsplit(urljoin(ORIGIN, anchor["href"]))
                if value.netloc != "elice.io":
                    continue
                if value.path.rstrip("/") in {"/resources/blog", "/ko/resources/blog"}:
                    query = parse_qs(value.query)
                    number = query.get("page", [""])[0]
                    # The real site renders a next-page link past the final page.
                    # Only numbered pagination links describe existing pages.
                    if (
                        query.get("filter") == ["tech_blog"]
                        and number.isdigit()
                        and anchor.get_text(strip=True) == number
                    ):
                        number = int(number)
                        if number > 0 and number not in seen and number != page:
                            pending.add(number)
                else:
                    try:
                        found.add(article_url(anchor["href"]))
                    except CorpusError:
                        continue
            fingerprint = tuple(sorted(found))
            if not found or fingerprint in fingerprints:
                raise CorpusError("Empty or repeated listing page; discovery is incomplete")
            fingerprints.add(fingerprint)
            urls.update(found)
            seen.add(page)
            pending -= seen
        return sorted(urls)
