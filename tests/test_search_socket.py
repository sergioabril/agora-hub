"""Native acceptance over real loopback sockets, including an owned headless shell.

No MockTransport/ASGITransport: client -> gateway -> fake LLM/image API, and
loader -> pinning proxy -> fixture web server all use TCP. Only the injected
fixture loader/policy may translate an engine URL or allow fixture loopback.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import socket
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response
import httpx
import pytest
import uvicorn

from agora_hub.accounts import Accounts
from agora_hub.config import load_config
from agora_hub.gateway import create_gateway
from agora_hub.tools.search import SearchGroup
from agora_hub.tools.search.loader import Loader
from tests.test_gateway import sse, tool_call
from tests.test_search_browser import FixturePolicy, PNG


@asynccontextmanager
async def listening(app):
    """Reserve a spare loopback port without a bind/close race; own only this task."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='on', timeout_graceful_shutdown=3))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()
                    raise AssertionError('fixture server stopped before startup')
                await asyncio.sleep(.01)
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


class FixtureLoader(Loader):
    def __init__(self, base):
        super().__init__(policy=FixturePolicy(int(base.rsplit(':', 1)[1])), idle_seconds=.05)
        self.base = base
        self.cleaned = asyncio.Event()

    async def load(self, state, url, mode='auto'):
        if url.startswith('https://www.bing.com/search?'):
            url = self.base + '/search'
        return await super().load(state, url, mode)

    async def cleanup(self, state):
        await super().cleanup(state)
        self.cleaned.set()


@pytest.fixture
async def wire(config_dir):
    web, llm = FastAPI(), FastAPI()
    hits, requests, edits, replies = [], [], [], []
    navigating, navigation_gone = asyncio.Event(), asyncio.Event()
    fixture = {}

    @web.get('/{path:path}')
    async def page(path: str, request: Request):
        hits.append('/' + path)
        if path == 'slow':
            navigating.set()
            # Do not send response headers: Chromium is inside page.goto.
            async with asyncio.timeout(10):
                while not await request.is_disconnected():
                    await asyncio.sleep(.01)
            navigation_gone.set()
            return Response('<main>slow</main>', media_type='text/html')
        if path == 'image.png':
            return Response(PNG, media_type='image/png')
        evidence = '<p>' + 'Original fixture evidence. ' * 40 + '</p>'
        if path == 'search':
            html = evidence + '<a href="/article">Original article</a>'
        elif path == 'js':
            html = '''<p id="e">Enable JavaScript</p><script>
            document.getElementById('e').textContent='Rendered original evidence. '.repeat(40);
            let im=document.createElement('img'); im.src='/image.png'; im.alt='Original chart';
            document.querySelector('main').appendChild(im);</script>'''
        else:
            html = evidence + '<a href="/js">Rendered source</a><img src="/image.png" alt="Original chart">'
        return Response('<title>Fixture source</title><main>' + html + '</main>', media_type='text/html')

    @llm.post('/v1/chat/completions')
    async def model(request: Request):
        requests.append((time.monotonic(), await request.json()))
        assert replies, 'unexpected upstream model iteration'
        delay, reply = replies.pop(0)
        if delay:
            await asyncio.sleep(delay)
        return Response(reply.content, media_type='text/event-stream')

    @llm.post('/v1/images/edits')
    async def edit(request: Request):
        edits.append(await request.body())
        return {'data': [{'b64_json': base64.b64encode(PNG).decode()}]}

    async with listening(web) as web_url, listening(llm) as llm_url:
        base = web_url.replace('127.0.0.1', 'fixture.test')
        config = load_config(config_dir)
        for name in ('pro', 'images'):
            config.deployments[name].endpoint = llm_url + '/v1'
        class Controller:
            def serving(self, family):
                return [('pro', ['fixture-model'])] if family == 'llm' else [('images', ['fixture-image'])]
        accounts = Accounts(config_dir / 'agora.db')
        token = accounts.link(accounts.add('Socket test')['invite_code'], 'isolated fixture')['token']
        app = create_gateway(config, Controller(), accounts)
        tools = app.state.tools
        await tools.groups[1].close()
        loader = FixtureLoader(base)
        group = SearchGroup(config.hub.tools.search, tools.store, loader=loader)
        tools.groups[1] = group
        tools.owners.update({name: group for name in group.actions})
        async with listening(app) as hub, httpx.AsyncClient(base_url=hub, headers={'Authorization': 'Bearer ' + token}, timeout=15, trust_env=False) as client:
            fixture.update(client=client, base=base, loader=loader, group=group, tools=tools,
                           hits=hits, requests=requests, edits=edits, replies=replies, navigating=navigating, navigation_gone=navigation_gone)
            yield fixture
        assert not loader.browser.sessions and not loader.proxy.tasks


def queue(wire, *deltas, delay=0):
    wire['replies'].append((delay, sse(*deltas)))


async def streamed(wire, **body):
    events = []
    async with wire['client'].stream('POST', '/openai/v1/responses', json={'model': 'agora', 'input': 'Research fixture sources', 'stream': True, **body}) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if line.startswith('data: ') and line != 'data: [DONE]':
                events.append(json.loads(line[6:]))
    return events, events[-1]['response']


async def test_socket_research_browser_hidden_pixels_attribution_cache_and_edit_replay(wire):
    base = wire['base']
    steps = [('web_search', {'query': 'fixture facts'}), ('open_page', {'ref': 'p1:l1'}),
             ('open_page', {'url': base + '/js'}), ('view_web_images', {'images': ['p3:i1']}),
             ('web_look', {}), ('open_page', {'ref': 'p2'}),
             ('open_page', {'url': base + '/pasted'}), ('show_web_image', {'image': 'image_1'})]
    for name, args in steps:
        queue(wire, tool_call(name, **args))
    answer = 'Checked fixture evidence.\n\nSources:\n- ' + base + '/article\n- ' + base + '/js\n- ' + base + '/pasted'
    queue(wire, {'content': answer})
    events, result = await streamed(wire, input='Research sources, then open this pasted link: ' + base + '/pasted')
    assert result['status'] == 'completed'
    output = result['output']
    assert [item['type'] for item in output] == ['web_search_call'] * 7 + ['web_image_call', 'message']
    assert [item['status'] for item in output] == ['completed'] * 9
    assert [item['action']['type'] for item in output[:-1]] == [name for name, _ in steps]
    for item in output[:-1]:
        assert item['label'] == item['action']['label'] and item['label']
        related = [e['type'] for e in events if e.get('item_id') == item['id'] or e.get('item', {}).get('id') == item['id']]
        family = 'web_image_call' if item['type'] == 'web_image_call' else 'web_search_call'
        assert related.index('response.output_item.added') < related.index('response.' + family + '.in_progress')
        assert related.index('response.' + family + '.completed') < related.index('response.output_item.done')
    assert all('result' not in item for item in output[:7])
    shown = output[7]
    assert shown['source_page_url'] == base + '/js' and shown['image_url'] == base + '/image.png'
    assert shown['result'] == base64.b64encode(PNG).decode() and shown['output_format'] == 'png'
    assert output[-1]['content'][0]['text'] == answer
    payloads = [body for _, body in wire['requests']]
    assert len(payloads) == 9
    assert [event['sequence_number'] for event in events] == sorted({event['sequence_number'] for event in events})
    positions = []
    for item in output[:-1]:
        positions += [next(i for i, e in enumerate(events) if e['type'] == kind and e.get('item', {}).get('id') == item['id'])
                      for kind in ('response.output_item.added', 'response.output_item.done')]
    assert positions == sorted(positions)
    for index, payload in enumerate(payloads):
        completed = [m for m in payload['messages'] if m['role'] == 'tool']
        assert len(completed) == index
        calls = [c for m in payload['messages'] if m['role'] == 'assistant' for c in m.get('tool_calls', [])]
        assert [m['tool_call_id'] for m in completed] == [c['id'] for c in calls]
        assert [c['function']['name'] for c in calls] == [name for name, _ in steps[:index]]
    tool_results = [m['content'] for m in payloads[-1]['messages'] if m['role'] == 'tool']
    rendered = json.loads(tool_results[2])
    assert rendered['mode'] == 'browser' and [a['mode'] for a in rendered['attempts']] == ['http', 'browser']
    assert 'Rendered original evidence' in rendered['text']
    assert wire['hits'].count('/article') == 1  # revisit uses cached p2
    assert wire['hits'].count('/search') == wire['hits'].count('/pasted') == 1
    assert any(p['type'] == 'image_url' for m in payloads[4]['messages'] if isinstance(m.get('content'), list) for p in m['content'])
    assert not wire['loader'].browser.sessions  # response teardown, before hub shutdown
    before = list(wire['hits'])
    queue(wire, tool_call('edit_image', images=['image_1'], prompt='Make this blue'))
    queue(wire, {'content': 'Edited the stored found image.'})
    _, edited = await streamed(wire, input=[{'type': 'web_image_call', 'id': shown['id'], 'status': 'completed'}, {'role': 'user', 'content': 'Make this blue'}])
    assert edited['status'] == 'completed' and edited['output'][0]['type'] == 'image_generation_call'
    assert len(wire['edits']) == 1 and PNG in wire['edits'][0] and shown['id'][3:].encode() in wire['edits'][0]
    assert wire['hits'] == before  # stored bytes, no web refetch
    assert not wire['replies']


async def test_socket_elapsed_research_expiry_across_model_delay(wire):
    wire['group'].config.time_limit_seconds = .15
    queue(wire, tool_call('open_page', url=wire['base'] + '/article', mode='http'))
    queue(wire, tool_call('open_page', url=wire['base'] + '/must-not-fetch'), delay=.3)
    queue(wire, {'content': 'Partial answer from checked evidence; research time expired.'})
    _, result = await streamed(wire)
    assert result['status'] == 'completed'
    assert result['output'][1]['status'] == 'failed'
    assert 'elapsed time limit' in result['output'][1]['error']['message']
    assert '/must-not-fetch' not in wire['hits']
    assert wire['requests'][2][0] - wire['requests'][1][0] >= .3
    final = wire['requests'][-1][1]
    assert 'Search elapsed time limit reached' in final['messages'][0]['content']
    assert not {'open_page', 'web_search'} & {t['function']['name'] for t in final['tools']}


@pytest.mark.parametrize('limit', ['family', 'global'])
@pytest.mark.parametrize('refuse', [False, True])
async def test_socket_budget_final_synthesis_and_refusal(wire, limit, refuse):
    if limit == 'family':
        wire['group'].round_limit = 1
    else:
        wire['tools'].config.total_round_limit = 1
    queue(wire, tool_call('open_page', url=wire['base'] + '/article', mode='http'))
    queue(wire, tool_call('open_page', url=wire['base'] + '/stale') if refuse else {'content': 'Partial checked answer. Sources: ' + wire['base'] + '/article'})
    if refuse and limit == 'family':
        queue(wire, {'content': 'Partial answer after stale search refusal.'})
    _, result = await streamed(wire, tool_choice={'type': 'function', 'name': 'open_page'})
    # With family exhausted images remain usable; a stale call must fail as hub-owned.
    # Exhaust the remaining family too when testing the final iteration refusal.
    if refuse and limit == 'family':
        assert result['output'][1]['status'] == 'failed' and result['status'] == 'completed'
        assert 'action limit' in result['output'][1]['error']['message']
    else:
        assert result['status'] == ('failed' if refuse else 'completed')
    assert '/stale' not in wire['hits']
    final = wire['requests'][1][1]
    assert 'tool_choice' not in final
    assert 'open_page' not in {t['function']['name'] for t in final.get('tools', [])}
    assert 'sources' in final['messages'][0]['content'].lower()
    assert len(wire['requests']) == (3 if refuse and limit == 'family' else 2)
    assert not wire['replies']


@pytest.mark.parametrize('stream', [False, True])
async def test_socket_disconnect_mid_navigation_cleans_owned_resources(wire, stream):
    queue(wire, tool_call('open_page', url=wire['base'] + '/slow', mode='browser'))
    client = wire['client']
    body = json.dumps({'model': 'agora', 'input': 'Open slow fixture', 'stream': stream}).encode()
    reader, writer = await asyncio.open_connection(client.base_url.host, client.base_url.port)
    try:
        writer.write((f'POST /openai/v1/responses HTTP/1.1\r\nHost: localhost\r\n'
                      f'Authorization: {client.headers["authorization"]}\r\n'
                      f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body)
        await writer.drain()
        await asyncio.wait_for(wire['navigating'].wait(), 5)
        assert wire['loader'].browser.sessions  # actual navigation owns a browser context
        if stream:
            headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
            assert b'200 OK' in headers and b'text/event-stream' in headers
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(wire['loader'].cleaned.wait(), 3)
        await asyncio.wait_for(wire['navigation_gone'].wait(), 3)
        async with asyncio.timeout(3):
            while wire['loader'].browser.process is not None or wire['loader'].proxy.tasks:
                await asyncio.sleep(.02)
        assert not wire['loader'].browser.sessions
        assert wire['loader'].browser.pending == 0 and wire['loader'].browser.slots._value == 2
        assert len(wire['requests']) == 1 and not wire['replies']
    finally:
        writer.close()
        await writer.wait_closed()
