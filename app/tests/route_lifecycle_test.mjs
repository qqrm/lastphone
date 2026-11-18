// Dependency-free scheduling regression for the actual web/app.js functions.
// Android instrumentation separately exercises the full DOM and WebView bridge.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(process.env.LASTPHONE_WEB_SOURCE || new URL('../../web/app.js', import.meta.url), 'utf8');
const body = (name, end) => {
  const start = source.indexOf(`async function ${name}(`);
  assert.ok(start >= 0, name);
  const finish = source.indexOf(end, start);
  assert.ok(finish > start, name);
  return source.slice(start, finish);
};
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no });
  return { promise, resolve, reject };
};
const noop = () => {};
const classes = { remove: noop, contains: () => false };

function routeWorld(topics, videosHome) {
  const main = { innerHTML: '', classList: classes };
  const context = vm.createContext({
    main, topics, videosHome, selected: 'videos', routeGeneration: 0,
    document: { activeElement: null, body: { className: '' } },
    parse: () => ({ parts: [context.selected], q: new URLSearchParams() }),
    here: () => '#/' + context.selected,
    $: s => s === '#wframe' || s === 'main h1' ? null : { replaceChildren: noop }, $$: () => [],
    current: null, scrolls: {}, scrollY: 0, history: { state: { lb: 0 } },
    navAt: 0, navEnd: 0, navStack: [], keepNav: noop, sessionStorage: { setItem: noop },
    NAV_OF: {}, savedCount: noop, railTitles: noop, html: { classList: classes },
    h1seen: { disconnect: noop, observe: noop }, searchLive: null, leaving: null,
    window: {}, MapView: { stop: noop },
    reader: async () => { main.innerHTML = 'current reader' },
    setTitle: noop, emptyState: (...args) => 'ERROR ' + args.join(' '),
    wireRails: noop, dock: noop, barState: noop, bandState: noop, alignTitle: noop,
    requestAnimationFrame: noop, startHere: noop,
  });
  // The real function includes router bookkeeping, catch and completion work.
  vm.runInContext(body('route', '\n// Back, Forward'), context);
  return context;
}

test('late startup topics cannot enter an obsolete Videos route', async () => {
  const pending = deferred();
  let requests = 0, videoCalls = 0;
  const ctx = routeWorld(() => ++requests === 1 ? pending.promise : Promise.resolve([]), async () => { videoCalls++ });
  const old = ctx.route();
  ctx.selected = 'read';
  await ctx.route();
  pending.resolve([]);
  await old;
  assert.equal(videoCalls, 0);
  assert.equal(ctx.main.innerHTML, 'current reader');
});

test('an old route error cannot replace the reader or its body class', async () => {
  const pending = deferred(), entered = deferred();
  const ctx = routeWorld(async () => [], () => { entered.resolve(); return pending.promise });
  const old = ctx.route();
  await entered.promise;
  ctx.selected = 'read';
  await ctx.route();
  pending.reject(new TypeError("Cannot set properties of null (setting 'innerHTML')"));
  await old;
  assert.equal(ctx.main.innerHTML, 'current reader');
  assert.equal(ctx.document.body.className, 'v-read');
});

test('late Videos data only belongs to its original container', async () => {
  const pending = deferred();
  const rows = { isConnected: true, innerHTML: 'loading' };
  const main = { innerHTML: '' };
  const ctx = vm.createContext({
    main, setTitle: noop, searchBox: () => '', ico: () => '',
    $: s => s === '#vrows' ? rows.isConnected ? rows : null : { addEventListener: noop },
    api: () => pending.promise, resumable: async () => [], wireRails: noop,
  });
  vm.runInContext(body('videosHome', '\nasync function videoRow'), ctx);
  const work = ctx.videosHome();
  rows.isConnected = false;
  main.innerHTML = 'current reader';
  pending.resolve([]);
  await work;
  assert.equal(main.innerHTML, 'current reader');
  assert.equal(rows.innerHTML, 'loading');
});

test('a document metadata response cannot reopen a reader after navigation', async () => {
  const pending = deferred();
  const ctx = vm.createContext({ routeGeneration: 1, api: () => pending.promise });
  vm.runInContext(body('reader', '\n/*'), ctx);
  const work = ctx.reader('old', 1, '', 'web');
  ctx.routeGeneration++;
  pending.resolve({ kind: 'html', path: 'pages/old.html' });
  // Any obsolete render would touch undefined UI helpers and fail this test.
  await work;
});

test('late Android frame authorization cannot attach handlers to the next page', async () => {
  const pending = deferred(), entered = deferred();
  const documentBody = { isConnected: true, innerHTML: '' };
  const main = { innerHTML: '', onkeydown: null };
  const ctx = vm.createContext({
    main, routeGeneration: 1,
    api: async () => ({ kind: 'html', title: 'Old article', path: 'pages/old.html' }),
    $: s => s === '#docbody' ? documentBody : null, $$: () => [],
    setTitle: noop, remember: noop, esc: value => String(value ?? ''),
    backButton: () => '', askLink: () => '', saveButton: () => '', ico: () => '',
    fileUrl: path => '/' + path, IN_WINDOW: true, innerWidth: 400,
    authorizeFrame: () => { entered.resolve(); return pending.promise },
  });
  vm.runInContext(body('reader', '\n/*'), ctx);
  const work = ctx.reader('old', 1, '', 'web');
  await entered.promise;
  documentBody.isConnected = false;
  const nextHandler = () => 'next page';
  main.onkeydown = nextHandler;
  pending.resolve();
  await work;
  assert.equal(documentBody.innerHTML, '');
  assert.equal(main.onkeydown, nextHandler);
});
