"""Exercise window controls through the existing trusted WebView2 bridge."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

try:
    import fever_uimgr
except ImportError:
    fever_uimgr = None

from fever_webview2_bridge import ORIGIN, RequestRejected, authorize

PATH = '/_idv-login/webview2/window-controls'


def request(**changes):
    result = {'url': ORIGIN + PATH, 'page_id': 1, 'method': 'POST',
              'resource_context': 8, 'is_document_entry': False,
              'header_items': [('Origin', ORIGIN), ('X-IDV-UI', 'fever-webview2'),
                               ('Content-Type', 'application/json')],
              'body': b'{"action":"close"}'}
    result.update(changes)
    return result


class ControlAuthorizationTests(unittest.TestCase):
    def test_only_trusted_top_level_context_is_allowed(self):
        self.assertEqual(authorize(request(), ORIGIN + '/index.html')[1], PATH)
        for changes, document in (
            ({'page_id': 2}, ORIGIN + '/index.html'),
            ({'url': 'https://attacker.invalid' + PATH}, ORIGIN + '/index.html'),
            ({}, 'https://attacker.invalid/index.html'),
            ({'header_items': [('Origin', 'https://attacker.invalid'),
                               ('X-IDV-UI', 'fever-webview2')]}, ORIGIN + '/index.html'),
            ({'header_items': [('Origin', ORIGIN)]}, ORIGIN + '/index.html'),
        ):
            with self.subTest(changes=changes, document=document), self.assertRaises(RequestRejected):
                authorize(request(**changes), document)


@unittest.skipIf(fever_uimgr is None, 'PyQt6 is unavailable')
class ControlActionTests(unittest.TestCase):
    def test_only_three_actions_and_response_precedes_action(self):
        class Window:
            def __init__(self): self.events = []
            def _finish_request(self, rid, result): self.events.append(('response', rid, result))
            def showMinimized(self): self.events.append('minimize')
            def toggle_maximized(self): self.events.append('maximize')
            def close(self): self.events.append('close')

        for action in ('minimize', 'maximize', 'close'):
            window, pending = Window(), []
            with self.subTest(action=action), patch.object(fever_uimgr.QTimer, 'singleShot',
                    side_effect=lambda delay, callback: pending.append(callback)):
                fever_uimgr.FeverWindow._native_action(window, 17, PATH, {'action': action}, 'POST')
                self.assertEqual(len(pending), 1)
                self.assertEqual(window.events[0][0:2], ('response', 17))
                self.assertEqual(json.loads(window.events[0][2][2]), {'success': True})
                self.assertEqual(len(window.events), 1)
                pending[0]()
                self.assertEqual(window.events[-1], action)

        for value in ('restart', '', None, [], {}):
            with self.subTest(value=value), self.assertRaises(RequestRejected) as error:
                fever_uimgr.FeverWindow._native_action(Window(), 17, PATH, {'action': value}, 'POST')
            self.assertEqual(error.exception.status, 400)
        with self.assertRaises(RequestRejected) as error:
            fever_uimgr.FeverWindow._native_action(Window(), 17, PATH, {}, 'GET')
        self.assertEqual(error.exception.status, 405)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_javascript_rejects_untrusted_context_and_dispatches_buttons(self):
        # Run the actual injected script with a tiny DOM and transport, not a
        # copy of the handler. This catches syntax, origin and dispatch errors.
        harness = r'''
const vm = require('vm');
const source = require('fs').readFileSync(0, 'utf8');
function run(origin, path, inFrame) {
  const requests = [], callbacks = {}, buttons = ['minimize','maximize','close'].map(action => ({
    dataset: {action}, addEventListener(name, handler) { this[name] = handler; }
  }));
  const document = {body:{children:[], appendChild(x) {this.children.push(x);}},
    addEventListener(n, f) {callbacks[n] = f;}, createElement() {
      return {style:{}, attachShadow() {return {set innerHTML(v) {}, querySelectorAll() {return buttons;}};}};
    }};
  const win = {}; win.top = inFrame ? {} : win;
  const context = {window:win, document, location:{origin, pathname:path},
    fetch:(url, init) => {requests.push({url, init}); return Promise.resolve({});},
    JSON};
  vm.runInNewContext(source, context);
  if (callbacks.DOMContentLoaded) callbacks.DOMContentLoaded();
  return {document, requests, buttons};
}
for (const x of [run('https://evil.invalid','/index.html',false),
  run('https://idv-ui.invalid','/other',false), run('https://idv-ui.invalid','/index.html',true)]) {
  if (x.document.body.children.length || x.requests.length) throw Error('untrusted frame installed controls');
}
const x = run('https://idv-ui.invalid','/index.html',false);
if (x.document.body.children.length !== 1) throw Error('controls not installed');
for (const button of x.buttons) { let stopped=false; button.click({stopPropagation(){stopped=true;}}); if (!stopped) throw Error('click bubbled'); }
if (x.requests.length !== 3) throw Error('wrong request count');
for (let i=0;i<3;i++) {
 const req=x.requests[i];
 if (req.url !== '/_idv-login/webview2/window-controls' || req.init.method !== 'POST'
     || JSON.parse(req.init.body).action !== x.buttons[i].dataset.action) throw Error('incorrect action');
}
'''
        subprocess.run([shutil.which('node'), '-e', harness], input=fever_uimgr.WINDOW_CONTROLS_SCRIPT,
                       text=True, check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
