import pytest

from harvest_ai import extract, robots

ROBOTS = """
# comment
User-agent: Googlebot
Disallow: /

User-agent: harvest-bot
User-agent: otherbot
Disallow: /private
Allow: /private/public$
Disallow: /*?sort=
Disallow: /*.pdf$
Crawl-delay: 3

User-agent: *
Disallow: /admin
Sitemap: https://x.test/sitemap.xml
"""


def test_robots_groups_wildcards_and_delay():
    r = robots.parse(ROBOTS)
    ua = "harvest-bot/0.1 (+x)"
    assert r.can_fetch(ua, "https://x.test/cars?page=2")
    assert not r.can_fetch(ua, "https://x.test/private/a")
    assert r.can_fetch(ua, "https://x.test/private/public")
    assert not r.can_fetch(ua, "https://x.test/private/public/more")
    assert not r.can_fetch(ua, "https://x.test/list?sort=price")
    assert not r.can_fetch(ua, "https://x.test/docs/a.pdf") and r.can_fetch(ua, "https://x.test/docs/a.pdf?x=1")
    assert r.can_fetch(ua, "https://x.test/admin")  # its own group applies, not '*'
    assert not r.can_fetch("SomeBot/1", "https://x.test/admin")
    assert not r.can_fetch("Googlebot", "https://x.test/")
    assert r.crawl_delay(ua) == 3 and r.sitemaps == ["https://x.test/sitemap.xml"]
    assert r.can_fetch(ua, "https://x.test/robots.txt")
    s = r.summary(ua, ["https://x.test/private/x"])
    assert s["checked"] == {"https://x.test/private/x": False} and s["crawl_delay"] == 3


def test_robots_repeated_groups_merge_and_exact_token():
    # trials 2026-10: a second `User-agent: *` group further down the file was ignored, so its disallows were not honoured
    r = robots.parse("User-agent: *\nDisallow: /admin\nCrawl-delay: 2\n\nUser-agent: Googlebot\nAllow: /\n\n"
                     "User-agent: *\nDisallow: /jobs/search\nCrawl-delay: 5\n")
    ua = "harvest-bot/0.1 (+x)"
    assert not r.can_fetch(ua, "https://x.test/jobs/search?q=python")
    assert not r.can_fetch(ua, "https://x.test/admin")
    assert r.can_fetch(ua, "https://x.test/jobs/123") and r.crawl_delay(ua) == 5
    # two groups for our own token merge too
    r = robots.parse("User-agent: harvest-bot\nDisallow: /a\n\nUser-agent: *\nDisallow: /\n\nUser-agent: harvest-bot\nDisallow: /b\n")
    assert not r.can_fetch(ua, "https://x.test/a/1") and not r.can_fetch(ua, "https://x.test/b/1") and r.can_fetch(ua, "https://x.test/c")
    # a group for another crawler whose name contains ours (or is contained in ours) is not ours: `*` applies
    r = robots.parse("User-agent: harvest\nAllow: /\n\nUser-agent: bot\nAllow: /\n\nUser-agent: *\nDisallow: /\n")
    assert not r.can_fetch(ua, "https://x.test/a")
    assert robots.parse("User-agent: Harvest-Bot/2.0\nDisallow: /x\n").can_fetch(ua, "https://x.test/y")
    assert not robots.parse("User-agent: Harvest-Bot/2.0\nDisallow: /x\n").can_fetch(ua, "https://x.test/x")


def test_robots_fetch_status_semantics():
    assert robots.from_status(404).can_fetch("a", "https://x/any")
    assert not robots.from_status(503).can_fetch("a", "https://x/any")
    assert not robots.from_status(None).can_fetch("a", "https://x/any")
    assert robots.from_status(200, "User-agent: *\nDisallow:\n").can_fetch("a", "https://x/any")


HTML = """<html lang="pt"><head><title>Casas &amp; Apartamentos</title><meta property="og:title" content="OG t">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Product","name":"Flat","offers":{"price":"950","priceCurrency":"EUR"}}]}</script>
<script type="application/ld+json">{"@type":"ItemList","itemListElement":[{"item":{"@type":"Car","name":"Golf"}}]}</script>
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"items":[{"id":1,"price":5},{"id":2,"price":6}]}}}</script>
<script>window.__INITIAL_STATE__ = {"list": [{"id": "a", "t": "x}y"}]};</script>
</head><body><div class="card big" data-id="7"><a href="/item/7">Seven <b>bold</b></a><img src="/i.jpg"><br></div>
<div class="card"><a href="https://other.test/8">Eight</a></div><p>Hello<script>var x=1</script> world</p></body></html>"""


def test_html_dom_and_embedded_json():
    doc = extract.parse_html(HTML)
    cards = doc.find_all("div", cls="card")
    assert len(cards) == 2 and cards[0].get("data-id") == "7" and cards[0].find("a").text == "Seven bold"
    assert doc.find("div", cls="card big") is cards[0] and doc.find("img").get("src") == "/i.jpg"
    assert doc.find("p").text == "Hello world"
    assert [x["@type"] for x in extract.jsonld(HTML)] == ["Product", "ItemList"]
    assert set(extract.jsonld_types(HTML)) == {"Product", "ItemList", "Car"}
    nd = extract.next_data(HTML)
    assert [i["id"] for i in extract.find_items(nd, ("id", "price"))] == [1, 2]
    assert extract.script_json(HTML, "__INITIAL_STATE__") == {"list": [{"id": "a", "t": "x}y"}]}
    assert extract.links(HTML, "https://site.test/") == ["https://site.test/item/7", "https://other.test/8"]
    m = extract.meta(HTML)
    assert m["og:title"] == "OG t" and m["title"] == "Casas & Apartamentos" and m["lang"] == "pt"


def test_feeds_and_sitemaps():
    rss = "<rss><channel><item><title>A</title><link>https://x/a</link><guid>1</guid><pubDate>Fri, 25 Sep 2026 00:00:00 +0000</pubDate></item></channel></rss>"
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title><link href="https://x/b"/><id>2</id></entry></feed>'
    jf = '{"version":"https://jsonfeed.org/version/1.1","items":[{"id":"3","url":"https://x/c"}]}'
    assert extract.feed_items(rss)[0]["link"] == "https://x/a" and extract.feed_items(atom)[0]["link"] == "https://x/b"
    assert extract.feed_items(jf)[0]["id"] == "3" and extract.feed_items("") == [] and extract.feed_items("  ") == []
    for bad in ("garbage", rss[: len(rss) // 2], '{"items": [{"id": 1'):  # not a feed, a truncated feed, a truncated JSON Feed
        with pytest.raises(extract.FeedError):
            extract.feed_items(bad)
    sm = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://x/1</loc></url><url><loc>https://x/2</loc></url></urlset>'
    idx = '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://x/s1.xml</loc></sitemap></sitemapindex>'
    assert extract.sitemap_urls(sm) == (["https://x/1", "https://x/2"], []) and extract.sitemap_urls(idx) == ([], ["https://x/s1.xml"])
