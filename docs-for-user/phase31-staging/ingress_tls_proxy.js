/**
 * Staging TLS ingress.
 *
 * The deployment contract puts TLS in front of the application: the platform
 * ingress terminates TLS for the public origin, and this process is the local
 * equivalent used by the validation harness, so HTTPS behaviour (secure
 * cookies, HTTPS detection, forwarded headers, WebSocket upgrade) can be
 * exercised against the real staging frontend rather than a dev server.
 *
 * It is a reverse proxy, not part of the application: no application logic, no
 * bypass, no rewriting beyond the hop headers a real ingress would set.
 *
 * Usage: node ingress_tls_proxy.js <certDir> <listenPort> <targetPort>
 */
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const net = require("node:net");
const path = require("node:path");

const [, , certDir, listenArg, targetArg] = process.argv;
const LISTEN = Number(listenArg || 8443);
const TARGET = Number(targetArg || 3982);
const CERT = path.join(certDir || __dirname, "ingress.crt");
const KEY = path.join(certDir || __dirname, "ingress.key");
const FORWARDED_PROTO = "https";

/**
 * Operational log: one line per lifecycle, proxy and upgrade event, prefixed
 * with an ISO-8601 timestamp and a severity so an incident can be correlated
 * with the application's own logs. No request bodies, headers or credentials
 * are ever logged.
 */
function log(level, message) {
  process.stdout.write(`${new Date().toISOString()} ${level} ingress - ${message}\n`);
}

/**
 * Hop-by-hop headers belong to one connection, never to the proxied message.
 * Forwarding them (notably a backend's `connection: close`) desynchronises the
 * client connection from the upstream one: the browser keeps a socket open that
 * the proxy already told it to close, and the next request on it is rejected
 * before it reaches the application. A terminating proxy owns both connections
 * separately, so both directions have them stripped.
 */
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-connection",
  "transfer-encoding",
  "upgrade",
  "te",
  "trailer",
]);

function forwardRequestHeaders(incoming) {
  const headers = {};
  for (const [name, value] of Object.entries(incoming)) {
    if (!HOP_BY_HOP.has(name.toLowerCase())) headers[name] = value;
  }
  headers["x-forwarded-proto"] = FORWARDED_PROTO;
  headers["x-forwarded-for"] = headers["x-forwarded-for"] || incoming["x-forwarded-for"] || "";
  headers["x-forwarded-host"] = headers["x-forwarded-host"] || incoming.host || "";
  headers["x-real-ip"] = headers["x-real-ip"] || "";
  return headers;
}

function forwardResponseHeaders(upstream) {
  const headers = {};
  for (const [name, value] of Object.entries(upstream)) {
    if (!HOP_BY_HOP.has(name.toLowerCase())) headers[name] = value;
  }
  return headers;
}

const server = https.createServer(
  { cert: fs.readFileSync(CERT), key: fs.readFileSync(KEY) },
  (req, res) => {
    const headers = forwardRequestHeaders(req.headers);
    headers["x-forwarded-for"] = headers["x-forwarded-for"] || req.socket.remoteAddress || "";
    headers["x-real-ip"] = headers["x-real-ip"] || req.socket.remoteAddress || "";
    const proxied = http.request(
      // A fresh upstream connection per request keeps the two connections
      // independent, which is what a terminating proxy is.
      { host: "127.0.0.1", port: TARGET, method: req.method, path: req.url, headers, agent: false },
      (upstream) => {
        const status = upstream.statusCode || 502;
        if (status >= 400) {
          log(status >= 500 ? "ERROR" : "WARNING",
            `${req.method} ${req.url} -> ${status} (upstream)`);
        }
        res.writeHead(status, forwardResponseHeaders(upstream.headers));
        upstream.pipe(res);
      },
    );
    proxied.on("error", (error) => {
      log("ERROR", `${req.method} ${req.url} -> 502 upstream=${error.code || "BadGateway"}`);
      res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ detail: "The request could not be completed.", type: error.code || "BadGateway" }));
    });
    req.pipe(proxied);
  },
);

// WebSocket / upgrade passthrough: the app serves chat streams over /ws.
server.on("upgrade", (req, socket, head) => {
  const upstream = net.connect(TARGET, "127.0.0.1", () => {
    const headers = forwardRequestHeaders(req.headers);
    headers["x-forwarded-for"] = headers["x-forwarded-for"] || req.socket.remoteAddress || "";
    const lines = [`GET ${req.url} HTTP/1.1`, ...Object.entries(headers).map(([k, v]) => `${k}: ${v}`)];
    upstream.write(lines.join("\r\n") + "\r\n\r\n");
    if (head && head.length) upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  });
  upstream.on("error", (error) => {
    log("ERROR", `upgrade ${req.url} -> closed upstream=${error.code || "BadGateway"}`);
    socket.destroy();
  });
});

server.listen(LISTEN, "0.0.0.0", () => {
  log("INFO", `listening on https://0.0.0.0:${LISTEN} -> 127.0.0.1:${TARGET}`);
});
server.on("tlsClientError", (error) => log("WARNING", `tls handshake refused: ${error.code || error.message}`));
