import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

from smart_store_aios.dashboard import INDEX_HTML


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


@unittest.skipUnless(shutil.which("node"), "Node required for embedded dashboard JavaScript regression")
class OpsDashboardRenderingTest(unittest.TestCase):
    def test_hostile_status_fields_render_as_text_not_html(self):
        source, = re.findall(r"<script>(.*?)</script>", INDEX_HTML, re.S)
        hostile = '</li><img src=x onerror="globalThis.compromised=1"><svg onload="globalThis.compromised=1">&"\''
        data = {
            "phase": {"name": hostile, "evidence": [hostile]},
            "agents": [{"agent_id": hostile, "state": hostile, "current_task": hostile}],
            "blockers": [hostile], "next_work": hostile,
            "recent_commits": [{"id": hostile, "subject": hostile}],
        }
        runner = r'''
const fs = require('fs'), vm = require('vm');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = new Map();
const sandbox = {
 document: {hidden: false, addEventListener() {}, getElementById(id) {
   if (!elements.has(id)) elements.set(id, {value: '', textContent: '', innerHTML: '', className: ''});
   return elements.get(id);
 }}, localStorage: {getItem() {return null;}, setItem() {}}, setInterval() {},
 fetch() {throw new Error('network prohibited');}
};
vm.createContext(sandbox);
vm.runInContext(payload.source, sandbox, {timeout: 1000});
sandbox.render(payload.data);
process.stdout.write(JSON.stringify({html: elements.get('app').innerHTML}));
'''
        result = subprocess.run([shutil.which("node"), "-e", runner],
                                input=json.dumps({"source": source, "data": data}),
                                text=True, encoding="utf-8", capture_output=True, check=True, timeout=10)
        markup = Markup()
        markup.feed(json.loads(result.stdout)["html"])
        self.assertFalse(any(tag in {"img", "svg", "script"} for tag, _ in markup.tags))
        self.assertFalse(any(name.startswith("on") for _, attrs in markup.tags for name in attrs))
        self.assertIn(hostile, "".join(markup.text))


if __name__ == "__main__":
    unittest.main()
