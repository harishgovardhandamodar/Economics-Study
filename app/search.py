"""Search providers: RSS feeds, the arXiv API, and DuckDuckGo HTML.

Each returns a list of candidate dicts:
  title, url, description, content, source, author, date_published,
  artifact_type, query

The default feed list is the economics one: central banks, the IMF/BFS/OECD,
the FT's macro columns, and the venues that actually carry cross-domain news
before anyone has written about it. A mapper pointed at "rate decision → stablecoin
float" needs the central bank's own release in the corpus, not three blog posts
summarising it.
"""
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import feedparser
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) economics-mapper/1.0"}

# Economics-first feed list. ``type`` becomes the artifact type, which is what
# lets the reviewer tell a central-bank release from a newspaper write-up at a
# glance rather than reading every card.
DEFAULT_RSS = [
    # --- central banks and official statistics ---
    {"url": "https://www.federalreserve.gov/feeds/press_monetary.xml",
     "source": "Federal Reserve", "type": "policy_doc"},
    {"url": "https://www.ecb.europa.eu/rss/press.html",
     "source": "ECB", "type": "policy_doc"},
    {"url": "https://www.bankofengland.co.uk/boeapps/rss/feeds.aspx?feed=News",
     "source": "Bank of England", "type": "policy_doc"},
    {"url": "https://www.bankofjapan.or.jp/en/rss/whatsnew.xml",
     "source": "Bank of Japan", "type": "policy_doc"},
    {"url": "https://www.imf.org/en/rss-list", "source": "IMF", "type": "report"},
    {"url": "https://www.bis.org/list/cbspeeches/index.rss",
     "source": "BIS speeches", "type": "speech"},
    {"url": "https://www.bis.org/list/rss.xml", "source": "BIS", "type": "report"},
    {"url": "https://www.oecd.org/newsroom/rss.xml", "source": "OECD",
     "type": "report"},
    # --- markets and finance ---
    {"url": "https://www.ft.com/rss/home", "source": "Financial Times",
     "type": "news"},
    {"url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
     "source": "WSJ Markets", "type": "news"},
    {"url": "https://www.economist.com/finance-and-economics/rss.xml",
     "source": "The Economist", "type": "news"},
    {"url": "https://www.bloomberg.com/feed/podcast/etf-report.xml",
     "source": "Bloomberg", "type": "news"},
    # --- crypto, stablecoins, on-chain ---
    {"url": "https://cointelegraph.com/rss", "source": "Cointelegraph",
     "type": "news"},
    {"url": "https://blog.thether.to/feed/", "source": "Tether",
     "type": "announcement"},
    {"url": "https://www.circle.com/blog/rss.xml", "source": "Circle",
     "type": "announcement"},
    # --- AI labs and compute (for the build-out side of the map) ---
    {"url": "https://openai.com/news/rss.xml", "source": "OpenAI",
     "type": "announcement"},
    {"url": "https://www.anthropic.com/rss.xml", "source": "Anthropic",
     "type": "announcement"},
    {"url": "https://deepmind.google/blog/rss.xml", "source": "Google DeepMind",
     "type": "announcement"},
    # --- energy and datacentres ---
    {"url": "https://www.eia.gov/rss/todayinenergy.xml", "source": "EIA",
     "type": "statistic"},
    {"url": "https://www.iea.org/rss/news.xml", "source": "IEA", "type": "report"},
    # --- think tanks ---
    {"url": "https://www.brookings.edu/feed/", "source": "Brookings",
     "type": "report"},
    {"url": "https://voxeu.org/feed", "source": "VoxEU", "type": "opinion"},
]

# Source names that count as primary: the institution speaking for itself.
# Used to boost official documents over coverage of them, the way the security
# mapper boosted vendor docs over blogs.
PRIMARY_SOURCES = {
    "federal reserve", "ecb", "bank of england", "bank of japan", "imf", "bis",
    "bis speeches", "oecd", "eia", "iea", "treasury", "ec", "federal reserve bank",
    "openai", "anthropic", "google deepmind", "tether", "circle", "bis bulletins",
}


def _clean(text: str | None, limit: int = 1200) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", text)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        return parsedate_to_datetime(str(value))
    except Exception:
        return None


def _fetch(url: str, timeout: int = 12, *, data=None) -> bytes | str | None:
    """Fetch a public read-only URL.

    Direct fetch only. The mapper runs on the operator's own machine and every
    endpoint here is public and unauthenticated, so a broker buys nothing and
    costs a dependency that can be down.
    """
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, headers=UA, follow_redirects=True) \
            if data is None else \
            httpx.post(url, data=data, headers=UA, timeout=timeout,
                       follow_redirects=True)
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:
        pass
    return None


def search_rss(query: str, feeds: list | None = None, per_feed: int = 8) -> list:
    """Keyword-filtered scan of RSS feeds (no key needed)."""
    out = []
    terms = [t.lower() for t in re.split(r"[,\s]+", query) if t and len(t) > 2]
    feed_list = feeds or DEFAULT_RSS
    for feed in feed_list:
        content = _fetch(feed["url"], timeout=10)
        if content is None:
            continue
        try:
            parsed = feedparser.parse(content)
        except Exception:
            continue
        for e in parsed.entries[:30]:
            title = _clean(getattr(e, "title", ""), 500)
            desc = _clean(getattr(e, "summary", getattr(e, "description", "")), 1200)
            blob = f"{title} {desc}".lower()
            if terms and not any(t in blob for t in terms):
                continue
            out.append({
                "title": title or "(untitled)",
                "url": getattr(e, "link", ""),
                "description": desc,
                "content": "",
                "source": feed.get("source", "RSS"),
                "author": _clean(getattr(e, "author", ""), 200),
                "date_published": _parse_date(getattr(e, "published", None)),
                "artifact_type": feed.get("type", "news"),
                "query": query,
                "primary": feed.get("source", "").lower() in PRIMARY_SOURCES,
            })
            if len(out) >= per_feed * len(feed_list):
                break
    return out


# arXiv categories that carry economics work worth reading. econ.* is the point;
# the rest are the cross-domain spillover (cs.CY for crypto-economics,
# q-fin for finance, cs.LG where capability claims are made).
ARXIV_CATEGORIES = "econ.*"


def search_arxiv(query: str, max_results: int = 15) -> list:
    """arXiv API full-text search."""
    out = []
    q = urllib.parse.quote(query)
    url = (f"http://export.arxiv.org/api/query?search_query=all:{q}"
           f"&start=0&max_results={max_results}"
           f"&sortBy=submittedDate&sortOrder=descending")
    content = _fetch(url, timeout=15)
    if content is None:
        return out
    try:
        root = ET.fromstring(content)
    except Exception:
        return out
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns):
        # Bind `entry` as a default argument. The closure is only called inside
        # this iteration, so it works either way -- but the unbound form breaks
        # the moment anyone moves a call out of the loop body, and the failure
        # would then be silent: every entry rendering the first one's title.
        def _t(tag, entry=entry):
            el = entry.find(f"a:{tag}", ns)
            return _clean(el.text) if el is not None and el.text else ""

        link = entry.find("a:id", ns)
        authors = entry.findall("a:author/a:name", ns)
        pub = entry.find("a:published", ns)
        try:
            dt = (datetime.fromisoformat(pub.text.replace("Z", "+00:00"))
                  if pub is not None else None)
        except Exception:
            dt = None
        summary = _t("summary")
        out.append({
            "title": _t("title") or "(untitled)",
            "url": link.text.strip() if link is not None and link.text else "",
            "description": summary[:600],
            "content": summary,
            "source": "arXiv",
            "author": ", ".join(a.text for a in authors if a.text) or "Unknown",
            "date_published": dt,
            "artifact_type": "paper",
            "query": query,
            "primary": False,
        })
    return out


def search_web(query: str, max_results: int = 10) -> list:
    """DuckDuckGo HTML endpoint (no key needed)."""
    out = []
    html = ""
    try:
        import httpx
        r = httpx.post("https://html.duckduckgo.com/html/",
                       data={"q": query, "b": "", "kl": ""},
                       headers=UA, timeout=15, follow_redirects=True)
        if r.status_code == 200:
            html = r.text
    except Exception:
        html = ""
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    for r in soup.select(".result")[:max_results]:
        a = r.select_one(".result__a")
        snip = r.select_one(".result__snippet")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"uddg=([^&]+)", href)
        url = urllib.parse.unquote(m.group(1)) if m else href
        if url.startswith("//"):
            url = "https:" + url
        host = urllib.parse.urlparse(url).netloc or "web"
        out.append({
            "title": _clean(a.get_text(), 500) or "(untitled)",
            "url": url,
            "description": _clean(snip.get_text() if snip else "", 600),
            "content": "",
            "source": host,
            "author": "",
            "date_published": None,
            "artifact_type": "news",
            "query": query,
            "primary": host.lower().removeprefix("www.") in PRIMARY_SOURCES,
        })
    return out


def fetch_page_summary(url: str, timeout: int = 12) -> dict:
    """Best-effort title/description/first-paragraph from one URL.

    Used by the explainer to offer a source document when the graph is thin.
    Everything is best effort: a page that blocks us yields an empty summary
    rather than an exception, because failing one of several sources must not
    fail the answer.
    """
    content = _fetch(url, timeout=timeout)
    if content is None:
        return {"url": url, "ok": False, "title": "", "text": ""}
    try:
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        title = _clean(soup.title.get_text() if soup.title else "", 300)
        desc = ""
        meta = soup.find("meta", attrs={"name": "description"}) or \
            soup.find("meta", attrs={"property": "og:description"})
        if meta:
            desc = _clean(meta.get("content", ""), 600)
        paras = [_clean(p.get_text(), 1200) for p in soup.find_all("p")]
        text = " ".join(p for p in paras if len(p) > 60)[:4000]
        return {"url": url, "ok": bool(title or text), "title": title,
                "description": desc, "text": text}
    except Exception as e:
        return {"url": url, "ok": False, "title": "", "text": "", "error": str(e)}
