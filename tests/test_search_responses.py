import base64
import json

import httpx
import pytest

from agora_hub.harness import Harness
from agora_hub.responses import chat_request, respond
from agora_hub.tools import ToolOutput
from agora_hub.tools.images import spec
from tests.test_gateway import PNG, sse, sse_events, tool_call, upstream
from tests.test_tool_registry import Group, registry


class MediaGroup(Group):
    schemas = [spec(name, name) for name in ('web_search', 'open_page', 'web_look', 'view_web_images', 'show_web_image')]

    def __init__(self, store):
        self.store = store

    async def prepare(self, call, context):
        call.progress.update(call.args)
        call.progress['label'] = 'Showing example.org image' if call.action == 'show_web_image' else 'Opening example.org'

    async def run(self, call, context):
        if call.action == 'open_page':
            return ToolOutput('Article evidence', [], progress=call.progress)
        name = self.store.save(PNG, call.id)
        context.label(name)
        media = None
        if call.action == 'show_web_image':
            media = {'name': name, 'source_page_url': 'https://example.org/article', 'image_url': 'https://example.org/image.png'}
            self.store.save_call(call.id, call.tool_call)
            self.store.save_metadata(call.id, {'type': 'web_image_call', **media})
        return ToolOutput('image inspected', [name], progress=call.progress, media=media, web_images=[name])


@pytest.mark.parametrize('stream', [False, True])
async def test_structured_progress_inspection_and_published_image(tmp_path, stream):
    tools = registry(tmp_path)
    tools.groups[1] = group = MediaGroup(tools.store)
    tools.owners.update({name: group for name in group.actions})
    requests, replies = [], [sse(tool_call('open_page', url='https://example.org/article')),
                             sse(tool_call('web_look')), sse(tool_call('view_web_images', images=['p1:i1'])),
                             sse(tool_call('show_web_image', image='p1:i1')), sse({'content': 'Checked answer.\n\nSources: https://example.org/article'})]
    async with httpx.AsyncClient(transport=upstream(requests, replies)) as http:
        harness = Harness(http, tools, tools.store)
        response = await respond(harness, tools.store, {'model': 'm', 'input': 'research', 'stream': stream}, 'http://llm', 'r')
        if stream:
            chunks = ''.join([chunk async for chunk in response.body_iterator])
            events = sse_events(chunks)
            result = events[-1]['response']
            assert any(e['type'] == 'response.web_image_call.in_progress' for e in events)
            assert any(e['type'] == 'response.web_image_call.completed' for e in events)
        else:
            result = json.loads(response.body)
    assert result['status'] == 'completed'
    assert [i['type'] for i in result['output']] == ['web_search_call'] * 3 + ['web_image_call', 'message']
    opening, screenshot, inspection, shown, _ = result['output']
    assert opening['action']['type'] == 'open_page' and opening['action']['url'] == 'https://example.org/article'
    assert opening['label'] == 'Opening example.org'
    assert not any('result' in i for i in (opening, screenshot, inspection))
    assert shown['result'] == base64.b64encode(PNG).decode() and shown['output_format'] == 'png'
    assert shown['source_page_url'] == 'https://example.org/article'
    assert shown['image_url'] == 'https://example.org/image.png'
    replay = chat_request({'model': 'm', 'input': [{'type': 'web_image_call', 'id': shown['id'], 'status': 'completed', 'result': None},
                                                {'role': 'user', 'content': 'edit this'}]}, tools.store)
    assert replay['messages'][0]['tool_calls'][0]['function']['name'] == 'show_web_image'
    assert 'Source: https://example.org/article' in replay['messages'][1]['content']
    # Stored replay preserves normal image labels and supplies pixels to later editing.
    async with httpx.AsyncClient(transport=upstream([], [sse({'content': 'ready'})])) as http:
        harness = Harness(http, tools, tools.store)
        turn = await harness.start(replay, 'http://llm', 'r2')
        name = turn.context.images[0]
        assert name == tools.store.find(shown['id'][3:]) and name in turn.attached
        edit = tools.call({'id': 'edit', 'function': {'name': 'edit_image', 'arguments': '{"images":["image_1"],"prompt":"blue"}'}})
        assert tools.groups[0]._files(edit, turn.context) == [name]
        await harness.cleanup(turn)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('name', ['web_search', 'show_web_image'])
async def test_failed_progress_explicit_error(tmp_path, stream, name):
    tools = registry(tmp_path)
    tools.groups[1].schemas = [*Group.schemas, spec('show_web_image', 'Show image')]
    async with httpx.AsyncClient(transport=upstream([], [sse(tool_call(name)), sse({'content': 'Cannot establish it.'})])) as http:
        response = await respond(Harness(http, tools, tools.store), tools.store,
                                 {'model': 'm', 'input': 'research', 'stream': stream}, 'http://llm', 'r')
        if stream:
            events = sse_events(''.join([chunk async for chunk in response.body_iterator]))
            assert any(e['type'] == ('response.web_image_call.failed' if name == 'show_web_image' else 'response.web_search_call.failed') for e in events)
            result = events[-1]['response']
        else:
            result = json.loads(response.body)
    assert result['output'][0]['status'] == 'failed'
    assert result['output'][0]['error']['message'] == 'invalid arguments'


async def test_eight_web_pixels_keep_user_pixels_and_review(tmp_path):
    tools = registry(tmp_path)
    async with httpx.AsyncClient(transport=upstream([], [sse({'content': 'ok'})])) as http:
        harness = Harness(http, tools, tools.store)
        user_name = tools.store.save(PNG)
        turn = await harness.start({'model': 'm', 'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': tools.store.data_url(user_name)}}]}]}, 'http://llm', 'r')
        names = [tools.store.save(PNG, f'{i:032x}') for i in range(12)]
        turn.web_names.update(names)
        turn.visible.update(names)
        turn.messages += harness._shown(names, turn)
        harness._cap_web_images(turn)
        parts = [part for m in turn.messages if isinstance(m.get('content'), list) for part in m['content']]
        assert sum(p['type'] == 'image_url' for p in parts) == 9  # user pixels plus eight web-only parts
        assert len(turn.context.images) == 13 and len(turn.web_attached) == 8
        assert any(p.get('text') == '[image_2, not attached]' for p in parts)
        turn.visible.add(names[0])
        turn.messages += harness._shown([names[0]], turn)
        harness._cap_web_images(turn)
        assert names[0] in turn.attached and len(turn.web_attached) == 8
        await harness.cleanup(turn)


@pytest.mark.parametrize('stream', [False, True])
async def test_real_search_group_http_progress_image_and_offline_replay(tmp_path, stream):
    """Exercise the actual SearchGroup via an isolated HTTP transport, without external fetches."""
    import struct
    import zlib
    from agora_hub.config import SearchConfig
    from agora_hub.tools.search import SearchGroup
    from agora_hub.tools.search.loader import Loader
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b'')
    fetched = []
    def web(request):
        fetched.append(str(request.url))
        if request.url.path == '/photo.png':
            return httpx.Response(200, content=png, headers={'content-type': 'image/png'})
        html = '<main><h1>Checked evidence</h1><p>' + ('Reliable fixture evidence. ' * 30) + '</p><a href="https://example.org/article">Article</a><img src="https://example.org/photo.png" alt="Chart"></main>'
        return httpx.Response(200, text=html, headers={'content-type': 'text/html'})
    tools = registry(tmp_path)
    loader = Loader(transport=httpx.MockTransport(web), browser_enabled=False)
    group = SearchGroup(SearchConfig(browser={'enabled': False}), tools.store, loader=loader)
    tools.groups[1] = group
    tools.owners.update({name: group for name in group.actions})
    replies = [sse(tool_call('web_search', query='facts', mode='http')),
               sse(tool_call('open_page', ref='p1:l1')),
               sse(tool_call('view_web_images', images=['p2:i1'])),
               sse(tool_call('show_web_image', image='image_1')),
               sse({'content': 'Checked evidence.\n\nSources:\n- https://example.org/article'})]
    try:
        async with httpx.AsyncClient(transport=upstream([], replies)) as http:
            response = await respond(Harness(http, tools, tools.store), tools.store,
                                     {'model': 'm', 'input': 'research', 'stream': stream}, 'http://llm', 'r')
            result = sse_events(''.join([s async for s in response.body_iterator]))[-1]['response'] if stream else json.loads(response.body)
        assert result['status'] == 'completed'
        assert [i['status'] for i in result['output']] == ['completed'] * 5
        assert result['output'][0]['action']['type'] == 'web_search'
        assert result['output'][0]['label'] == 'Searching: facts'
        shown = result['output'][3]
        assert shown['type'] == 'web_image_call' and shown['source_page_url'] == 'https://example.org/article'
        assert shown['result'] == base64.b64encode(png).decode()
        assert fetched == ['https://www.bing.com/search?q=facts', 'https://example.org/article', 'https://example.org/photo.png']
        replay = chat_request({'model': 'm', 'input': [{'type': 'web_image_call', 'id': shown['id'], 'status': 'completed'}]}, tools.store)
        assert replay['messages'][0]['tool_calls'][0]['function']['name'] == 'show_web_image'
        assert 'https://example.org/photo.png' in replay['messages'][1]['content']
        replay['messages'].append({'role': 'user', 'content': 'Make the found image blue.'})
        edits = []
        async with httpx.AsyncClient(transport=upstream(edits, [sse(tool_call('edit_image', images=['image_1'], prompt='Make it blue')),
                                                              sse({'content': 'Edited.'})])) as image_client:
            tools.groups[0].client = image_client
            tools.groups[0].image_target = lambda: ('http://images/v1', 'image-model')
            edit_harness = Harness(image_client, tools, tools.store)
            edit_turn = await edit_harness.start(replay, 'http://llm/chat/completions', 'followup')
            edit_events = [event async for event in edit_harness.events(edit_turn)]
            from agora_hub.harness import ToolFinished
            edited = next(e for e in edit_events if isinstance(e, ToolFinished))
            assert edited.call.action == 'edit' and edited.output and not edited.error
            multipart = next(request for request in edits if request.url.path.endswith('/images/edits'))
            assert png in multipart.content and shown['id'][3:].encode() in multipart.content
            assert tools.store.call(edited.call.id)['function']['name'] == 'edit_image'
        assert len(fetched) == 3  # replay/edit never redownloads the website or image
    finally:
        await tools.close()
