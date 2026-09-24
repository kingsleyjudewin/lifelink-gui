#!/usr/bin/env python3
"""
LifeLink Medical Identity System
Local HTTP Server for Raspberry Pi 5 & Modern Web Browsers
"""

import http.server
import socketserver
import os
import sys

PORT = 3000
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        # Enable caching and CORS headers for medical asset streaming
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        super().end_headers()

def main():
    port = PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), Handler) as httpd:
        print(f"==================================================")
        print(f" LifeLink Emergency Medical Identity System")
        print(f" Local Terminal Server online at: http://localhost:{port}")
        print(f" Hardware Profile: Raspberry Pi 5 Touchscreen Terminal")
        print(f"==================================================")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down LifeLink server...")
            httpd.server_close()

if __name__ == '__main__':
    main()
