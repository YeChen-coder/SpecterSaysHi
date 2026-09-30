import json
import hashlib
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from phone_link import PhoneLink, classify, make_server, tls_fingerprint


class PhoneLinkTests(unittest.TestCase):
    def test_signal_classification_and_staleness(self):
        sample = {"screen_interactive": True, "device_locked": False,
                  "usage_access": True, "last_interaction_age_ms": 20_000}
        self.assertEqual(classify(sample, 100, 120), "active")
        self.assertEqual(classify(sample, 100, 146), "unknown")
        self.assertEqual(classify({**sample, "device_locked": True}, 100, 120), "idle")
        self.assertEqual(classify({**sample, "usage_access": False}, 100, 120), "unknown")

    def test_authenticated_http_round_trip(self):
        link = PhoneLink()
        server = make_server(link, "test-secret", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def call(path, value=None, token="test-secret"):
            request = urllib.request.Request(
                base + path, data=json.dumps(value).encode() if value is not None else None,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.load(response)

        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                call("/v1/status", token="wrong")
            self.assertEqual(error.exception.code, 401)
            sample = {"screen_interactive": True, "device_locked": False,
                      "usage_access": True, "last_interaction_age_ms": 1000}
            self.assertEqual(call("/v1/status", sample)["signal"], "active")
            self.assertEqual(call("/v1/status")["signal"], "active")
            command = call("/v1/commands", {"name": "focus_on"})
            self.assertEqual(call("/v1/commands/next")["command"]["id"], command["id"])
            self.assertTrue(call("/v1/commands/ack", {"id": command["id"]})["acknowledged"])
            self.assertIsNone(call("/v1/commands/next")["command"])
            with self.assertRaises(urllib.error.HTTPError) as error:
                call("/v1/commands", {"name": "anything_else"})
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_stale_signal_emits_unknown_transition(self):
        transitions = []
        link = PhoneLink(lambda signal, sample: transitions.append(signal))
        link.ingest({"screen_interactive": True, "device_locked": False,
                     "usage_access": True, "last_interaction_age_ms": 1000})
        link.received_at -= 46
        link.expire_signal()
        link.expire_signal()
        self.assertEqual(transitions, ["active", "unknown"])

    def test_first_unknown_sample_still_announces_state(self):
        transitions = []
        link = PhoneLink(lambda signal, sample: transitions.append(signal))
        link.ingest({"screen_interactive": True, "device_locked": False,
                     "usage_access": True, "last_interaction_age_ms": None})
        self.assertEqual(transitions, ["unknown"])

    def test_lan_tls_certificate_and_authenticated_status(self):
        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory) / "cert.pem"
            key = Path(directory) / "key.pem"
            with patch("phone_link.CERT_FILE", cert), patch("phone_link.KEY_FILE", key):
                expected = tls_fingerprint()
                server = make_server(PhoneLink(), "test-secret", 0)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(str(cert), str(key))
                server.socket = context.wrap_socket(server.socket, server_side=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    host = "127.0.0.1"
                    port = server.server_port
                    pem = ssl.get_server_certificate((host, port))
                    observed = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
                    self.assertEqual(observed, expected)
                    request = urllib.request.Request(
                        f"https://{host}:{port}/v1/status",
                        headers={"Authorization": "Bearer test-secret"},
                    )
                    with urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=2) as response:
                        self.assertEqual(json.load(response)["signal"], "unknown")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
