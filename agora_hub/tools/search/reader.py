"""Generic, bounded HTML extraction and immutable response-local snapshots."""
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

TEXT_CAP = 120_000
REF_CAP = 120
CACHE_CAP = 1_000_000


@dataclass(frozen=True)
class Reference:
    url: str
    text: str


@dataclass(frozen=True)
class Snapshot:
    requested_url: str
    final_url: str
    title: str
    text: str
    links: tuple[Reference, ...]
    images: tuple[Reference, ...]
    status: str
    mode: str
    fetched_at: str
    omitted_chars: int = 0
    omitted_links: int = 0
    omitted_images: int = 0
    id: str = ""
    detail: str = ""


def web_url(base, value):
    if not value or len(value) > 4096:
        return None
    url = urljoin(base, value)
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password else None


def image_url(base, img):
    # Lazy-loading pages put a data: placeholder in src and the real address in data-src or srcset.
    srcset = (img.get("srcset") or img.get("data-srcset") or "").split(",")[0].split()
    for value in (img.get("data-agora-current-src"), img.get("src"), img.get("data-src"), img.get("data-lazy-src"),
                  srcset[0] if srcset else None):
        if url := web_url(base, value):
            return url
    return None


def extract(html: str, requested: str, final: str, mode: str, status_code=200) -> Snapshot:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True)[:300] if soup.title else ""
    base_tag = soup.find("base", href=True)
    base = web_url(final, base_tag.get("href")) if base_tag else final
    base = base or final
    for tag in soup(["script", "style", "noscript", "template", "svg", "nav", "aside", "header", "footer"]):
        # Site headers and footers are boilerplate; an article's own header holds its title, byline and date.
        if not tag.decomposed and not (tag.name in ("header", "footer") and tag.find_parent(["article", "main"])):
            tag.decompose()
    for tag in list(soup.select('[hidden], [aria-hidden="true"], input[type="hidden"]')):
        if not tag.decomposed:
            tag.decompose()
    for tag in list(soup.find_all(style=True)):
        # A hidden ancestor removed earlier in this loop also removed this tag.
        if not tag.decomposed and re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", tag.get("style", ""), re.I):
            tag.decompose()
    # One article is the page's content; several (a listing, search results, a feed) are read together.
    articles = [a for a in soup.find_all("article") if not a.find_parent("article")]
    root = (articles[0] if len(articles) == 1 else None) or soup.find("main") or soup.body or soup
    links, images = [], []
    for a in root.find_all("a", href=True):
        url = web_url(base, a["href"])
        if url:
            links.append(Reference(url, a.get_text(" ", strip=True)[:240]))
    for img in soup.find_all("img"):
        if url := image_url(base, img):
            images.append(Reference(url, str(img.get("alt", ""))[:240]))
    for row in root.find_all("tr"):
        row.replace_with("\n" + " | ".join(c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])) + "\n")
    for block in root.find_all(["p", "h1", "h2", "h3", "h4", "li", "figcaption", "caption", "br", "div", "section"]):
        block.insert_before("\n")
        block.insert_after("\n")
    text = "\n".join(" ".join(line.split()) for line in root.get_text(" ").splitlines() if line.strip())
    prominent = (title + "\n" + text[:1200]).lower()
    # Challenge phrases must dominate the page or occur in its title; an article
    # discussing CAPTCHA is ordinary content.
    challenge = ("verify you are human", "checking your browser", "unusual traffic", "access denied", "just a moment", "robot check", "complete the captcha")
    consent = ("before you continue", "consent required", "review your privacy choices", "accept cookies to continue")
    status = "content"
    if status_code in (401, 403, 407, 429, 451):
        status = "blocked"
    elif status_code >= 400:
        status = "error"
    elif any(p in prominent for p in consent) and (len(text) < 6000 or any(p in title.lower() for p in consent)):
        status = "consent"
    elif any(p in prominent for p in challenge) and (len(text) < 5000 or any(p in title.lower() for p in challenge)):
        status = "blocked"
    elif len(text) < 80 or (len(text) < 500 and re.search(r"enable javascript|javascript (?:is required|disabled)|turn on javascript", prominent)):
        status = "empty"
    return Snapshot(requested, final, title, text[:TEXT_CAP], tuple(links[:REF_CAP]), tuple(images[:REF_CAP]), status,
                    mode, datetime.now(timezone.utc).isoformat(), max(0, len(text)-TEXT_CAP),
                    max(0, len(links)-REF_CAP), max(0, len(images)-REF_CAP))


class Pages:
    def __init__(self):
        self.items = OrderedDict()
        self.sequence = 0
        self.current = None

    def add(self, page):
        self.sequence += 1
        page = replace(page, id=f"p{self.sequence}")
        while self.size(page) > CACHE_CAP and (page.links or page.images):
            if page.links:
                page = replace(page, links=page.links[:-1], omitted_links=page.omitted_links+1)
            else:
                page = replace(page, images=page.images[:-1], omitted_images=page.omitted_images+1)
        self.items[page.id] = page
        if page.status == "content":
            self.current = page.id
        while len(self.items) > 30 or sum(self.size(p) for p in self.items.values()) > CACHE_CAP:
            # Failed navigation must not evict the last successful current page.
            victim = next((key for key in self.items if key != self.current), None)
            if victim is None:
                break
            del self.items[victim]
        return page

    @staticmethod
    def size(p):
        return sum(len(value.encode()) for value in (p.text, p.requested_url, p.final_url, p.title, p.detail, p.fetched_at, p.mode, p.id)) + sum(len(r.url.encode()) + len(r.text.encode()) for r in (*p.links, *p.images)) + 512

    def resolve(self, ref, kind=None):
        match = re.fullmatch(r"(p[1-9][0-9]*)(?::([li])([1-9][0-9]*))?", ref or "")
        if not match or match[1] not in self.items:
            raise ValueError("Unknown or evicted page reference; references last only for this response")
        page = self.items[match[1]]
        if not match[2]:
            if kind:
                raise ValueError(f"Expected a {kind} reference")
            return page
        if kind and match[2] != kind:
            raise ValueError(f"Expected a {kind} reference")
        entries = page.links if match[2] == "l" else page.images
        if int(match[3]) > len(entries):
            raise ValueError("Unknown page reference")
        return page, entries[int(match[3])-1]

    def by_url(self, url):
        return next((p for p in reversed(self.items.values()) if url in (p.requested_url, p.final_url) and p.status == "content"), None)


def render(page, offset=0, cap=12000, attempts=()):
    if type(offset) is not int or offset < 0 or offset > len(page.text):
        raise ValueError("offset must be between zero and the retained page text length")
    result = dict(snapshot=page.id, requested_url=page.requested_url, final_url=page.final_url, title=page.title,
                  fetched_at=page.fetched_at, mode=page.mode, access_status=page.status, detail=page.detail,
                  offset=offset, next_offset=None, text="", links=[], images=[],
                  omitted_chars=page.omitted_chars, omitted_links=page.omitted_links,
                  omitted_images=page.omitted_images, attempts=list(attempts))
    # Reserve text room; metadata is bounded independently, including hostile long URLs. Links stop at 3/8 of the
    # result so that image references always get room too, up to half of it in all.
    for key, refs, prefix, limit in (("links", page.links, "l", cap * 3 // 8), ("images", page.images, "i", cap // 2)):
        for i, ref in enumerate(refs, 1):
            entry = dict(ref=f"{page.id}:{prefix}{i}", url=ref.url, text=ref.text)
            result[key].append(entry)
            if len(json.dumps(result, ensure_ascii=False)) > limit:
                result[key].pop()
                break
        result[f"omitted_{key}"] += len(refs) - len(result[key])
    # Metadata alone may exceed a very small configured cap. Trim strings, then refs.
    for key in ("requested_url", "final_url", "detail", "title"):
        if len(json.dumps(result, ensure_ascii=False)) > cap - 200:
            result[key] = str(result[key])[:100]
    while len(json.dumps(result, ensure_ascii=False)) > cap - 200 and (result["links"] or result["images"]):
        key = "links" if result["links"] else "images"
        result[key].pop()
        result[f"omitted_{key}"] += 1
    room = max(0, cap - len(json.dumps(result, ensure_ascii=False)) - 100)
    result["text"] = page.text[offset:offset+room]
    end = offset + len(result["text"])
    result["next_offset"] = end if end < len(page.text) else None
    encoded = json.dumps(result, ensure_ascii=False)
    while len(encoded) > cap and result["text"]:
        result["text"] = result["text"][:max(0, len(result["text"]) - (len(encoded)-cap))]
        result["next_offset"] = offset + len(result["text"])
        encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) > cap:
        return json.dumps({"snapshot": page.id, "access_status": page.status, "error": "Metadata exceeds output limit", "offset": offset})[:cap]
    return encoded
