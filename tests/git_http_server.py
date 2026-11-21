"""A local git smart-HTTP server that, like GitHub for a private repo, serves
only requests carrying the token in HTTP basic authentication (card #602).

It runs `git http-backend` (a CGI program shipped with git) for each request
that authenticates, and answers 401 to every other one, recording what each
request presented so a test can tell an anonymous attempt from a wrong token.
"""

import base64
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

USER = "x-access-token"


class GitHttpServer:
    def __init__(self, root: Path, token: str) -> None:
        """Serve every repository under `root` to requests that present `token`."""
        self.root = root
        # Each request's Authorization header ("" when it sent none).
        self.authorizations: list[str] = []
        expected = "Basic " + base64.b64encode(f"{USER}:{token}".encode()).decode()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._serve()

            def do_POST(self) -> None:
                self._serve()

            def _serve(self) -> None:
                presented = self.headers.get("Authorization", "")
                server.authorizations.append(presented)
                if presented != expected:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="private"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                parts = urlsplit(self.path)
                env = {
                    "PATH": os.environ.get("PATH", ""),
                    "GIT_PROJECT_ROOT": str(server.root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": self.command,
                    "PATH_INFO": parts.path,
                    "QUERY_STRING": parts.query,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                    "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                    "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                    "REMOTE_USER": USER,
                    "REMOTE_ADDR": "127.0.0.1",
                }
                result = subprocess.run(
                    ["git", "http-backend"], input=body, env=env, capture_output=True, check=False
                )
                head, _, payload = result.stdout.partition(b"\r\n\r\n")
                headers = [line.split(":", 1) for line in head.decode().splitlines() if line]
                status = next((int(v.split()[0]) for k, v in headers if k.lower() == "status"), 200)
                self.send_response(status)
                for key, value in headers:
                    if key.lower() != "status":
                        self.send_header(key, value.strip())
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def url(self, repo: str) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host!s}:{port}/{repo}"

    def __enter__(self) -> "GitHttpServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
