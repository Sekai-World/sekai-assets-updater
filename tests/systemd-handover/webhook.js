const http = require("node:http");

const server = http.createServer((request, response) => {
  if (request.url !== "/health") {
    response.writeHead(404).end("not found\n");
    return;
  }

  response.writeHead(200, { "content-type": "application/json" });
  response.end(JSON.stringify({ status: "ok", pid: process.pid }));
});

server.listen(18765, "127.0.0.1");
process.on("SIGTERM", () => server.close(() => process.exit(0)));
