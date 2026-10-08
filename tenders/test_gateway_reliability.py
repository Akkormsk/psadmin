import json
import os
from unittest.mock import patch

from django.test import SimpleTestCase

from .services import TenderAIError, _ai_gateway_json


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class GatewayReliabilityTests(SimpleTestCase):
    @patch.dict(os.environ, {"TIMEWEB_AI_API_KEY": "test", "TIMEWEB_AI_BASE_URL": "https://gateway.test"})
    @patch("tenders.services.urlopen")
    def test_malformed_first_response_is_repaired_once_and_usage_is_combined(self, urlopen_mock):
        broken = json.dumps({
            "choices": [{"message": {"content": "not json"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }).encode()
        valid = json.dumps({
            "choices": [{"message": {"content": '{"items": []}'}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 40},
        }).encode()
        urlopen_mock.side_effect = [_Response(broken), _Response(valid)]

        payload, usage = _ai_gateway_json("prompt", model="test/model", network_attempts=1)

        self.assertEqual(payload, {"items": []})
        self.assertEqual(usage, {"prompt_tokens": 40, "completion_tokens": 60})
        self.assertEqual(urlopen_mock.call_count, 2)

    @patch.dict(os.environ, {"TIMEWEB_AI_API_KEY": "test", "TIMEWEB_AI_BASE_URL": "https://gateway.test"})
    @patch("tenders.services.urlopen")
    def test_repeated_invalid_response_has_usage_for_failed_call_accounting(self, urlopen_mock):
        broken = json.dumps({
            "choices": [{"message": {"content": "not json"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }).encode()
        urlopen_mock.side_effect = [_Response(broken), _Response(broken)]

        with self.assertRaises(TenderAIError) as error:
            _ai_gateway_json("prompt", model="test/model", network_attempts=1)

        self.assertEqual(error.exception.usage, {"prompt_tokens": 20, "completion_tokens": 40})
