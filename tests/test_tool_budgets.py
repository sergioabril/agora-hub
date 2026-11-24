import asyncio
import json

import httpx
import pytest

from agora_hub.config import ToolsConfig
from agora_hub.harness import Failed, Finished, Harness, ToolFinished
from agora_hub.tools import ToolContext
from tests.test_gateway import chats, sse, tool_call, upstream
from tests.test_tool_registry import Group, registry


def calls(*names):
    return {'tool_calls': [dict(tool_call(name, query='facts')['tool_calls'][0], index=i)
                           for i, name in enumerate(names)]}


async def run(tmp_path, replies, config=None, **body):
    requests = []
    tools = registry(tmp_path, config)
    http = httpx.AsyncClient(transport=upstream(requests, replies))
    harness = Harness(http, tools, tools.store)
    turn = await harness.start({'model': 'm', 'messages': [{'role': 'user', 'content': 'research'}], **body},
                               'http://llm/chat/completions', 'r')
    events = [e async for e in harness.events(turn)]
    await http.aclose()
    return events, chats(requests), turn, tools


async def test_family_boundary_sequential_stale_calls(tmp_path, monkeypatch):
    # Each call in the batch counts; the third is recognized and fails after the allowance.
    monkeypatch.setattr(Group, 'round_limit', 2)
    events, requests, turn, _ = await run(tmp_path, [sse(calls('web_search', 'web_search', 'web_search')),
                                                   sse({'content': 'Answer with Sources.'})])
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert len(finished) == 3 and not finished[0].error and finished[2].error
    assert turn.context.total_actions == 3
    assert 'web_search' not in [s['function']['name'] for s in requests[1]['tools']]
    assert 'Research instruction' in requests[1]['messages'][0]['content']
    assert isinstance(events[-1], Finished)


async def test_global_boundary_forced_choice_and_final_stale(tmp_path):
    events, requests, turn, _ = await run(tmp_path, [sse(calls('web_search', 'web_search')),
                                                   sse(calls('web_search'))],
                                         ToolsConfig(total_round_limit=2),
                                         tool_choice={'type': 'function', 'function': {'name': 'web_search'}})
    assert turn.context.total_actions == 2 and len(requests) == 2
    assert 'tools' not in requests[-1] and 'tool_choice' not in requests[-1]
    assert 'sources actually read' in requests[-1]['messages'][0]['content']
    assert isinstance(events[-1], Failed)
    assert not any(isinstance(e, Finished) and e.calls for e in events)


async def test_none_and_disabled_calls_never_leak(tmp_path):
    events, requests, turn, _ = await run(tmp_path, [sse(calls('web_search'))], tool_choice='none')
    assert 'tools' not in requests[0] and turn.context.total_actions == 0
    assert isinstance(events[-1], Failed)
    events, _, turn, _ = await run(tmp_path, [sse(calls('web_search')), sse({'content': 'disabled'})],
                                   ToolsConfig(search={'enabled': False}))
    assert [e for e in events if isinstance(e, ToolFinished)][0].error == 'search is disabled'
    assert events[-1] == Finished([], 'stop')


@pytest.mark.parametrize('enabled', [True, False])
async def test_collision_remains_client_owned_even_disabled(tmp_path, enabled):
    client_tool = Group.schemas[0]
    events, requests, turn, _ = await run(tmp_path, [sse(calls('web_search'))],
                                         ToolsConfig(search={'enabled': enabled}), tools=[client_tool])
    assert events[-1].calls[0]['function']['name'] == 'web_search'
    assert turn.context.total_actions == 0
    assert len([t for t in requests[0]['tools'] if t['function']['name'] == 'web_search']) == 1


async def test_cleanup_on_upstream_start_failure(tmp_path):
    tools = registry(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as http:
        harness = Harness(http, tools, tools.store)
        with pytest.raises(Exception):
            await harness.start({'model': 'm', 'messages': []}, 'http://llm', 'r')
    assert tools.groups[1].cleaned


async def test_disconnect_waits_for_tool_cancellation_cleanup(tmp_path):
    tools = registry(tmp_path)
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def slow(call, context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    tools.groups[1].run = slow
    async with httpx.AsyncClient(transport=upstream([], [sse(calls('web_search'))])) as http:
        harness = Harness(http, tools, tools.store)
        turn = await harness.start({'model': 'm', 'messages': []}, 'http://llm', 'r')
        async def consume():
            return [e async for e in harness.events(turn)]
        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set() and turn.cleaned and tools.groups[1].cleaned


async def test_default_family_ten_attempts(tmp_path):
    tools = registry(tmp_path)
    context = ToolContext([])
    from agora_hub.tools import ToolError
    from tests.test_tool_registry import call
    for _ in range(10):
        with pytest.raises(ToolError):
            await tools.run(call(tools, 'view_image', '{}'), context)
    assert context.counters['images'] == 10
    specs, note = tools.offer(context)
    assert [s['function']['name'] for s in specs] == ['web_search']
    assert 'images action limit reached' in note


async def test_global_ceiling_across_large_batch(tmp_path):
    replies = [sse(calls(*(['web_search'] * 45))), sse({'content': 'partial answer'})]
    events, _, turn, _ = await run(tmp_path, replies)
    assert turn.context.total_actions == 40
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert sum(e.output is not None for e in finished) == 30
    assert finished[-1].error == 'global hub action limit reached'
    assert events[-1] == Finished([], 'stop')


async def test_iteration_backstop_one_final_answer(tmp_path, monkeypatch):
    import agora_hub.harness as module
    monkeypatch.setattr(module, 'MAX_TOOL_ITERATIONS', 2)
    events, requests, turn, _ = await run(tmp_path, [sse(calls('web_search')), sse(calls('web_search')),
                                                   sse({'content': 'final evidence'})])
    assert len(requests) == 3 and turn.context.total_actions == 2
    assert 'tools' not in requests[-1]
    assert events[-1] == Finished([], 'stop')


async def test_required_removed_without_executable_tools(tmp_path):
    events, requests, turn, _ = await run(tmp_path, [sse(calls('view_image')), sse({'content': 'answer'})],
                                         ToolsConfig(default_round_limit=1, search={'enabled': False}),
                                         tool_choice='required')
    assert requests[0]['tool_choice'] == 'required'
    assert 'tools' not in requests[1] and 'tool_choice' not in requests[1]
    assert turn.final and events[-1] == Finished([], 'stop')


async def test_anyio_disconnect_cleans_owned_task(tmp_path):
    import anyio
    tools = registry(tmp_path)
    started = asyncio.Event()
    async def slow(call, context):
        started.set()
        await asyncio.Event().wait()
    tools.groups[1].run = slow
    async with httpx.AsyncClient(transport=upstream([], [sse(calls('web_search'))])) as http:
        harness = Harness(http, tools, tools.store)
        turn = await harness.start({'model': 'm', 'messages': []}, 'http://llm', 'r')
        async with anyio.create_task_group() as tasks:
            async def consume():
                async for _ in harness.events(turn):
                    pass
            tasks.start_soon(consume)
            await started.wait()
            tasks.cancel_scope.cancel()
        assert tools.groups[1].cleaned and turn.cleaned


async def test_real_elapsed_deadline_includes_intervening_model_call(tmp_path):
    from agora_hub.tools.search import SearchGroup
    from agora_hub.tools.search.loader import Loader
    tools = registry(tmp_path, ToolsConfig(search={'time_limit_seconds': .02, 'browser': {'enabled': False}}))
    web_hits = []
    loader = Loader(transport=httpx.MockTransport(lambda request: web_hits.append(request)), browser_enabled=False)
    group = SearchGroup(tools.config.search, tools.store, loader=loader)
    tools.groups[1] = group
    tools.owners.update({name: group for name in group.actions})
    requests = []
    replies = [sse(tool_call('web_search', query='')), sse(tool_call('open_page', url='https://example.org/article')),
               sse({'content': 'Partial answer; research time expired.'})]
    async def model(request):
        requests.append(json.loads(request.content))
        if len(requests) == 2:
            await asyncio.sleep(.04)
        return replies.pop(0)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as http:
            harness = Harness(http, tools, tools.store)
            turn = await harness.start({'model': 'm', 'messages': []}, 'http://llm', 'r')
            events = [event async for event in harness.events(turn)]
        finished = [event for event in events if isinstance(event, ToolFinished)]
        assert len(finished) == 2 and 'elapsed time limit' in finished[1].error
        assert not web_hits and turn.context.total_actions == 2
        assert 'Search elapsed time limit reached' in requests[-1]['messages'][0]['content']
        assert 'web_search' not in {tool['function']['name'] for tool in requests[-1]['tools']}
        assert events[-1] == Finished([], 'stop')
    finally:
        await tools.close()
