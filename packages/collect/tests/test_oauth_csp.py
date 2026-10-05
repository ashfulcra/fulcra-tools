from html.parser import HTMLParser
from fulcra_collect.routes.oauth import _oauth_success_html

class Page(HTMLParser):
    def __init__(self, body):
        super().__init__(); self.scripts = []; self.feed(body)
    def handle_starttag(self, tag, attrs):
        if tag == 'script': self.scripts.append(dict(attrs))

def test_callback_uses_packaged_script_and_escaped_data():
    plugin = '\"<>&\'\\\n</script>'
    body = _oauth_success_html(plugin)
    page = Page(body)
    assert page.scripts == [{'src': '/static/oauth-complete.js', 'defer': None}]
    assert '<body data-plugin-id=' in body
    assert '</script>' not in body.split('<body', 1)[1].split('<script', 1)[0]
