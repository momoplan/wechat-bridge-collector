import tempfile
import unittest
from pathlib import Path

from wechat_bridge_collector.selected_file_upload import MessageAttachment, UploadError, upload_selected_file


class SelectedFileUploadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "export.txt"
        self.original = MessageAttachment("account", "conversation", "source-message", "original")

    def run_upload(self, content, *, existing=False, corrupt=False):
        self.path.write_bytes(content)
        seen = []
        ready = {"status": "complete", "uploadId": "u", "fileId": "f", "mimeType": "text/plain",
                 "sizeBytes": len(content) + int(corrupt)}

        def invoke(method, args):
            seen.append(method)
            if method == "completeFileUpload":
                self.assertEqual(args, {"uploadId": "u"})
                return ready
            self.assertEqual(method, "prepareFileUpload")
            self.assertEqual(args, {"accountId": "account", "conversationId": "conversation",
                                   "messageId": "source-message", "attachmentKey": "original",
                                   "fileName": "export.txt", "sizeBytes": len(content), "mimeType": "text/plain"})
            return ready if existing else {"status": "upload", "uploadId": "u", "transfer": {"test": True}}

        def send(transfer, opened, size):
            self.assertEqual((transfer, opened.read(), size), ({"test": True}, content, len(content)))
            seen.append("upload")

        result = upload_selected_file(self.original, self.path, "text/plain", invoke=invoke, send=send)
        self.assertEqual(result, "f")
        return seen

    def test_module_controls_transfer_and_returns_final_file(self):
        self.assertEqual(self.run_upload(b"selected bytes"), ["prepareFileUpload", "upload", "completeFileUpload"])

    def test_repeat_and_empty_receipts_skip_byte_transfer(self):
        for content in [b"", b"saved"]:
            self.assertEqual(self.run_upload(content, existing=True), ["prepareFileUpload"])

    def test_invalid_size_is_not_success(self):
        with self.assertRaises(UploadError):
            self.run_upload(b"x", corrupt=True)

    def test_storage_failure_stops_before_completion(self):
        self.path.write_bytes(b"x")
        methods = []
        def invoke(method, args):
            methods.append(method)
            return {"status": "upload", "uploadId": "u", "transfer": {}}
        def reject(*args):
            raise UploadError("object storage rejected the upload")
        with self.assertRaises(UploadError):
            upload_selected_file(self.original, self.path, "text/plain", invoke=invoke, send=reject)
        self.assertEqual(methods, ["prepareFileUpload"])


class HostTransportTest(unittest.TestCase):
    def test_credential_only_reaches_the_loopback_module_endpoint(self):
        import json
        import os
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from unittest.mock import patch
        from wechat_bridge_collector.selected_file_upload import call_host
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, self.headers["Authorization"], body))
                payload = json.dumps({"requestId": body["requestId"], "success": True, "data": {"fileId": "f"}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp, HTTPServer(("127.0.0.1", 0), Handler) as server:
            token = Path(tmp) / "credential"
            token.write_text("fixture-token\n")
            endpoint = f"http://127.0.0.1:{server.server_port}/v1/local-app-module-calls"
            with patch.dict(os.environ, {"BAIJIMU_LOCAL_APP_MODULE_ENDPOINT": endpoint,
                                          "BAIJIMU_LOCAL_APP_EVENT_TOKEN_FILE": str(token)}):
                request = threading.Thread(target=server.handle_request, daemon=True)
                request.start()
                self.assertEqual(call_host("completeFileUpload", {"uploadId": "u"}), {"fileId": "f"})
                request.join(2)
                self.assertFalse(request.is_alive())
                os.environ["BAIJIMU_LOCAL_APP_MODULE_ENDPOINT"] = "http://192.0.2.1/v1/local-app-module-calls"
                with self.assertRaises(UploadError):
                    call_host("completeFileUpload", {})
        self.assertEqual(len(received), 1)
        path, authorization, body = received[0]
        self.assertEqual((path, authorization), ("/v1/local-app-module-calls", "Bearer fixture-token"))
        self.assertEqual(set(body), {"requestId", "moduleKey", "method", "arguments"})
