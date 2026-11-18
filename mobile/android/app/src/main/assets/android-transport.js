/* Native transport for POST bodies. GET/Range is streamed by shouldInterceptRequest.
   This script is installed at document start, before the upstream keyed fetch wrapper. */
(() => {
  'use strict';
  if (window.top !== window || location.origin !== 'https://lastphone.invalid') return;
  const channel = window.LastphoneNative;
  if (!channel) throw new Error('Lastphone requires an up-to-date Android System WebView.');
  const originalFetch = window.fetch.bind(window), pending = new Map();
  let serial = 0;
  const encode = bytes => {
    let text = '';
    for (let at = 0; at < bytes.length; at += 32768) text += String.fromCharCode(...bytes.subarray(at, at + 32768));
    return btoa(text);
  };
  const decode = text => Uint8Array.from(atob(text || ''), c => c.charCodeAt(0));
  channel.onmessage = event => {
    let result;
    try { result = JSON.parse(event.data) } catch { return }
    const slot = pending.get(result.id);
    if (!slot) return;
    pending.delete(result.id);
    clearTimeout(slot.timer);
    if (result.error) slot.reject(new Error(result.error));
    else slot.resolve(result);
  };
  const send = data => new Promise((resolve, reject) => {
    const id = String(++serial);
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('The local request timed out.')) }, 180000);
    pending.set(id, { resolve, reject, timer });
    try { channel.postMessage(JSON.stringify({ ...data, id })) }
    catch (error) { clearTimeout(timer); pending.delete(id); reject(error) }
  });
  window.LastphoneShell = Object.freeze({ authorizeFrame: path => send({ action: 'ticket', url: new URL(path, location.href).href }) });
  window.fetch = async (input, init) => {
    const request = new Request(input, init), url = new URL(request.url);
    if (url.origin !== location.origin) throw new TypeError('Lastphone blocks web requests from library pages.');
    if (request.method !== 'POST') return originalFetch(request);
    if (request.signal.aborted) throw new DOMException('Aborted', 'AbortError');
    const body = new Uint8Array(await request.arrayBuffer());
    if (body.length > 256 * 1024) throw new RangeError('Request is too large.');
    const result = await send({ action: 'request', method: 'POST', url: url.href,
      headers: Object.fromEntries(request.headers), body: encode(body) });
    return new Response([204, 205, 304].includes(result.status) ? null : decode(result.body),
      { status: result.status, statusText: result.reason, headers: result.headers });
  };
  // A clicked download stays on-device. Blob exports never reach a URL or server.
  document.addEventListener('click', event => {
    const link = event.target.closest && event.target.closest('a');
    if (link && link.target === '_blank' && new URL(link.href).origin === location.origin) {
      event.preventDefault(); send({ action: 'navigate', url: link.href }).catch(() => {}); return;
    }
    const anchor = link && link.hasAttribute('download') ? link : null;
    if (!anchor || !anchor.href.startsWith('blob:')) return;
    event.preventDefault();
    originalFetch(anchor.href).then(r => r.arrayBuffer()).then(buffer => {
      if (buffer.byteLength > 8 * 1024 * 1024) throw new Error('Export is too large.');
      return send({ action: 'save', name: anchor.download || 'lastphone-export.md', body: encode(new Uint8Array(buffer)) });
    }).catch(error => alert(error.message));
  }, true);
  // Programmatic anchor.click() is also caught above, including the original chat exporter.
  document.addEventListener('DOMContentLoaded', () => {
    document.title = document.title.replaceAll('Lastbook', 'Lastphone');
    document.querySelectorAll('.side-brand span,.brand span,.capband span').forEach(el => { if (el.textContent === 'Lastbook') el.textContent = 'Lastphone' });
  });
})();
