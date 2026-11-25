import json
from dataclasses import replace

import pytest
from agora_hub.tools.search.reader import extract, Pages, render


def test_generic_reader_base_fragments_tables_and_noise():
    page = extract('''<title>Report</title><base href="https://cdn.example/x/"><nav>noise</nav>
    <main><h1>Population</h1><p>Useful facts and details about the population of all these countries and cities.</p>
    <table><tr><th>City</th><th>People</th></tr><tr><td>Madrid</td><td>3000000</td></tr></table>
    <a href="../story#table">Story</a><img src="photo.png" alt="Chart"><p hidden>secret</p><style>noise</style></main>''',
    'https://example.org/start', 'https://example.org/final', 'http')
    assert page.links[0].url == 'https://cdn.example/story#table'
    assert page.images[0].url == 'https://cdn.example/x/photo.png'
    assert 'City | People' in page.text and 'Madrid | 3000000' in page.text
    assert 'noise' not in page.text and 'secret' not in page.text
    assert page.final_url == 'https://example.org/final'


@pytest.mark.parametrize('html,status', [
    ('<title>Just a moment</title><p>Verify you are human</p>', 'blocked'),
    ('<title>Before you continue to Google</title>' + '<p>Privacy settings</p>'*500, 'consent'),
    ('<main>Enable JavaScript to continue</main>', 'empty'),
    ('<article><h1>How CAPTCHA works</h1><p>' + 'An ordinary article discusses CAPTCHA research and useful public facts. '*100 + '</p></article>', 'content'),
])
def test_access_classification(html, status):
    assert extract(html, 'https://example.org', 'https://example.org', 'http').status == status


def test_immutable_snapshots_eviction_and_bounded_pagination():
    pages = Pages()
    raw = '<p>' + 'Article row data. '*10000 + '</p>' + ''.join(f'<a href="/{i}">link</a>' for i in range(1000))
    first = pages.add(extract(raw, 'https://example.org', 'https://example.org', 'http'))
    result = render(first)
    parsed = json.loads(result)
    assert len(result) <= 12000 and parsed['next_offset'] > 0
    next_page = json.loads(render(first, parsed['next_offset']))
    assert next_page['offset'] == parsed['next_offset']
    assert first.omitted_links == 880
    assert len(first.text) == 120000
    for i in range(31):
        pages.add(replace(first, text='Small article '+str(i)))
    with pytest.raises(ValueError, match='evicted'):
        pages.resolve('p1:l1')
    assert len(pages.items) == 30


def test_bad_offset_and_minimum_output_bound():
    page = Pages().add(extract('<p>'+ ('"\\\n' * 10000) + '</p>', 'https://e.org', 'https://e.org', 'http'))
    assert len(render(page, cap=1000)) <= 1000
    with pytest.raises(ValueError):
        render(page, -1)


def test_failed_snapshots_cannot_evict_last_successful_current():
    pages = Pages()
    first = pages.add(extract('<article>'+ 'Original content. '*100+'</article>','https://e.org','https://e.org','http'))
    for _ in range(40):
        pages.add(replace(first,status='blocked'))
    assert pages.current == first.id and pages.resolve(first.id) == first
    assert len(pages.items) == 30


def test_hidden_parent_with_styled_child():
    page = extract('<main><p>Visible text that is long enough to count as readable page content in this small test page.</p>'
                   '<div style="display:none"><span style="color:red">secret</span></div></main>',
                   'https://example.org', 'https://example.org', 'http')
    assert page.status == 'content' and 'secret' not in page.text


def test_listing_articles_article_header_and_lazy_images():
    filler = 'Readable sentence for the extraction length check. ' * 3
    page = extract('<header>Site menu</header><article><h2>Repo one</h2><p>' + filler + '</p></article>'
                   '<article><header><h1>Repo two</h1><time>24 Nov 2026</time></header><p>' + filler + '</p></article>'
                   '<img src="data:image/gif;base64,R0lGOD" data-src="/real.jpg"><img srcset="/a.png 1x, /b.png 2x">',
                   'https://example.org', 'https://example.org', 'http')
    assert 'Repo one' in page.text and 'Repo two' in page.text and '24 Nov 2026' in page.text
    assert 'Site menu' not in page.text
    assert [i.url for i in page.images] == ['https://example.org/real.jpg', 'https://example.org/a.png']


def test_image_references_get_room_beside_many_links():
    html = '<main><p>' + 'Text. ' * 50 + '</p>' + ''.join(f'<a href="/story/{i}">Story {i}</a>' for i in range(200)) \
        + ''.join(f'<img src="/photo/{i}.jpg" alt="Photo {i}">' for i in range(50)) + '</main>'
    result = json.loads(render(Pages().add(extract(html, 'https://example.org', 'https://example.org', 'http'))))
    assert result["links"] and result["images"] and len(json.dumps(result, ensure_ascii=False)) <= 12000
