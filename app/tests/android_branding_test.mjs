// Run the Android document-start script and the actual upstream title helper.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const transport = readFileSync(new URL('../../mobile/android/app/src/main/assets/android-transport.js', import.meta.url), 'utf8');
const titleHelper = readFileSync(new URL('../../web/app.js', import.meta.url), 'utf8').split('\n').find(line => line.startsWith('const setTitle = '));
assert.ok(titleHelper);

function world(readyState = 'complete', origin = 'https://lastphone.invalid', frame = false) {
  const listeners = new Map(), observers = [], shellLabels = Array.from({ length: 4 }, () => ({ textContent: 'Lastbook' }));
  const source = { textContent: 'Original Lastbook source credits' };
  const link = { label: 'Lastbook videos', getAttribute() { return this.label }, setAttribute(_, value) { this.label = value } };
  const titleNode = {}, subNode = {};
  const document = {
    readyState, head: {}, titleWrites: 0, _title: 'Lastbook',
    get title() { return this._title },
    set title(value) {
      this._title = value; this.titleWrites++;
      for (const callback of observers) queueMicrotask(callback);
    },
    addEventListener(name, handler, options = {}) {
      const list = listeners.get(name) || [];
      list.push({ handler, once: options.once }); listeners.set(name, list);
    },
    querySelectorAll(selector) {
      return selector.includes('[aria-label]') ? [link] : shellLabels;
    },
  };
  const fetch = async () => new Response('ok');
  const window = { fetch, LastphoneNative: { postMessage() {} } };
  window.top = frame ? {} : window;
  const context = vm.createContext({
    window, document, location: { origin, href: origin + '/' },
    Request, Response, URL, Uint8Array, DOMException, btoa, atob, setTimeout, clearTimeout,
    MutationObserver: class {
      constructor(callback) { this.callback = callback }
      observe(target, options) {
        assert.equal(target, document.head);
        assert.equal(options.characterData, true);
        observers.push(this.callback);
      }
    },
    $: selector => selector === '#tbTitle' ? titleNode : subNode,
  });
  const inject = () => vm.runInContext(transport, context);
  const fireReady = () => {
    document.readyState = 'complete';
    const callbacks = listeners.get('DOMContentLoaded') || [];
    listeners.set('DOMContentLoaded', callbacks.filter(item => !item.once));
    for (const { handler } of callbacks) handler();
  };
  vm.runInContext(titleHelper + '\nthis.setTitle = setTitle;', context);
  return { context, document, window, shellLabels, source, link, titleNode, subNode, observers, listeners, fetch, inject, fireReady };
}

test('already-loaded Android document receives shell branding immediately', async () => {
  const w = world();
  w.inject();
  assert.equal(w.document.title, 'Lastphone');
  assert.ok(w.shellLabels.every(el => el.textContent === 'Lastphone'));
  assert.equal(w.link.label, 'Lastphone videos');
  assert.equal(w.source.textContent, 'Original Lastbook source credits');
  assert.equal(w.listeners.has('DOMContentLoaded'), false);
  const writes = w.document.titleWrites;
  w.observers[0](); w.observers[0]();
  await Promise.resolve();
  assert.equal(w.document.titleWrites, writes, 'Branding must not cause a mutation loop');
});

test('document-start injection waits for parsed shell elements exactly once', () => {
  const w = world('loading');
  w.inject();
  assert.equal(w.observers.length, 0);
  assert.ok(w.shellLabels.every(el => el.textContent === 'Lastbook'));
  w.fireReady();
  assert.equal(w.document.title, 'Lastphone');
  assert.ok(w.shellLabels.every(el => el.textContent === 'Lastphone'));
  w.fireReady();
  assert.equal(w.observers.length, 1);
});

test('later route titles stay branded synchronously and preserve source names', async () => {
  const w = world();
  w.inject();
  w.context.setTitle('Lastbook field manual', 'Publisher', 'Reading');
  assert.equal(w.document.title, 'Lastbook field manual · Lastphone');
  assert.equal(w.titleNode.textContent, 'Reading');
  assert.equal(w.subNode.textContent, 'Publisher');
  w.context.setTitle('');
  assert.equal(w.document.title, 'Lastphone');
  // Chat assigns its title directly rather than calling app.js setTitle.
  w.document.title = 'My Lastbook notes · Lastbook';
  await Promise.resolve(); await Promise.resolve();
  assert.equal(w.document.title, 'My Lastbook notes · Lastphone');
  w.document.title = 'Set up Lastbook';
  await Promise.resolve(); await Promise.resolve();
  assert.equal(w.document.title, 'Set up Lastphone');
});

test('desktop pages and article frames retain upstream branding and transport', () => {
  for (const [origin, frame] of [['https://lastbook.invalid', false], ['https://lastphone.invalid', true]]) {
    const w = world('complete', origin, frame);
    w.inject();
    w.context.setTitle('Manual');
    assert.equal(w.document.title, 'Manual · Lastbook');
    assert.equal(w.window.LastphoneShell, undefined);
    assert.equal(w.window.fetch, w.fetch);
    assert.equal(w.observers.length, 0);
    assert.ok(w.shellLabels.every(el => el.textContent === 'Lastbook'));
  }
});
