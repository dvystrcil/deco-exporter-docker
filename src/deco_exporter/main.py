"""Entry point: load config, start the poll loop, serve /metrics and /healthz."""

import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from .collector import DecoCollector
from .config import ConfigError, load_config
from .poller import Poller

log = logging.getLogger("deco_exporter")


def serve(poller, port, addr="0.0.0.0"):
    registry = CollectorRegistry()
    registry.register(DecoCollector(poller))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/metrics":
                body, ctype, code = generate_latest(registry), CONTENT_TYPE_LATEST, 200
            elif path == "/healthz":
                # Liveness of the process only. Router reachability is
                # deco_scrape_success, alerted on in Prometheus: a router
                # outage must not crash-loop the pod that reports it.
                body, ctype, code = b"ok\n", "text/plain", 200
            else:
                body, ctype, code = b"not found\n", "text/plain", 404
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # keep scrape noise out of the logs
            pass

    server = ThreadingHTTPServer((addr, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_config(os.environ)
    except ConfigError as e:
        log.error("config: %s", e)
        return 2

    log.info("starting: host=%s poll_interval=%ss mesh_stats=%s port=%s",
             config.host, config.poll_interval, config.mesh_enabled, config.port)
    poller = Poller(config)
    server, port = serve(poller, config.port)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    poller.run_forever(stop)
    server.shutdown()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
