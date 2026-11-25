"""Bounded HTTP and owned Chromium behind a DNS-pinning egress proxy.

The local proxy is a transport, not a separate service. Every new outbound TCP
connection uses a checked numeric address; neither httpx nor Chromium resolves
remote destinations. This closes the validation/connect DNS-rebinding gap.
"""
import asyncio
from contextlib import suppress
from dataclasses import replace
import ipaddress
from http.cookiejar import CookieJar, DefaultCookiePolicy
import socket
import time
import zlib
from urllib.parse import urlsplit

import httpx
import anyio
from bs4 import UnicodeDammit

from ...trace import trace
from .reader import extract

HTML_CAP = 5 * 1024 * 1024
IMAGE_CAP = 8 * 1024 * 1024
SCREENSHOT_CAP = 2 * 1024 * 1024
DOM_CAP = 2_000_000
# Sent by HTTP and the browser alike. Wikimedia rejects agents without contact details, and also rejects browser
# agents (Safari, Chrome) whose TLS fingerprint doesn't match, so the hub names itself honestly.
USER_AGENT = "AgoraAgent/1.0 (+https://abril.me)"


class NoWebCookies(DefaultCookiePolicy):
    # HTTP extraction is deliberately stateless. Browser cookies live only in
    # the owning response's isolated context, never on this shared loader.
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class AccessError(ValueError):
    pass


class UnsupportedDocument(AccessError):
    def __init__(self, url, mime):
        self.url, self.mime = url, mime
        super().__init__(f"Unsupported document content type: {mime or 'unknown'}")


class DestinationPolicy:
    """Tests may inject a resolver/policy; production never allows private IPs."""
    def __init__(self, resolver=None):
        self.resolver = resolver

    def validate(self, url):
        if not isinstance(url, str) or len(url) > 4096:
            raise AccessError("Invalid or oversized URL")
        try:
            parts = urlsplit(url)
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError as exc:
            raise AccessError("Invalid URL port") from exc
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
            raise AccessError("Only public HTTP(S) URLs without credentials are allowed")
        if any(ord(c) <= 32 for c in url) or '%' in parts.hostname:
            raise AccessError("Invalid URL hostname")
        return parts.hostname, port

    async def addresses(self, host, port):
        if self.resolver:
            addresses = await self.resolver(host, port)
        else:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(info[4][0] for info in infos))
        if not addresses:
            raise AccessError("Destination has no addresses")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96"))):
                raise AccessError("Destination resolves to a non-public address")
        return addresses


class PinnedProxy:
    def __init__(self, policy):
        self.policy = policy
        self.server = None
        self.tasks = set()
        self.lock = asyncio.Lock()
        self.connections = asyncio.Semaphore(64)

    async def start(self):
        async with self.lock:
            if not self.server:
                self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=32_768)
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        remote = None
        try:
            async with asyncio.timeout(45), self.connections:
                raw = await reader.readuntil(b"\r\n\r\n")
                if len(raw) > 32_768:
                    raise AccessError("Proxy header cap")
                method, target, version = raw.split(b"\r\n", 1)[0].decode("ascii").split(" ")
                if method == "CONNECT":
                    host, port = self.policy.validate("https://" + target + "/")
                else:
                    host, port = self.policy.validate(target)
                    if method not in ("GET", "HEAD") or urlsplit(target).scheme != "http":
                        raise AccessError("Unsupported proxy request")
                addresses = await self.policy.addresses(host, port)
                # Numeric IP passed directly to open_connection: no second DNS lookup.
                upstream, remote = await asyncio.wait_for(asyncio.open_connection(addresses[0], port), 10)
                peer = remote.get_extra_info("peername")[0]
                if ipaddress.ip_address(peer) != ipaddress.ip_address(addresses[0]):
                    raise AccessError("Connected peer differs from pinned destination")
                if method == "CONNECT":
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                else:
                    parts = urlsplit(target)
                    path = parts.path or "/"
                    if parts.query:
                        path += "?" + parts.query
                    headers = []
                    for line in raw.split(b"\r\n")[1:-2]:
                        key = line.split(b":", 1)[0].lower()
                        if key not in (b"proxy-connection", b"connection", b"proxy-authorization", b"host", b"content-length", b"transfer-encoding"):
                            headers.append(line)
                    authority = (f"[{host}]" if ":" in host else host) + (f":{port}" if port != 80 else "")
                    remote.write(f"{method} {path} HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n".encode() + b"\r\n".join(headers) + b"\r\n\r\n")
                    await remote.drain()
                async def pump(source, destination, cap):
                    total = 0
                    while True:
                        chunk = await source.read(65536)
                        if not chunk:
                            return
                        total += len(chunk)
                        if total > cap:
                            raise AccessError("Proxy connection byte cap exceeded")
                        destination.write(chunk)
                        await destination.drain()
                flows = [asyncio.create_task(pump(reader, remote, 2*1024*1024)), asyncio.create_task(pump(upstream, writer, 20*1024*1024))]
                try:
                    done, pending = await asyncio.wait(flows, return_when=asyncio.FIRST_COMPLETED)
                    for flow in done:
                        flow.result()
                finally:
                    for flow in flows:
                        flow.cancel()
                    await asyncio.gather(*flows, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            with suppress(Exception):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 27\r\nConnection: close\r\n\r\nDestination policy rejected")
                await writer.drain()
        finally:
            for stream in (remote, writer):
                if stream:
                    stream.close()
                    with suppress(Exception):
                        await stream.wait_closed()
            self.tasks.discard(task)

    async def close(self):
        server, self.server = self.server, None
        if server:
            server.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if server:
            await server.wait_closed()


# Serialize a bounded generic DOM clone inside the renderer. Never return the
# unbounded document.outerHTML across the Playwright wire.
DOM_SCRIPT = r"""() => {
 const max=2000000, nodes=20000; let used=0, count=0, omitted=false;
 function copy(n) {
  if (++count>nodes || used>=max) {omitted=true; return '';}
  if(n.nodeType===3) {let s=n.textContent.slice(0,Math.min(8000,max-used));
    s=s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'); used+=s.length; return s;}
  if(n.nodeType!==1) return '';
  const tag=n.tagName.toLowerCase();
  if(['script','style','noscript','template','svg','iframe','object','embed'].includes(tag)) return '';
  const style=getComputedStyle(n); if(n.hidden || style.display==='none' || style.visibility==='hidden') return '';
  let s='<'+tag;
  for(const key of ['href','src','alt','title']) {let v=n.getAttribute(key); if(v) s+=' '+key+'="'+v.slice(0,4096).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;')+'"';}
  if(tag==='img' && n.currentSrc) s+=' data-agora-current-src="'+n.currentSrc.slice(0,4096).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;')+'"';
  s+='>'; used+=s.length;
  for(const c of n.childNodes) {s+=copy(c);if(used>=max || count>nodes){omitted=true;break;}}
  return s+'</'+tag+'>';
 }
 let body=copy(document.body || document.documentElement);
 return {html:('<html><head><title>'+document.title.slice(0,300).replaceAll('<','&lt;')+'</title><base href="'+document.baseURI.slice(0,4096).replaceAll('&','&amp;').replaceAll('"','&quot;')+'"></head>'+body+'</html>').slice(0,max), omitted};
}"""


class Browser:
    def __init__(self, proxy, idle_seconds=300):
        self.proxy = proxy
        self.idle_seconds = idle_seconds
        self.playwright = None
        self.process = None
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(2)
        self.sessions = {}
        self.idle_task = None
        self.pending = 0

    async def _start(self):
        async with self.lock:
            if self.idle_task:
                self.idle_task.cancel()
                self.idle_task = None
            if self.process and self.process.is_connected():
                return
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            launching = None
            try:
                proxy_url = await self.proxy.start()
                launching = asyncio.create_task(self.playwright.chromium.launch(
                    headless=True, chromium_sandbox=True,
                    proxy={"server": proxy_url, "bypass": "<-loopback>"},
                    args=["--disable-quic", "--disable-background-networking", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--proxy-bypass-list=<-loopback>"],
                ))
                self.process = await asyncio.shield(launching)
            except BaseException:
                # Cancellation can race a live process before launch returns.
                # Retrieve and close it before stopping its protocol owner.
                with anyio.CancelScope(shield=True):
                    if launching is not None:
                        with suppress(Exception):
                            process = await launching
                            await process.close()
                    await self.playwright.stop()
                    self.playwright = None
                    self.process = None
                raise

    async def page(self, state):
        key = id(state)
        if key in self.sessions:
            context, page = self.sessions[key]
            if not page.is_closed() and self.process and self.process.is_connected():
                return page
            await self.cleanup(state)
        # Semaphore queue/startup time is covered by the group's action deadline.
        self.pending += 1
        acquired = False
        try:
            await self.slots.acquire()
            acquired = True
            await self._start()
            # A context may exist in Chromium before the protocol await returns.
            # Keep that operation alive on cancellation, then close the acquired
            # resource before releasing its slot (also under ASGI cancellation).
            creating = asyncio.create_task(self.process.new_context(
                service_workers="block", accept_downloads=False, viewport={"width":1280,"height":900},
                user_agent=USER_AGENT))
            try:
                context = await asyncio.shield(creating)
            except BaseException:
                with anyio.CancelScope(shield=True):
                    with suppress(Exception):
                        context = await creating
                        await context.close()
                raise
            try:
                # All HTTP(S) traffic uses the pinning proxy. Reject other network
                # protocols and cap requests; data/blob images remain local only.
                requests = [0]
                async def route(request_route):
                    requests[0] += 1
                    url = request_route.request.url
                    if requests[0] > 300:
                        await request_route.abort()
                        return
                    try:
                        self.proxy.policy.validate(url)
                    except AccessError:
                        await request_route.abort()
                    else:
                        await request_route.continue_()
                await context.route("**/*", route)
                # WebSocket connections have no role in document research.
                async def close_socket(ws):
                    await ws.close()
                await context.route_web_socket("**/*", close_socket)
                page = await context.new_page()
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await context.close()
                raise
            self.sessions[key] = (context, page)
            acquired = False  # session owns the semaphore now
            return page
        finally:
            self.pending -= 1
            if acquired:
                self.slots.release()
            if not self.sessions and not self.pending:
                self._idle()

    async def load(self, state, url, scroll=None):
        self.proxy.policy.validate(url)
        page = await self.page(state)
        try:
            if scroll is None or page.url != url:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                code = response.status if response else 200
            else:
                code = 200
            if scroll in ("down", "up"):
                await page.evaluate("d => window.scrollBy(0, d * innerHeight * .8)", 1 if scroll == "down" else -1)
            await page.wait_for_timeout(350)
            dom = await page.evaluate(DOM_SCRIPT)
            result = extract(dom["html"], url, page.url, "browser", code)
            if dom["omitted"]:
                result = replace(result, detail="Rendered DOM truncated at bounded extraction limit")
            return result
        except BaseException:
            await self.cleanup(state)
            raise

    async def screenshot(self, state):
        page = await self.page(state)
        data = await page.screenshot(type="jpeg", quality=65, full_page=False, timeout=10_000)
        if len(data) > SCREENSHOT_CAP:
            raise AccessError("Screenshot exceeds 2 MB cap")
        return data

    def _idle(self):
        if self.idle_task:
            self.idle_task.cancel()
        async def expire():
            await asyncio.sleep(self.idle_seconds)
            async with self.lock:
                if not self.sessions and not self.pending:
                    await self._stop()
        self.idle_task = asyncio.create_task(expire())

    async def cleanup(self, state):
        session = self.sessions.pop(id(state), None)
        if session:
            try:
                await session[0].close()
            finally:
                self.slots.release()
        if not self.sessions and not self.pending:
            self._idle()

    async def _stop(self):
        if self.process:
            with suppress(Exception):
                await self.process.close()
            self.process = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def close(self):
        if self.idle_task:
            self.idle_task.cancel()
            await asyncio.gather(self.idle_task, return_exceptions=True)
            self.idle_task = None
        for context, _ in list(self.sessions.values()):
            with suppress(Exception):
                await context.close()
            self.slots.release()
        self.sessions.clear()
        await self._stop()


class Loader:
    def __init__(self, *, policy=None, transport=None, browser=None, browser_enabled=True, idle_seconds=300):
        self.policy = policy or DestinationPolicy()
        self.proxy = PinnedProxy(self.policy)
        self.transport = transport  # deterministic tests only; production uses pinned proxy
        self.client = None
        self.client_lock = asyncio.Lock()
        self.browser_enabled = browser_enabled
        self.browser = browser or Browser(self.proxy, idle_seconds)

    async def _client(self):
        async with self.client_lock:
            if self.client is None:
                self.client = httpx.AsyncClient(proxy=None if self.transport else await self.proxy.start(),
                    transport=self.transport, trust_env=False, timeout=15, follow_redirects=False,
                    cookies=CookieJar(policy=NoWebCookies()),
                    headers={"User-Agent":USER_AGENT, "Accept":"text/html,text/plain,image/*;q=0.8", "Accept-Encoding":"gzip, deflate, identity"})
            return self.client

    async def fetch(self, url, cap=HTML_CAP, image=False):
        client = await self._client()
        for redirect in range(11):
            self.policy.validate(url)
            async with asyncio.timeout(15), client.stream("GET", url) as response:
                if response.is_redirect:
                    if redirect == 10:
                        raise AccessError("Redirect limit exceeded")
                    from urllib.parse import urljoin
                    url = urljoin(str(response.url), response.headers.get("location", ""))
                    continue
                mime = response.headers.get("content-type", "").split(";")[0].lower()
                if image:
                    if response.status_code != 200 or mime not in ("image/png", "image/jpeg", "image/webp"):
                        raise AccessError(f"Unsupported image response ({response.status_code}, {mime})")
                elif mime not in ("text/html", "application/xhtml+xml", "text/plain"):
                    raise UnsupportedDocument(str(response.url), mime)
                chunks, size, wire_size = [], 0, 0
                encoding = response.headers.get("content-encoding", "identity").lower()
                if encoding not in ("identity", "gzip", "deflate"):
                    raise AccessError("Unsupported response compression")
                decoder = zlib.decompressobj(16+zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS) if encoding != "identity" else None
                # Raw iteration plus max_length prevents a compressed bomb from
                # allocating its expanded body inside httpx before our cap check.
                # MockTransport pre-decoded responses are a test-only special case.
                raw_stream = response.aiter_bytes(65536) if response.is_stream_consumed else response.aiter_raw(65536)
                if response.is_stream_consumed:
                    decoder = None
                async for raw in raw_stream:
                    wire_size += len(raw)
                    if wire_size > cap:
                        raise AccessError(f"Response exceeds {cap} byte cap")
                    try:
                        chunk = decoder.decompress(raw, cap-size+1) if decoder else raw
                    except zlib.error as exc:
                        raise AccessError("Invalid compressed response") from exc
                    size += len(chunk)
                    if size > cap or (decoder and decoder.unconsumed_tail):
                        raise AccessError(f"Response exceeds {cap} byte cap")
                    chunks.append(chunk)
                if decoder and (not decoder.eof or decoder.unused_data):
                    raise AccessError("Incomplete or concatenated compressed response")
                return b"".join(chunks), str(response.url), response.status_code, mime, response.charset_encoding
        raise AccessError("Redirect limit exceeded")

    async def load(self, state, url, mode="auto"):
        if mode not in ("auto", "http", "browser"):
            raise ValueError("mode must be auto, http or browser")
        self.policy.validate(url)
        attempts = []
        page = None
        if mode != "browser":
            started = time.monotonic()
            try:
                data, final, code, mime, charset = await self.fetch(url)
                # The header's charset wins; otherwise the page's <meta charset>, then UTF-8, then Windows-1252.
                text = UnicodeDammit(data, [charset] if charset else [], is_html=True).unicode_markup
                text = text if text is not None else data.decode("utf-8", "replace")
                if mime == "text/plain":
                    import html
                    text = "<pre>" + html.escape(text) + "</pre>"
                page = extract(text, url, final, "http", code)
                attempts.append({"mode":"http", "status":page.status})
            except UnsupportedDocument as exc:
                page = replace(extract("", url, exc.url, "http"), status="unsupported", detail=str(exc))
                attempts.append({"mode":"http", "status":"unsupported", "detail":str(exc)[:300]})
            except (AccessError, httpx.HTTPError, OSError, TimeoutError) as exc:
                attempts.append({"mode":"http", "status":"error", "detail":str(exc)[:300]})
            self._trace(state, url, attempts[-1]["status"], "http", started)
            if page and (page.status in ("content", "unsupported") or mode == "http"):
                return page, attempts
        if mode != "http" and self.browser_enabled:
            try:
                started = time.monotonic()
                page = await self.browser.load(state, url)
                attempts.append({"mode":"browser", "status":page.status})
                self._trace(state, url, page.status, "browser", started)
                return page, attempts
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts.append({"mode":"browser", "status":"error", "detail":str(exc)[:300]})
                self._trace(state, url, "error", "browser", started)
        elif mode == "browser":
            raise AccessError("Browser rendering is disabled")
        if page:
            return replace(page, detail=str(attempts)[-700:]), attempts
        raise AccessError("Loading failed: " + str(attempts)[-1000:])

    @staticmethod
    def _trace(state, url, status, mode, started):
        trace(getattr(state, "request_id", None), "web.fetch", mode=mode, status=status,
              seconds=round(time.monotonic() - started, 3), url=url[:300])

    async def cleanup(self, state):
        await self.browser.cleanup(state)

    async def close(self):
        await self.browser.close()
        if self.client:
            await self.client.aclose()
        await self.proxy.close()
