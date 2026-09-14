"""Explicit localhost read-path outage fixture; never forwards or performs writes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
from pathlib import Path
from agentcheck_biz.events import EventLog
from agentcheck_biz.reports import save_json


def main():
    config = json.loads(sys.stdin.readline())
    if config.get('test_only') is not True:
        raise ValueError('Explicit isolated outage test required')
    path = Path(config['directory'])
    events = EventLog(path/'outage-events.jsonl', path.name)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            events.record('read_unavailable', path=self.path, method='GET', status_code=503, pid=os.getpid())
            data = b'{"detail":"isolated test read outage"}'
            self.send_response(503)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    with ThreadingHTTPServer(('127.0.0.1',0), Handler) as server:
        save_json(path/'outage-ready.json', dict(pid=os.getpid(), origin='http://127.0.0.1:'+str(server.server_port)))
        server.serve_forever()


if __name__ == '__main__':
    main()
