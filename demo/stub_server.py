"""A local OpenAI-compatible endpoint for rehearsing the klyd demo offline.

klyd calls a model after every commit. If the room has no wifi, or you do not
want to spend a key on a rehearsal, point klyd at this server:

    py demo/stub_server.py --port 8765 --reply demo/stub_reply.json
    kl config --api-base http://127.0.0.1:8765/v1

It answers /v1/chat/completions with the contents of the reply file and prints
what it received, so you can see the real prompt klyd builds from the diff.

This is a rehearsal tool. It does not call a model, and the decisions it
returns are whatever is in the file. If you record a demo against it, say so on
screen; do not present a canned answer as a model result.

    py demo/stub_server.py --help
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


DEFAULT_REPLY = [
    {
        "decision": (
            "JWT signatures are verified with WebCrypto and the alg header is "
            "ignored; never trust it."
        ),
        "module": "auth",
        "file_patterns": "worker/src/lib/*,worker/src/middleware/*",
        "confidence": "HIGH",
        "event": "NEW",
        "semantic_summary": "JWTs are verified with WebCrypto; alg is untrusted.",
    }
]


class Handler(BaseHTTPRequestHandler):
    reply_text = "[]"
    verbose = True

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        prompt = ""
        messages = body.get("messages") or []
        if messages:
            prompt = messages[0].get("content", "")

        if self.verbose:
            print(f"POST {self.path}  model={body.get('model')}")
            print(f"  prompt: {len(prompt)} chars")
            for line in prompt.splitlines():
                if line.startswith("diff --git"):
                    print(f"  {line}")
            print()

        payload = json.dumps(
            {"choices": [{"message": {"content": type(self).reply_text}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--reply",
        type=Path,
        default=None,
        help="JSON file containing the decision list to return",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.reply is not None:
        Handler.reply_text = args.reply.read_text(encoding="utf-8").strip()
    else:
        Handler.reply_text = json.dumps(DEFAULT_REPLY)
    Handler.verbose = not args.quiet

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"stub listening on http://127.0.0.1:{args.port}/v1")
    print("point klyd at it with: kl config --api-base http://127.0.0.1:"
          f"{args.port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
