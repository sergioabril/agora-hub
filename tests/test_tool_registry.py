import httpx
import pytest
from pydantic import ValidationError

from agora_hub.config import ToolsConfig, load_config
from agora_hub.tools import FileStore, ToolContext, ToolError, ToolOutput, Tools
from agora_hub.tools.images import spec


class Group:
    id = 'search'
    schemas = [spec('web_search', 'Search', query={'type': 'string'})]
    actions = {name: ('search', name) for name in
               ('web_search', 'open_page', 'web_look', 'view_web_images', 'show_web_image')}
    enabled = True
    note = 'Research instruction; cite sources actually read.'
    round_limit = 30

    async def prepare(self, call, context):
        pass

    def limit_reason(self, context):
        return context.state.get('expired')

    async def run(self, call, context):
        if not call.args.get('query'):
            raise ToolError('invalid arguments')
        return ToolOutput('source evidence', [])

    async def cleanup(self, context):
        context.state['cleaned'] = True
        self.cleaned = True

    async def close(self):
        self.closed = True


def registry(tmp_path, config=None, group=None):
    store = FileStore(tmp_path / 'files')
    group = group or Group()
    group.enabled = (config or ToolsConfig()).search.enabled
    return Tools(httpx.AsyncClient(), store, lambda: None, 10, config, search_group=group)


def call(tools, name='web_search', arguments='{"query":"facts"}'):
    return tools.call({'id': 'c1', 'type': 'function', 'function': {'name': name, 'arguments': arguments}})


async def test_old_config_and_nested_defaults(config_dir):
    import yaml
    path = config_dir / 'hub.yaml'
    hub = yaml.safe_load(path.read_text())
    hub.pop('tools', None)
    path.write_text(yaml.safe_dump(hub))
    settings = load_config(config_dir).hub.tools
    assert settings == ToolsConfig()
    assert settings.search.enabled and settings.search.browser.enabled
    assert ToolsConfig(search={'browser': {'enabled': False}}).search.round_limit == 30
    async with httpx.AsyncClient() as http:
        tools = Tools(http, FileStore(config_dir / 'files'), lambda: None, 10, settings)
        specs, note = tools.offer()
        assert {'web_search', 'open_page', 'web_look', 'view_web_images', 'show_web_image'} <= {s['function']['name'] for s in specs}
        assert 'Sources list' in note
        await tools.close()


@pytest.mark.parametrize('value', [
    {'default_round_limit': 0}, {'total_round_limit': -1}, {'search': {'engines': []}},
    {'search': {'engines': ['bing', 'bing']}}, {'search': {'engines': ['other']}},
    {'search': {'time_limit_seconds': 0}}, {'search': {'action_timeout_seconds': -1}},
    {'search': {'page_chars': 1}}, {'search': {'browser': {'idle_seconds': 0}}}, {'search': {'time_limit_seconds': float('inf')}},
])
def test_invalid_config(value):
    with pytest.raises(ValidationError):
        ToolsConfig(**value)


async def test_registry_attempts_failure_and_stale_ownership(tmp_path):
    tools = registry(tmp_path)
    context = ToolContext([])
    for _ in range(30):
        with pytest.raises(ToolError, match='invalid arguments'):
            await tools.run(call(tools, arguments='not json'), context)
    assert context.counters == {'search': 30} and context.total_actions == 30
    specs, note = tools.offer(context)
    assert not any(s['function']['name'] == 'web_search' for s in specs)
    assert 'Research instruction' in note and 'limit' in note
    with pytest.raises(ToolError, match='limit'):
        await tools.run(call(tools), context)
    assert 'web_search' in tools.owned_names and context.total_actions == 31
    await tools.cleanup(context)
    assert not context.state and tools.groups[1].cleaned
    await tools.close()
    assert tools.groups[1].closed


async def test_disabled_and_deadline(tmp_path):
    tools = registry(tmp_path, ToolsConfig(search={'enabled': False}))
    specs, note = tools.offer()
    assert 'Research instruction' not in note
    assert 'web_search' in tools.owned_names
    with pytest.raises(ToolError, match='disabled'):
        await tools.run(call(tools), ToolContext([]))
    tools.groups[1].enabled = True
    context = ToolContext([], state={'expired': 'research time exhausted'})
    assert 'research time exhausted' in tools.offer(context)[1]
    with pytest.raises(ToolError, match='time exhausted'):
        await tools.run(call(tools), context)


async def test_registered_group_controls_actions_and_availability(tmp_path):
    """A family need not use an existing module path, id or function name."""
    class ReportGroup(Group):
        id = 'reports'
        actions = {'fetch_report': ('report', 'fetch')}
        schemas = [spec('fetch_report', 'Read a report', query={'type': 'string'})]
        note = 'Use the report evidence.'
        round_limit = 1

    group = ReportGroup()
    async with httpx.AsyncClient() as http:
        tools = Tools(http, FileStore(tmp_path), lambda: None, 10,
                      ToolsConfig(search={'enabled': False}), search_group=group)
        context = ToolContext([])
        try:
            assert 'fetch_report' in {s['function']['name'] for s in tools.offer()[0]}
            action = call(tools, 'fetch_report')
            assert (action.kind, action.action) == ('report', 'fetch')
            await tools.prepare(action, context)
            assert (await tools.run(action, context)).content == 'source evidence'
            assert context.counters == {'reports': 1}
            assert 'fetch_report' not in {s['function']['name'] for s in tools.offer(context)[0]}
            with pytest.raises(ToolError, match='reports action limit'):
                await tools.run(call(tools, 'fetch_report'), context)
            assert 'fetch_report' in tools.owned_names
            group.enabled = False
            assert 'Use the report evidence.' not in tools.offer()[1]
            with pytest.raises(ToolError, match='reports is disabled'):
                await tools.run(call(tools, 'fetch_report'), ToolContext([]))
        finally:
            await tools.cleanup(context)
            await tools.close()
        assert group.cleaned and group.closed


async def test_package_facade_and_moved_tools_work_outside_repository(tmp_path, monkeypatch):
    import base64
    import importlib
    import json

    facade = importlib.import_module('agora_hub.tools')
    base = importlib.import_module('agora_hub.tools.base')
    store_module = importlib.import_module('agora_hub.tools.store')
    from agora_hub.tools.images import ImageGroup
    from agora_hub.tools.search import SearchGroup

    assert facade.__file__.endswith('tools/__init__.py')
    assert facade.Call is base.Call
    assert facade.ToolContext is base.ToolContext
    assert facade.ToolOutput is base.ToolOutput
    assert facade.ToolError is base.ToolError
    assert facade.FileStore is store_module.FileStore
    monkeypatch.chdir(tmp_path)

    # Existing generation accepts the image server's stored format unchanged.
    png = b'\x89PNG\r\n\x1a\nfixture image bytes'
    image_paths = []
    def image_server(request):
        image_paths.append(request.url.path)
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png).decode()}]})

    def web_server(request):
        return httpx.Response(200, headers={'content-type': 'text/html'},
                              text='<article>' + 'Original source evidence. ' * 20 + '</article>')

    settings = ToolsConfig()
    store = facade.FileStore(tmp_path / 'images')
    group = SearchGroup(settings.search, store, transport=httpx.MockTransport(web_server))
    async with httpx.AsyncClient(transport=httpx.MockTransport(image_server)) as http:
        tools = facade.Tools(http, store, lambda: ('https://images.example/v1', 'image-model'),
                             10, settings, search_group=group)
        assert isinstance(tools.groups[0], ImageGroup)
        assert isinstance(tools.groups[1], SearchGroup)
        context = facade.ToolContext([])
        async def execute(name, **args):
            action = tools.call({'id': name, 'function': {'name': name, 'arguments': json.dumps(args)}})
            await tools.prepare(action, context)
            return await tools.run(action, context)

        try:
            assert tools.owned_names == {
                'generate_image', 'edit_image', 'view_image', 'web_search', 'open_page',
                'web_look', 'view_web_images', 'show_web_image',
            }
            assert {s['function']['name'] for s in tools.offer()[0]} == tools.owned_names
            generated = await execute('generate_image', prompt='A red fox')
            edited = await execute('edit_image', prompt='Make it blue', images=['image_1'])
            viewed = await execute('view_image', images=['image_1', 'image_2'])
            assert viewed.images == generated.images + edited.images
            assert all(store.path(name).read_bytes() == png for name in viewed.images)
            searched = await execute('web_search', query='original evidence', mode='http')
            assert json.loads(searched.content)['access_status'] == 'content'
            assert searched.progress['type'] == 'web_search'
            assert image_paths == ['/v1/images/generations', '/v1/images/edits']
        finally:
            await tools.cleanup(context)
            await tools.close()
