import asyncio
import base64
import json
import secrets

import httpx
import pytest

from agora_hub.config import SearchConfig
from agora_hub.tools.base import Call, ToolContext, ToolError
from agora_hub.tools.store import FileStore
from agora_hub.tools.search import SearchGroup, validate_image
from agora_hub.tools.search.loader import AccessError

# Real one-pixel PNG, including CRC/deflate integrity.
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


def call(name, **args):
    raw = {'id':'call_test','type':'function','function':{'name':name,'arguments':json.dumps(args)}}
    return Call(secrets.token_hex(16), raw, 'search', name, '', [], args)


def fixture(request):
    path = request.url.path
    if path == '/image':
        return httpx.Response(200, headers={'content-type':'image/png'}, content=PNG)
    if path == '/failure':
        raise httpx.ConnectError('fixture connection failed')
    if 'bing' in request.url.host:
        html = '<title>Access denied</title><p>Verify you are human</p>'
    elif 'duckduckgo' in request.url.host:
        html = '<main><h1>Search results</h1><p>'+ 'Search result introduction. '*8 + '</p><a href="https://example.org/one">First original source</a></main>'
    else:
        html = '<article><h1>Source '+path+'</h1><p>'+ 'Original source evidence. '*20 + '</p><a href="/two">Second source</a><img src="/image" alt="A chart"><table>' + '<tr><td>City</td><td>100</td></tr>'*2000+'</table></article>'
    return httpx.Response(200, headers={'content-type':'text/html'}, text=html)


@pytest.fixture
def group(tmp_path):
    return SearchGroup(SearchConfig(browser={'enabled':False}),FileStore(tmp_path),transport=httpx.MockTransport(fixture))


async def test_search_follow_two_links_revisit_paginate_direct_and_publish(group):
    context = ToolContext([])
    try:
        result = json.loads((await group.run(call('web_search',query='cities'),context)).content)
        assert result['attempts'][0]['engine'] == 'bing' and result['attempts'][-1]['engine'] == 'duckduckgo'
        assert result['access_status'] == 'content'
        one = json.loads((await group.run(call('open_page', ref=result['links'][0]['ref']),context)).content)
        two = json.loads((await group.run(call('open_page', ref=one['links'][0]['ref']),context)).content)
        assert two['final_url'].endswith('/two')
        again = json.loads((await group.run(call('open_page',ref=one['snapshot'],offset=one['next_offset']),context)).content)
        assert again['snapshot'] == one['snapshot'] and again['offset'] == one['next_offset']
        direct = json.loads((await group.run(call('open_page',url='https://example.org/pasted'),context)).content)
        assert direct['final_url'].endswith('/pasted')
        viewed = await group.run(call('view_web_images', images=[direct['images'][0]['ref']]), context)
        assert viewed.media is None and viewed.web_images == viewed.images
        published_call = call('show_web_image', image='image_1')
        published = await group.run(published_call, context)
        assert published.media['name'].startswith(published_call.id)
        assert published.media['source_page_url'] == 'https://example.org/pasted'
        assert group.store.metadata(published_call.id)['image_url'] == 'https://example.org/image'
        assert group.store.call(published_call.id) == published_call.tool_call
        # A label from an earlier response replays provenance from disk.
        following = ToolContext([published.media['name']])
        repeated = await group.run(call('show_web_image',image='image_1'), following)
        assert repeated.media['image_url'] == 'https://example.org/image'
        await group.cleanup(following)
    finally:
        await group.cleanup(context)
        await group.close()
    assert 'search' not in context.state


async def test_failure_preserves_cached_current_and_explicit_refresh(group):
    context = ToolContext([])
    try:
        first = json.loads((await group.run(call('open_page',url='https://example.org/one'),context)).content)
        with pytest.raises(ToolError, match='failed'):
            await group.run(call('open_page',url='https://example.org/failure'),context)
        state = context.state['search']
        assert state.pages.current == first['snapshot']
        cached = json.loads((await group.run(call('open_page',ref=first['snapshot']),context)).content)
        assert cached['snapshot'] == first['snapshot']
        fresh = json.loads((await group.run(call('open_page',ref=first['snapshot'],mode='http'),context)).content)
        assert fresh['snapshot'] != first['snapshot']
        with pytest.raises(ToolError, match='disabled'):
            await group.run(call('open_page',url='https://example.org/one',mode='browser'),context)
        state.started -= 181
        assert group.limit_reason(context)
        with pytest.raises(ToolError, match='time limit'):
            await group.run(call('open_page',ref=first['snapshot']),context)
    finally:
        await group.cleanup(context)
        await group.close()


@pytest.mark.parametrize('name,args', [
    ('web_search', {'query':''}), ('web_search',{'query':'x','engine':'unknown'}),
    ('open_page',{}), ('open_page',{'url':'https://e.org','ref':'p1'}),
    ('open_page',{'ref':'p100'}), ('open_page',{'url':'https://e.org','offset':True}),
    ('view_web_images',{'images':[]}), ('show_web_image',{'image':'image_55'}),
])
async def test_argument_validation(group,name,args):
    context = ToolContext([])
    try:
        with pytest.raises(ToolError):
            await group.run(call(name,**args),context)
    finally:
        await group.cleanup(context)
        await group.close()


def test_image_validation_rejects_malformed_and_mismatched():
    validate_image(PNG, 'image/png')
    for data,mime in [(PNG,'image/jpeg'),(PNG[:20],'image/png'),(b'\xff\xd8\xfffake\xff\xd9','image/jpeg'),(b'<svg/>','image/svg+xml')]:
        with pytest.raises((AccessError, ValueError)):
            validate_image(data,mime)


async def test_cancel_action_propagates_and_cleans_browser(tmp_path):
    class SlowBrowser:
        cleaned = 0
        async def load(self, *args):
            await asyncio.sleep(100)
        async def cleanup(self, state):
            self.cleaned += 1
        async def close(self): pass
    browser = SlowBrowser()
    group = SearchGroup(SearchConfig(), FileStore(tmp_path),browser=browser)
    context = ToolContext([])
    task = asyncio.create_task(group.run(call('open_page',url='https://example.org',mode='browser'),context))
    await asyncio.sleep(.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert browser.cleaned >= 1
    await group.cleanup(context)
    await group.close()


async def test_disabled_schema_and_budget_clock(tmp_path):
    group = SearchGroup(SearchConfig(enabled=False),FileStore(tmp_path))
    assert not group.schemas and not group.note and len(group.actions) == 5
    assert group.limit_reason(ToolContext([])) is None
    await group.close()


async def test_screenshot_can_be_reviewed_but_never_published(tmp_path):
    from agora_hub.tools.search.reader import extract
    class PixelBrowser:
        async def load(self,state,url,scroll=None):
            return extract('<main><p>'+ 'Rendered chart evidence. '*20+'</p></main>',url,url,'browser')
        async def screenshot(self,state):
            return PNG
        async def cleanup(self,state): pass
        async def close(self): pass
    group = SearchGroup(SearchConfig(),FileStore(tmp_path),transport=httpx.MockTransport(fixture),browser=PixelBrowser())
    context = ToolContext([])
    try:
        await group.run(call('open_page',url='https://example.org/chart',mode='http'),context)
        looked = await group.run(call('web_look'),context)
        assert len(looked.content) <= 12000 and looked.media is None
        reviewed = await group.run(call('view_web_images',images=['image_1']),context)
        assert reviewed.images == looked.images
        with pytest.raises(ToolError,match='model-only'):
            await group.run(call('show_web_image',image='image_1'),context)
    finally:
        await group.cleanup(context)
        await group.close()


async def test_null_optional_arguments_are_ignored(group):
    context = ToolContext([])
    try:
        page = json.loads((await group.run(call('open_page', url='https://example.org/one', ref=None, mode=None,
                                                offset=None), context)).content)
        assert page['access_status'] == 'content'
        again = json.loads((await group.run(call('open_page', url='', ref=page['snapshot']), context)).content)
        assert again['snapshot'] == page['snapshot']
    finally:
        await group.cleanup(context)
