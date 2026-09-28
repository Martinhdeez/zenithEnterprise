"""Disposable public fixture for checking Vite's actual development proxy."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DOCUMENT_ID = "22222222-2222-4222-8222-222222222222"
CHUNK_ID = "11111111-1111-4111-8111-111111111111"
BODY = "Public water guide: at standard pressure, water boils at 100 C."


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass  # Do not record query text or bearer headers.

    def respond(self, body: object, status: int = 200) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        if urlsplit(self.path).path == "/auth/login":
            self.respond({"access_token": "public-fixture-token", "refresh_token": "fixture-refresh"})
        else:
            self.respond({"detail": "fixture route unavailable"}, 404)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/auth/profile":
            self.respond(
                {
                    "user_id": "33333333-3333-4333-8333-333333333333",
                    "email": "public@example.test",
                    "name": "Public Fixture",
                    "tenant_id": "44444444-4444-4444-8444-444444444444",
                    "tenant_name": "Public fixture",
                    "roles": [],
                    "permissions": ["query.execute"],
                    "labels": [],
                    "documents_uploaded": 0,
                    "is_system_admin": False,
                    "created_at": "2026-09-26T00:00:00Z",
                }
            )
        elif path == "/tenant/status":
            self.respond(
                {
                    "documents": {"ready": 1},
                    "chunks": 1,
                    "hardware": "gpu",
                    "components": {"embeddings": True, "reranker": True, "generation": False},
                    "searchable": True,
                }
            )
        elif path == "/labels":
            self.respond([])
        elif path == "/search/capabilities":
            self.respond({"direct_enabled": True})
        elif path == f"/documents/{DOCUMENT_ID}/file":
            encoded = BODY.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        elif path == "/search":
            mode = parse_qs(urlsplit(self.path).query).get("mode", ["legacy"])[0]
            self.respond(
                {
                    "hits": [
                        {
                            "chunk_id": CHUNK_ID,
                            "document_id": DOCUMENT_ID,
                            "filename": "public-water-guide.txt",
                            "media_type": "text/plain",
                            "page_num": None,
                            "char_start": 0,
                            "char_end": len(BODY),
                            "text": BODY,
                            "bboxes": [],
                            "label_ids": [],
                            "lexical_rank": None,
                            "dense_rank": None,
                            "score": 1.0,
                            "lexical_score": None,
                            "dense_score": None,
                            "rerank_score": 1.0,
                        }
                    ],
                    "degraded": False,
                    "reason": None,
                    "took_ms": 12,
                    "relevance": "not_assessed",
                    "receipt": {
                        "version": "evidence-coverage-v1",
                        "strategy": "direct" if mode == "direct" else "hybrid",
                        "coverage_method": "eligible_scope_manifest" if mode == "direct" else "candidate_set",
                        "execution_status": "complete",
                        "eligible_units": 1 if mode == "direct" else None,
                        "selected_units": 1,
                        "assessed_units": 1,
                        "failed_units": 0,
                        "skipped_units": 0,
                        "assessment_windows": 1,
                        "manifest_assessment_complete": mode == "direct",
                        "snapshot_status": "unchanged" if mode == "direct" else "unknown",
                        "source_representation": "eligible_parsed_content",
                        "reason_codes": [],
                    },
                }
            )
        else:
            self.respond({"detail": "fixture route unavailable"}, 404)


ThreadingHTTPServer(("127.0.0.1", 18080), Fixture).serve_forever()
