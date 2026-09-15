"""Owned loopback proxy: observe committed SQLite row before closing client socket."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
from threading import Thread
import httpx
from agentcheck_biz.checks import observe_database
from agentcheck_biz.events import EventLog


class LossProxy:
    def __init__(self, service, directory, context, enabled):
        self.events = EventLog(directory/'loss-proxy.jsonl', context.run_id)
        self.hit = False
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self): self.forward()
            def do_POST(self): self.forward()
            def forward(self):
                if self.path not in {'/tickets','/version'}:
                    self.send_error(404); return
                body = self.rfile.read(int(self.headers.get('Content-Length','0')))
                headers = {k:v for k,v in self.headers.items() if k.lower() not in {'host','content-length','connection'}}
                with httpx.Client(trust_env=False, timeout=5, follow_redirects=False) as client:
                    response = client.request(self.command, service.transport.origin+self.path, headers=headers, content=body)
                if enabled and not outer.hit and self.command=='POST' and response.status_code==200:
                    ticket = response.json()
                    observed = observe_database(directory/'business.sqlite')
                    matches = [r for r in observed['tickets'] if r.get('ticket_id')==ticket.get('ticket_id')
                        and r.get('tenant_id')=='tenant-A' and r.get('operation_id')==context.operation_id]
                    if observed['run_id'] != context.run_id or len(matches)!=1:
                        self.send_error(502); return
                    outer.hit=True
                    outer.events.record('committed_response_dropped', request_id=self.headers.get('X-Request-Id'),
                        ticket=matches[0], database_run_id=observed['run_id'], upstream_status=200)
                    self.close_connection=True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                outer.events.record('forwarded',method=self.command,status=response.status_code,
                    request_id=self.headers.get('X-Request-Id'))
                self.send_response(response.status_code)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(response.content)))
                self.end_headers(); self.wfile.write(response.content)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.origin='http://127.0.0.1:'+str(self.server.server_port)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
