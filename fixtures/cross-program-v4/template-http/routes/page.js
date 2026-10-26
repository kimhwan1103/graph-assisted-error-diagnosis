export function route(template, theme, req, res) {
  if (req.url === '/') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'X-Theme': theme });
    return res.end(template);
  }
  res.setHeader('Content-Type', 'application/json');
  if (req.url === '/healthz') {
    res.writeHead(200);
    return res.end(JSON.stringify({ status: 'ok', app: 'template-http', theme }));
  }
  res.writeHead(404);
  res.end(JSON.stringify({ error: 'not_found' }));
}
