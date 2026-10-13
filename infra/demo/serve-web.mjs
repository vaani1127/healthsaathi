// Native demo web server (make demo-native): what Caddy does in the Docker demo, with Node only.
// Serves the production build of the PWA, proxies /api (including the WebSocket) to the API,
// serves /config.js from the demo state folder, and sets the same security headers.
//
//   node infra/demo/serve-web.mjs <dist dir> <state dir> <port> <api port>
import { createReadStream, existsSync, statSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import { extname, join, normalize, resolve } from "node:path";

const [dist, state, port = "8080", apiPort = "8081"] = process.argv.slice(2);
const ROOT = resolve(dist);
const CONFIG = join(resolve(state), "web", "config.js");

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".webmanifest": "application/manifest+json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
};

const HEADERS = {
  "Content-Security-Policy":
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; font-src 'self'; " +
    `connect-src 'self' http://localhost:8545 ws://localhost:${port}; worker-src 'self'; manifest-src 'self'; ` +
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
};

function sendFile(res, path, extra = {}) {
  res.writeHead(200, { ...HEADERS, "Content-Type": TYPES[extname(path)] ?? "application/octet-stream", ...extra });
  createReadStream(path).pipe(res);
}

function proxy(req, res) {
  const upstream = http.request(
    { host: "127.0.0.1", port: Number(apiPort), path: req.url, method: req.method, headers: req.headers },
    (up) => {
      res.writeHead(up.statusCode ?? 502, up.headers);
      up.pipe(res);
    },
  );
  upstream.on("error", () => {
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "application/problem+json" });
    }
    res.end(JSON.stringify({ status: 502, code: "internal-error" }));
  });
  req.pipe(upstream);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://localhost");
  if (url.pathname.startsWith("/api/")) {
    proxy(req, res);
    return;
  }
  if (url.pathname === "/config.js") {
    if (existsSync(CONFIG)) {
      sendFile(res, CONFIG, { "Cache-Control": "no-cache" });
    } else {
      res.writeHead(404, HEADERS).end();
    }
    return;
  }
  const path = normalize(join(ROOT, decodeURIComponent(url.pathname)));
  if (!path.startsWith(ROOT)) {
    res.writeHead(400, HEADERS).end();
    return;
  }
  if (existsSync(path) && statSync(path).isFile()) {
    const cache = url.pathname.startsWith("/assets/") ? "public, max-age=31536000, immutable" : "no-cache";
    sendFile(res, path, { "Cache-Control": cache });
    return;
  }
  // Single page app: every other path gets the shell.
  sendFile(res, join(ROOT, "index.html"), { "Cache-Control": "no-cache" });
});

// WebSocket upgrade for /api/v1/ws: a raw TCP pipe to the API.
server.on("upgrade", (req, socket, head) => {
  if (!(req.url ?? "").startsWith("/api/")) {
    socket.destroy();
    return;
  }
  const upstream = net.connect(Number(apiPort), "127.0.0.1", () => {
    const lines = [`${req.method} ${req.url} HTTP/${req.httpVersion}`];
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      lines.push(`${req.rawHeaders[i]}: ${req.rawHeaders[i + 1]}`);
    }
    upstream.write(lines.join("\r\n") + "\r\n\r\n");
    upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  });
  upstream.on("error", () => socket.destroy());
  socket.on("error", () => upstream.destroy());
});

server.listen(Number(port), "127.0.0.1", () => {
  console.log(`web on http://localhost:${port} (api on ${apiPort})`);
});
