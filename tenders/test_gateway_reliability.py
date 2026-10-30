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
    @patch.dict(os.environ, {"TIMEWEB_AI_API_KEY": "test"})
    @patch("tenders.services.urlopen")
    def test_document_effort_is_explicit_and_success_retains_token_breakdown(self, gateway):
        usage = {'prompt_tokens': 100, 'completion_tokens': 20,
                 'completion_tokens_details': {'reasoning_tokens': 12, 'text_tokens': 8}}
        gateway.return_value = _Response(json.dumps({
            'choices': [{'message': {'content': '{"items":[]}'}, 'finish_reason': 'stop'}],
            'usage': usage,
        }).encode())
        data, actual = _ai_gateway_json('prompt', model='anthropic/claude-haiku-5-5',
            reasoning_effort='low', capture_incomplete_response=True, json_attempts=1)
        body = json.loads(gateway.call_args.args[0].data)
        self.assertEqual(body['reasoning_effort'], 'low')
        self.assertEqual(actual, usage)
        self.assertEqual(data, {'items': []})
        _ai_gateway_json('prompt', model='other/model')
        self.assertNotIn('reasoning_effort', json.loads(gateway.call_args.args[0].data))

    @patch.dict(os.environ, {"TIMEWEB_AI_API_KEY": "test"})
    @patch("tenders.services.urlopen")
    def test_truncated_document_response_is_retained_only_when_requested(self, gateway):
        content = '{"items":[' + 'я' * 270000
        gateway.return_value = _Response(json.dumps({
            "id": "request-test", "model": "test/model", "choices": [{
                "finish_reason": "length", "message": {"content": content}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 32},
        }).encode())
        with self.assertRaises(TenderAIError) as caught:
            _ai_gateway_json("private input", capture_incomplete_response=True)
        evidence = caught.exception.incomplete_response
        self.assertEqual(len(evidence['content']), 256000)
        self.assertTrue(evidence['capture_truncated'])
        self.assertEqual(evidence['content_characters'], len(content))
        self.assertEqual(caught.exception.usage['completion_tokens'], 32)
        self.assertNotIn('private input', str(evidence))
        self.assertNotIn(content[:100], str(caught.exception))
        gateway.assert_called_once()
        with self.assertRaises(TenderAIError) as other:
            _ai_gateway_json("private input")
        self.assertFalse(hasattr(other.exception, 'incomplete_response'))

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
