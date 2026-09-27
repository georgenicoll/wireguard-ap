#!/usr/bin/env python3
"""Serves the AP's TLS certificate (DER form) over plain HTTP, at exactly
one path - nothing else here needs a general static-file server. Plain
HTTP, not the web UI's HTTPS, deliberately: application/x-x509-ca-cert is
the MIME type that makes Chrome hand a downloaded file straight to
Android's own certificate installer, and plain HTTP sidesteps the very
certificate-trust problem installing it is meant to solve.
"""
import http.server
import sys

PATH = "/wireguard-ap.crt"
CERT_FILE, BIND, PORT = sys.argv[1], sys.argv[2], int(sys.argv[3])
CERT_BYTES = open(CERT_FILE, "rb").read()


class Handler(http.server.BaseHTTPRequestHandler):
    def _headers(self):
        if self.path != PATH:
            self.send_error(404)
            return False
        self.send_response(200)
        self.send_header("Content-Type", "application/x-x509-ca-cert")
        self.send_header("Content-Length", str(len(CERT_BYTES)))
        self.end_headers()
        return True

    def do_GET(self):
        if self._headers():
            self.wfile.write(CERT_BYTES)

    # Download managers (Windows' in particular) probe with HEAD before
    # committing to the actual download - the default 501 for an
    # unimplemented method was enough to make them save an empty file.
    def do_HEAD(self):
        self._headers()


http.server.HTTPServer((BIND, PORT), Handler).serve_forever()
