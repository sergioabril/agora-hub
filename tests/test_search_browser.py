"""Actual bundled-shell checks; install matching shell before running this file."""
import asyncio
import base64
from contextlib import suppress
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from agora_hub.tools.search.loader import AccessError, DestinationPolicy, Loader
from agora_hub.tools.search import State, validate_image

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


class FixturePolicy(DestinationPolicy):
    """Explicit test-only allowance for one fixture hostname and port."""
    def __init__(self, port):
        super().__init__()
        self.port = port
        self.resolutions = 0

    async def addresses(self, host, port):
        if host == 'fixture.test' and port == self.port:
            self.resolutions += 1
            return ['127.0.0.1']
        return await super().addresses(host,port)


@pytest.fixture
async def fixture_web():
    hits = []
    tasks = set()
    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            request = (await reader.readuntil(b'\r\n\r\n')).decode()
            path = request.split(' ')[1]
            hits.append(path)
            port = server.sockets[0].getsockname()[1]
            if path == '/slow':
                await asyncio.sleep(30)
            if path == '/image':
                body, mime = PNG, 'image/png'
            elif path == '/redirect-private':
                writer.write(f'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{port}/private\r\nContent-Length: 0\r\n\r\n'.encode())
                await writer.drain()
                return
            else:
                mime = 'text/html'
                body = f'''<title>Fixture</title><body><main><p id="content">Enable JavaScript</p>
                <script>document.cookie='visited=yes';
                document.getElementById('content').textContent='Rendered original evidence. '.repeat(20);
                let im=document.createElement('img');im.src='/image';im.alt='Rendered image';document.body.appendChild(im);
                fetch('http://127.0.0.1:{port}/private').catch(()=>{{}});
                new WebSocket('ws://127.0.0.1:{port}/private');
                navigator.serviceWorker.register('/worker.js').catch(()=>{{}});
                </script></main></body>'''.encode()
                if path == '/clean':
                    body = b'<body><main><p id="cookie"></p><script>document.getElementById("cookie").textContent="Cookie before: "+(document.cookie || "none")+" original evidence ".repeat(20)</script></main></body>'
                if path == '/big':
                    body = b'<body><main><script>for(let i=0;i<4000;i++){let p=document.createElement("p");p.textContent="a".repeat(1000);document.querySelector("main").appendChild(p)}</script></main></body>'
            writer.write(f'HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
            await writer.drain()
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            tasks.discard(task)
    server = await asyncio.start_server(serve,'127.0.0.1',0)
    port = server.sockets[0].getsockname()[1]
    yield f'http://fixture.test:{port}', FixturePolicy(port), hits
    server.close()
    active = list(tasks)
    for task in active:
        task.cancel()
    await asyncio.gather(*active,return_exceptions=True)
    await server.wait_closed()


@pytest.fixture(autouse=True)
async def shell_available():
    async with async_playwright() as p:
        # executable_path names full Chromium, whereas launch uses headless shell.
        browsers = next(parent.parent for parent in Path(p.chromium.executable_path).parents if parent.name.startswith('chromium-'))
        if not list(browsers.glob('chromium_headless_shell-*')):
            pytest.skip('Matching Playwright headless shell is not installed; run playwright install --only-shell chromium')


async def test_real_browser_fallback_images_isolation_security_and_idle(fixture_web):
    base,policy,hits = fixture_web
    loader = Loader(policy=policy,idle_seconds=.05)
    first, second = State(), State()
    try:
        page, attempts = await loader.load(first,base+'/js')
        assert [a['mode'] for a in attempts] == ['http','browser']
        assert page.status == 'content' and 'Rendered original evidence' in page.text
        assert page.images and page.images[0].url == base+'/image'
        # Reader prefers main; images must be inside selected content.
        image_page = await loader.browser.page(first)
        assert await image_page.locator('img').count() == 1
        assert await image_page.locator('img').evaluate('(im)=>im.naturalWidth') == 1
        data = await loader.browser.screenshot(first)
        assert data[:3] == b'\xff\xd8\xff' and len(data) < 2*1024*1024
        validate_image(data,'image/jpeg')
        webp = await image_page.locator('img').evaluate('''async im => {const c=document.createElement('canvas'); c.width=im.naturalWidth;c.height=im.naturalHeight;c.getContext('2d').drawImage(im,0,0);return c.toDataURL('image/webp').split(',')[1]}''')
        validate_image(base64.b64decode(webp),'image/webp')
        clean,_ = await loader.load(second,base+'/clean','browser')
        assert 'Cookie before: none' in clean.text
        assert len(loader.browser.sessions) == 2
        assert '/private' not in hits and '/worker.js' not in hits
        rejected = await loader.browser.load(first,base+'/redirect-private')
        assert rejected.status == 'blocked'
        assert '/private' not in hits
        await loader.cleanup(first)
        await loader.cleanup(second)
        await asyncio.sleep(.12)
        assert loader.browser.process is None and not loader.browser.sessions
    finally:
        await loader.close()
    assert not loader.proxy.tasks


async def test_browser_max_two_queue_cancel_crash_recovery(fixture_web):
    base,policy,hits = fixture_web
    loader = Loader(policy=policy)
    states = [State() for _ in range(3)]
    try:
        await asyncio.gather(*(loader.browser.load(state,base+'/clean') for state in states[:2]))
        queued = asyncio.create_task(loader.browser.load(states[2],base+'/clean'))
        await asyncio.sleep(.05)
        assert len(loader.browser.sessions) == 2 and not queued.done()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert len(loader.browser.sessions) == 2
        # Simulate an owned browser process crash; retained session is rebuilt.
        await loader.browser.process.close()
        page = await loader.browser.load(states[0],base+'/clean')
        assert page.status == 'content' and loader.browser.process.is_connected()
        await loader.cleanup(states[0])
        await loader.cleanup(states[1])
        navigation = asyncio.create_task(loader.browser.load(states[2],base+'/slow'))
        await asyncio.sleep(.1)
        navigation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await navigation
        assert not loader.browser.sessions
    finally:
        await loader.close()


async def test_browser_dom_cap_and_startup_failure_leaves_http(fixture_web,monkeypatch):
    base,policy,hits = fixture_web
    loader = Loader(policy=policy)
    state = State()
    try:
        page = await loader.browser.load(state,base+'/big')
        assert len(page.text) <= 120000 and 'truncated' in page.detail
        await loader.cleanup(state)
        async def broken():
            raise RuntimeError('fixture missing browser executable')
        monkeypatch.setattr(loader.browser,'_start',broken)
        with pytest.raises(AccessError,match='missing browser'):
            await loader.load(state,base+'/js','browser')
        page,attempts = await loader.load(state,base+'/clean','http')
        assert attempts[0]['mode'] == 'http'
        assert not loader.browser.sessions
    finally:
        await loader.close()


async def test_cancel_during_context_startup_closes_created_context(fixture_web, monkeypatch):
    """A cancelled protocol await can race a context already created in Chromium."""
    base, policy, _ = fixture_web
    loader = Loader(policy=policy)
    state = State()
    created = asyncio.Event()
    try:
        await loader.browser._start()
        process = loader.browser.process
        original = process.new_context
        async def delayed_context(**kwargs):
            context = await original(**kwargs)
            created.set()
            await asyncio.sleep(.1)  # context exists before Python receives ownership
            return context
        monkeypatch.setattr(process, 'new_context', delayed_context)
        navigation = asyncio.create_task(loader.browser.load(state, base + '/clean'))
        await created.wait()
        navigation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await navigation
        assert not process.contexts
        assert not loader.browser.sessions and loader.browser.pending == 0
        assert loader.browser.slots._value == 2
        monkeypatch.setattr(process, 'new_context', original)
        page = await loader.browser.load(state, base + '/clean')
        assert page.status == 'content'
    finally:
        await loader.close()


async def test_cancel_during_browser_launch_releases_slot_and_recovers(fixture_web, monkeypatch):
    from playwright.async_api import BrowserType
    base, policy, _ = fixture_web
    loader = Loader(policy=policy)
    state = State()
    started = asyncio.Event()
    launched = []
    original = BrowserType.launch
    async def delayed_launch(browser_type, **kwargs):
        process = await original(browser_type, **kwargs)
        launched.append(process)
        started.set()
        await asyncio.sleep(.1)
        return process
    monkeypatch.setattr(BrowserType, 'launch', delayed_launch)
    try:
        navigation = asyncio.create_task(loader.browser.load(state, base + '/clean'))
        await started.wait()
        navigation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await navigation
        assert not launched[0].is_connected()
        assert loader.browser.playwright is None
        assert not loader.browser.sessions and loader.browser.pending == 0
        assert loader.browser.slots._value == 2
        monkeypatch.setattr(BrowserType, 'launch', original)
        page = await loader.browser.load(state, base + '/clean')
        assert page.status == 'content'
    finally:
        await loader.close()
