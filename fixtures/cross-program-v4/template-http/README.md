# Template HTTP
Run `node server.js`. The service loads its HTML template before listening on loopback port 8080.
GET / serves HTML, /healthz returns status/app/theme JSON, and unknown paths return 404 JSON.
OPTIONAL_CONFIG=1 enables optional local theme settings; a missing file uses plain theme.
