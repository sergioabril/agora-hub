import asyncio
import gzip

import httpx
import pytest

from agora_hub.tools.search.loader import AccessError, DestinationPolicy, Loader, PinnedProxy


@pytest.mark.parametrize('url', ['file:///etc/passwd','http://user:pass@example.org','ftp://example.org','http://example.org:bad','http://foo%25bar/'])
def test_url_policy(url):
    with pytest.raises(AccessError):
        DestinationPolicy().validate(url)


@pytest.mark.parametrize('address', ['127.0.0.1','10.0.0.1','192.168.0.1','169.254.169.254','::1','fc00::1','fe80::1','::ffff:127.0.0.1','2002:7f00:1::','64:ff9b::7f00:1','100.64.0.1'])
async def test_dns_policy(address):
    async def resolver(host, port):
        return [address]
    with pytest.raises(AccessError):
        await DestinationPolicy(resolver).addresses('public.example', 80)


async def test_actual_proxy_private_destination_and_rebinding_no_connection():
    hits = []
    async def private(reader, writer):
        hits.append(True)
        writer.close()
    server = await asyncio.start_server(private, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    async def rebinding(host, port):
        return ['127.0.0.1']
    proxy = PinnedProxy(DestinationPolicy(rebinding))
    try:
        async with httpx.AsyncClient(proxy=await proxy.start(), trust_env=False) as client:
            response = await client.get(f'http://public.example:{port}/')
            assert response.status_code == 403
            with pytest.raises(httpx.ProxyError):
                await client.get(f'https://public.example:{port}/')
        assert hits == []
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


async def test_redirect_validation_body_decompression_and_unsupported():
    def fixture(request):
        if request.url.path == '/redirect':
            return httpx.Response(302, headers={'location':'file:///etc/passwd'})
        if request.url.path == '/large':
            return httpx.Response(200, headers={'content-type':'text/html','content-encoding':'gzip'}, content=gzip.compress(b'a'*2000))
        return httpx.Response(200, headers={'content-type':'application/pdf'}, content=b'%PDF')
    loader = Loader(transport=httpx.MockTransport(fixture), browser_enabled=False)
    try:
        with pytest.raises(AccessError, match='HTTP'):
            await loader.fetch('https://e.org/redirect')
        with pytest.raises(AccessError, match='byte cap'):
            await loader.fetch('https://e.org/large', cap=1000)
        with pytest.raises(AccessError, match='Unsupported'):
            await loader.fetch('https://e.org/pdf')
    finally:
        await loader.close()


async def test_auto_fallback_once_and_cancellation_propagates():
    class FakeBrowser:
        calls = 0
        async def load(self, state, url):
            self.calls += 1
            raise asyncio.CancelledError()
        async def close(self): pass
    browser = FakeBrowser()
    loader = Loader(transport=httpx.MockTransport(lambda r:httpx.Response(200, headers={'content-type':'text/html'},text='<p>Enable JavaScript</p>')),browser=browser)
    try:
        with pytest.raises(asyncio.CancelledError):
            await loader.load(object(), 'https://e.org')
        assert browser.calls == 1
    finally:
        await loader.close()


async def test_streamed_gzip_bomb_rejected_before_expansion():
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield gzip.compress(b'x' * 10_000_000)
    loader = Loader(transport=httpx.MockTransport(lambda request:httpx.Response(200,
        headers={'content-type':'text/html','content-encoding':'gzip'},stream=Body())),browser_enabled=False)
    try:
        with pytest.raises(AccessError,match='byte cap'):
            await loader.fetch('https://e.org/bomb',cap=1000)
    finally:
        await loader.close()


async def test_proxy_close_cancels_live_tunnel_promptly():
    hit = asyncio.Event()
    active = set()
    async def fixture(reader,writer):
        active.add(writer)
        hit.set()
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            active.discard(writer)
    server = await asyncio.start_server(fixture,'127.0.0.1',0)
    port = server.sockets[0].getsockname()[1]
    class TestPolicy(DestinationPolicy):
        async def addresses(self,host,number):
            assert host == 'fixture.test' and number == port
            return ['127.0.0.1']
    proxy = PinnedProxy(TestPolicy())
    address = await proxy.start()
    reader,writer = await asyncio.open_connection('127.0.0.1',int(address.rsplit(':',1)[1]))
    writer.write(f'CONNECT fixture.test:{port} HTTP/1.1\r\n\r\n'.encode())
    await writer.drain()
    await hit.wait()
    await reader.readuntil(b'\r\n\r\n')
    try:
        async with asyncio.timeout(1):
            await proxy.close()
        assert not proxy.tasks
    finally:
        writer.close()
        await writer.wait_closed()
        server.close()
        for stream in list(active):
            stream.close()
        await server.wait_closed()


async def test_shared_http_transport_never_retains_response_cookies():
    seen = []
    def fixture(request):
        seen.append(request.headers.get('cookie'))
        return httpx.Response(200,headers={'content-type':'text/html','set-cookie':'private_user=alice; Path=/'},text='public content '*20)
    loader = Loader(transport=httpx.MockTransport(fixture),browser_enabled=False)
    try:
        await loader.fetch('https://example.org/one')
        await loader.fetch('https://example.org/two')
        assert seen == [None,None]
        assert not loader.client.cookies
    finally:
        await loader.close()


async def test_unsupported_document_is_honest_snapshot_without_browser():
    class UnexpectedBrowser:
        async def load(self,*args):
            pytest.fail('PDF must not fall back to browser extraction')
        async def close(self): pass
    loader = Loader(transport=httpx.MockTransport(lambda r:httpx.Response(200,
        headers={'content-type':'application/pdf'},content=b'%PDF')),browser=UnexpectedBrowser())
    try:
        for mode in ('http','auto'):
            page,attempts = await loader.load(object(),'https://example.org/paper.pdf',mode)
            assert page.status == 'unsupported' and page.final_url.endswith('/paper.pdf')
            assert attempts == [{'mode':'http','status':'unsupported','detail':'Unsupported document content type: application/pdf'}]
    finally:
        await loader.close()


async def test_browser_logging_includes_duration_and_access_status(caplog):
    from agora_hub.tools.search.reader import extract
    class FakeBrowser:
        async def load(self,state,url):
            return extract('<main>'+ 'Original source evidence. '*20+'</main>',url,url,'browser')
        async def close(self): pass
    loader = Loader(browser=FakeBrowser())
    try:
        with caplog.at_level('INFO',logger='agora_hub.tools.search.loader'):
            await loader.load(object(),'https://example.org','browser')
        assert any('mode=browser status=content duration=' in record.message for record in caplog.records)
    finally:
        await loader.close()
