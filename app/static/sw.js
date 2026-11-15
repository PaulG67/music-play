/* Service worker: app shell, offline audio and artwork.

   The Cache API cannot answer byte-range requests, but <audio> always asks for
   ranges - so downloaded tracks are stored as one complete response and sliced
   here into 206 replies. */

const AUDIO_CACHE = "music-play-audio-v1";
const COVER_CACHE = "music-play-covers-v1";
const KEEP = [AUDIO_CACHE, COVER_CACHE];

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => !KEEP.includes(key)).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

async function sliceRange(cached, rangeHeader) {
  const buffer = await cached.arrayBuffer();
  const size = buffer.byteLength;
  const match = /bytes=(\d*)-(\d*)/.exec(rangeHeader || "");

  let start = 0;
  let end = size - 1;

  if (match) {
    if (match[1] === "" && match[2] !== "") {
      start = Math.max(size - parseInt(match[2], 10), 0);
    } else {
      if (match[1] !== "") start = parseInt(match[1], 10);
      if (match[2] !== "") end = parseInt(match[2], 10);
    }
  }
  end = Math.min(end, size - 1);

  if (start > end || start >= size) {
    return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
  }

  const slice = buffer.slice(start, end + 1);
  return new Response(slice, {
    status: 206,
    headers: {
      "Content-Type": cached.headers.get("content-type") || "audio/mp4",
      "Content-Range": `bytes ${start}-${end}/${size}`,
      "Content-Length": String(slice.byteLength),
      "Accept-Ranges": "bytes",
    },
  });
}

async function handleAudio(request) {
  const cache = await caches.open(AUDIO_CACHE);
  const cached = await cache.match(request.url);

  if (cached) {
    const range = request.headers.get("range");
    return range ? sliceRange(cached.clone(), range) : cached;
  }

  return fetch(request);
}

async function handleCover(request) {
  const cache = await caches.open(COVER_CACHE);
  const cached = await cache.match(request.url);
  if (cached) return cached;

  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request.url, response.clone());
    return response;
  } catch (error) {
    return new Response(null, { status: 504 });
  }
}

const LOGIN_FALLBACK = `<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <title>Music Play</title>
  <style>
    body { margin:0; min-height:100vh; display:grid; place-items:center; background:#000; color:#fff; font-family:-apple-system,sans-serif; }
    form { width:min(360px,90vw); text-align:center; }
    p { color:#b3b3b3; }
    input, button { width:100%; padding:13px; margin:8px 0; border:0; border-radius:12px; font-size:16px; box-sizing:border-box; }
    input { background:#282828; color:#fff; }
    button { background:#1db954; color:#000; font-weight:700; }
  </style>
</head>
<body>
  <form method="post" action="/auth/login" autocomplete="off">
    <h1>Music Play</h1>
    <p>Die gespeicherte App-Seite war blockiert. Bitte neu anmelden.</p>
    <input name="mp_username" autocomplete="username" autocapitalize="none" placeholder="Benutzername">
    <input name="mp_password" type="password" autocomplete="current-password" placeholder="Passwort">
    <button type="submit">Anmelden</button>
  </form>
</body>
</html>`;

async function handleDocument(request) {
  try {
    const response = await fetch(request, { cache: "no-store", credentials: "include" });
    if (response.ok) return response;
    if (response.status === 403 || response.status === 401) {
      const alt = await fetch("/app?sw-bypass=1", { cache: "no-store", credentials: "include" });
      if (alt.ok) return alt;
      return new Response(LOGIN_FALLBACK, {
        status: 200,
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
      });
    }
    return response;
  } catch (error) {
    return new Response(LOGIN_FALLBACK, {
      status: 200,
      headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
    });
  }
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.searchParams.has("sw-bypass")) return;

  if (request.destination === "document" || url.pathname === "/" || url.pathname === "/app") {
    event.respondWith(handleDocument(request));
    return;
  }

  if (url.pathname.startsWith("/api/stream/")) {
    event.respondWith(handleAudio(request));
    return;
  }
  if (url.pathname === "/api/cover") {
    event.respondWith(handleCover(request));
    return;
  }
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/rest/")) return;

  event.respondWith(
    fetch(request).catch(() => caches.match(request).then((cached) => cached || fetch(request)))
  );
});
