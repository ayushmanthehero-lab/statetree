import asyncio
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber
from strands import Agent, tool
from strands.hooks import AfterModelCallEvent, BeforeModelCallEvent

from statetree.adapters.strands import StateTreeHooks
from statetree.context import ContextBudgetError
from statetree.models import SageMakerModel
from statetree.runtime.usage import UsageLedger
from statetree.storage.local import LocalStateStore


def completion(content='done', *, calls=None, usage=None, finish='stop', reasoning=None):
    message = {'role': 'assistant', 'content': content}
    if calls is not None:
        message['tool_calls'] = calls
    if reasoning is not None:
        message['reasoning_content'] = reasoning
    result = {'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 1,
              'model': 'Qwen/Qwen3.5-4B',
              'choices': [{'index': 0, 'message': message, 'finish_reason': finish}]}
    if usage is not None:
        result['usage'] = usage
    return result


def call(name='lookup', arguments='{}', call_id='call-1'):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': arguments}}


class Transport:
    """In-memory AWS boundary: captures requests and owns readable response bodies."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.bodies = []
        self.threads = []

    def invoke_endpoint(self, **request):
        self.requests.append(request)
        self.threads.append(threading.get_ident())
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        body = io.BytesIO(response if isinstance(response, bytes) else json.dumps(response).encode())
        self.bodies.append(body)
        return {'Body': body, 'ContentType': 'application/json'}


def collect(model, messages=None, **kwargs):
    async def run():
        return [event async for event in model.stream(
            messages or [{'role': 'user', 'content': [{'text': 'hello'}]}], **kwargs)]
    return asyncio.run(run())


class SageMakerTests(unittest.TestCase):
    def model(self, *responses, **kwargs):
        transport = Transport(*responses)
        return SageMakerModel('qwen-small', client=transport, **kwargs), transport

    def hook(self, **kwargs):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = UsageLedger(Path(temp.name) / 'usage.sqlite3')
        hook = StateTreeHooks(store=LocalStateStore(Path(temp.name) / 'state'),
                              ledger=ledger, goal='Resolve the task', run_id='sagemaker-test',
                              input_cache_convention='included', **kwargs)
        return hook, ledger

    def test_invocation_matches_boto3_contract_and_actual_usage(self):
        client = boto3.client('sagemaker-runtime', region_name='us-east-1',
                              aws_access_key_id='test', aws_secret_access_key='test')
        response = completion('hello back', usage={
            'prompt_tokens': 80, 'completion_tokens': 9, 'total_tokens': 89,
            'prompt_tokens_details': {'cached_tokens': 20}})
        raw = json.dumps(response).encode()
        body = StreamingBody(io.BytesIO(raw), len(raw))
        expected = {'model': 'Qwen/Qwen3.5-4B',
                    'messages': [{'role': 'system', 'content': 'Use evidence.'},
                                 {'role': 'user', 'content': 'hello'}],
                    'max_tokens': 512, 'temperature': 0.7, 'stream': False,
                    'chat_template_kwargs': {'enable_thinking': False}}
        with Stubber(client) as stub:
            stub.add_response('invoke_endpoint', {'Body': body, 'ContentType': 'application/json'},
                              {'EndpointName': 'qwen-small', 'ContentType': 'application/json',
                               'Accept': 'application/json',
                               'Body': json.dumps(expected, ensure_ascii=False, allow_nan=False).encode(),
                               'InferenceComponentName': 'qwen-component'})
            model = SageMakerModel('qwen-small', client=client,
                                   inference_component_name='qwen-component')
            hook, ledger = self.hook()
            result = Agent(model=model, system_prompt='Use evidence.', hooks=[hook],
                           callback_handler=None)('hello')
            self.assertEqual(result.message['content'], [{'text': 'hello back'}])
            self.assertEqual(ledger.summary()['total_tokens'], 89)
            self.assertEqual(ledger.summary()['cache_read_input_tokens'], 20)
            self.assertEqual(ledger.records()[0]['model'], 'Qwen/Qwen3.5-4B')
            self.assertTrue(body._raw_stream.closed)
            stub.assert_no_pending_responses()

    def test_real_agent_tool_loop_preserves_ids_inputs_and_json_results(self):
        @tool
        def lookup(key: str) -> dict:
            """Find a named value."""
            return {'key': key, 'value': 42}

        usage = {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}
        model, transport = self.model(completion(None, calls=[call(arguments='{"key":"answer"}')],
                                                  finish='tool_calls', usage=usage),
                                      completion('The answer is 42.', usage=usage))
        hook, ledger = self.hook(max_input_tokens=6000)
        result = Agent(model=model, tools=[lookup], hooks=[hook], context_manager=False,
                       callback_handler=None)('Find the answer')
        self.assertEqual(result.message['content'], [{'text': 'The answer is 42.'}])
        self.assertEqual(ledger.summary()['requests'], 2)
        self.assertEqual(ledger.summary()['total_tokens'], 220)
        payload = json.loads(transport.requests[1]['Body'])
        self.assertIn('StateTree context:', payload['messages'][0]['content'])
        assistant = next(item for item in payload['messages'] if item['role'] == 'assistant')
        self.assertEqual(assistant['tool_calls'][0], call(arguments='{"key": "answer"}'))
        result = next(item for item in payload['messages'] if item['role'] == 'tool')
        self.assertEqual(result['tool_call_id'], 'call-1')
        self.assertIn('42', result['content'])
        self.assertEqual(payload['tools'][0]['function']['name'], 'lookup')
        self.assertEqual(payload['tools'][0]['function']['parameters']['type'], 'object')
        self.assertNotEqual(transport.threads[0], threading.get_ident())
        self.assertTrue(all(body.closed for body in transport.bodies))

    def test_missing_and_partial_usage_stay_unknown_in_ledger(self):
        for usage in [None, {}, {'prompt_tokens': 8}, {'completion_tokens': 2}]:
            with self.subTest(usage=usage):
                model, _ = self.model(completion(usage=usage))
                hook, ledger = self.hook()
                Agent(model=model, hooks=[hook], callback_handler=None)('hello')
                self.assertEqual(ledger.summary()['unknown_usage_requests'], 1)
                self.assertEqual(ledger.summary()['known_usage_requests'], 0)
                recorded = ledger.records()[0]['usage']
                expected = None if usage is None else {
                    {'prompt_tokens': 'inputTokens', 'completion_tokens': 'outputTokens'}[key]: value
                    for key, value in usage.items()}
                self.assertEqual(recorded, expected)

    def test_reported_zero_usage_is_known_and_error_cannot_reuse_prior_usage(self):
        model, transport = self.model(completion(usage={
            'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}), RuntimeError('offline'))
        hook, ledger = self.hook()
        agent = Agent(model=model, hooks=[hook], callback_handler=None)
        agent('first')
        with self.assertRaisesRegex(RuntimeError, 'offline'):
            agent('second')
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(ledger.summary()['known_usage_requests'], 1)
        self.assertEqual(ledger.summary()['unknown_usage_requests'], 1)
        self.assertIsNone(model.last_provider_usage)

    def test_malformed_response_records_valid_usage_without_yielding_partial_output(self):
        usage = {'prompt_tokens': 80, 'completion_tokens': 9, 'total_tokens': 89}
        response = completion('text before bad tool', calls=[call(arguments='[]')],
                              finish='tool_calls', usage=usage)
        model, transport = self.model(response)
        seen = []
        async def run():
            async for event in model.stream([{'role': 'user', 'content': [{'text': 'hello'}]}]):
                seen.append(event)
        with self.assertRaisesRegex(ValueError, 'JSON object'):
            asyncio.run(run())
        self.assertEqual(seen, [])
        self.assertEqual(model.last_provider_usage, {'inputTokens': 80, 'outputTokens': 9, 'totalTokens': 89})
        self.assertTrue(transport.bodies[0].closed)

        model, transport = self.model(response, RuntimeError('network failed'))
        hook, ledger = self.hook()
        agent = Agent(model=model, hooks=[hook], callback_handler=None)
        with self.assertRaisesRegex(ValueError, 'JSON object'):
            agent('first')
        with self.assertRaisesRegex(RuntimeError, 'network failed'):
            agent('second')
        self.assertEqual(ledger.records()[0]['usage'], {'inputTokens': 80, 'outputTokens': 9, 'totalTokens': 89})
        self.assertEqual(ledger.records()[0]['status'], 'error')
        self.assertIsNone(ledger.records()[1]['usage'])
        self.assertEqual(ledger.summary()['total_tokens'], 89)
        self.assertEqual(ledger.summary()['unknown_usage_requests'], 1)

    def test_malformed_usage_after_success_is_unavailable_and_cannot_reuse_old_counts(self):
        model, transport = self.model(completion(usage={
            'prompt_tokens': 80, 'completion_tokens': 9, 'total_tokens': 89}),
            completion(usage={'prompt_tokens': 50, 'completion_tokens': 'bad', 'total_tokens': 59}))
        hook, ledger = self.hook()
        agent = Agent(model=model, hooks=[hook], callback_handler=None)
        agent('first')
        with self.assertRaisesRegex(ValueError, 'Usage counts'):
            agent('second')
        self.assertIsNone(model.last_provider_usage)
        self.assertIsNone(ledger.records()[1]['usage'])
        self.assertEqual(ledger.summary()['total_tokens'], 89)
        self.assertTrue(all(body.closed for body in transport.bodies))

    def test_failure_before_stream_starts_cannot_reuse_old_provider_counts(self):
        model, transport = self.model(completion(usage={
            'prompt_tokens': 80, 'completion_tokens': 9, 'total_tokens': 89}))
        hook, ledger = self.hook()
        agent = Agent(model=model, hooks=[hook], callback_handler=None)
        def fail_before_stream(event):
            if transport.requests:
                raise RuntimeError('preparation failed')
        agent.hooks.add_callback(BeforeModelCallEvent, fail_before_stream, order=300)
        agent('first')
        with self.assertRaisesRegex(RuntimeError, 'preparation failed'):
            agent('second')
        self.assertEqual(len(transport.requests), 1)
        self.assertIsNone(ledger.records()[1]['usage'])
        self.assertEqual(ledger.summary()['total_tokens'], 89)

    def test_synthetic_stop_without_stream_cannot_reuse_old_provider_counts(self):
        model, transport = self.model(completion(usage={
            'prompt_tokens': 80, 'completion_tokens': 9, 'total_tokens': 89}))
        hook, ledger = self.hook()
        agent = Agent(model=model, hooks=[hook], callback_handler=None)
        agent('first')
        # Middleware can short-circuit a model call with a synthetic completion.
        hook.before_model(BeforeModelCallEvent(agent=agent, invocation_state={}))
        hook.after_model(AfterModelCallEvent(agent=agent, invocation_state={},
            stop_response=AfterModelCallEvent.ModelStopResponse(
                stop_reason='end_turn', message={'role': 'assistant', 'content': [{'text': 'synthetic'}]})))
        self.assertEqual(len(transport.requests), 1)
        self.assertIsNone(ledger.records()[1]['usage'])
        self.assertEqual(ledger.summary()['total_tokens'], 89)

    def test_budget_rejection_prevents_invocation(self):
        model, transport = self.model()
        hook, ledger = self.hook(max_input_tokens=100)
        agent = Agent(model=model, hooks=[hook], context_manager=False, callback_handler=None)
        with self.assertRaises(ContextBudgetError):
            agent('request ' * 100)
        self.assertEqual(transport.requests, [])
        self.assertEqual(ledger.summary()['requests'], 0)

    def test_text_blocks_system_priority_and_error_tool_result(self):
        model, transport = self.model(completion())
        collect(model, [
            {'role': 'user', 'content': [{'text': 'one'}, {'text': 'two'}]},
            {'role': 'assistant', 'content': [{'toolUse': {
                'toolUseId': 'call-1', 'name': 'lookup', 'input': {}}}]},
            {'role': 'user', 'content': [{'toolResult': {
                'toolUseId': 'call-1', 'status': 'error',
                'content': [{'text': 'failed'}, {'json': {'code': 9}}]}}]},
        ], system_prompt='ignored', system_prompt_content=[{'text': 'first'}, {'text': 'second'}])
        messages = json.loads(transport.requests[0]['Body'])['messages']
        self.assertEqual(messages[0], {'role': 'system', 'content': 'first\nsecond'})
        self.assertEqual(messages[1]['content'], 'one\ntwo')
        self.assertEqual(messages[-1]['content'], 'Error: failed\n{"code": 9}')

    def test_tool_choice_conversion_and_rejection_before_inference(self):
        spec = {'name': 'lookup', 'description': 'Look up.', 'inputSchema': {'json': {'type': 'object'}}}
        for choice, expected in [({'auto': {}}, 'auto'), ({'any': {}}, 'required'),
                                 ({'tool': {'name': 'lookup'}}, {'type': 'function', 'function': {'name': 'lookup'}})]:
            with self.subTest(choice=choice):
                model, transport = self.model(completion())
                collect(model, tool_specs=[spec], tool_choice=choice)
                self.assertEqual(json.loads(transport.requests[0]['Body'])['tool_choice'], expected)
        for choice in [{'tool': {'name': 'missing'}}, {'none': {}}, {'auto': {}, 'any': {}}]:
            model, transport = self.model()
            with self.assertRaises(ValueError):
                collect(model, tool_specs=[spec], tool_choice=choice)
            self.assertEqual(transport.requests, [])

    def test_unsupported_content_and_non_json_arguments_fail_before_inference(self):
        for block in [{'image': {'format': 'png'}}, {'text': 'ok', 'image': {}},
                      {'toolUse': {'toolUseId': 'id', 'name': 'lookup', 'input': {'bad': float('nan')}}},
                      {'toolUse': {'toolUseId': 'id', 'name': 'lookup', 'input': []}},
                      {'toolResult': {'toolUseId': 'missing', 'content': [{'text': 'x'}]}}]:
            model, transport = self.model()
            role = 'assistant' if 'toolUse' in block else 'user'
            with self.subTest(block=block), self.assertRaises((TypeError, ValueError)):
                collect(model, [{'role': role, 'content': [block]}])
            self.assertEqual(transport.requests, [])
        model, transport = self.model()
        with self.assertRaises(ValueError):
            collect(model, system_prompt_content=[{'cachePoint': {'type': 'default'}}])
        self.assertEqual(transport.requests, [])

    def test_nonstring_json_keys_are_rejected_in_tool_input_results_and_schema(self):
        bad_json = {'nested': [{1: 'silently coerced'}]}
        use = {'toolUse': {'toolUseId': 'id', 'name': 'lookup', 'input': bad_json}}
        result = {'toolResult': {'toolUseId': 'id', 'content': [{'json': bad_json}]}}
        for messages, specs in [
            ([{'role': 'assistant', 'content': [use]},
              {'role': 'user', 'content': [{'toolResult': {
                  'toolUseId': 'id', 'content': [{'text': 'done'}]}}]}], None),
            ([{'role': 'assistant', 'content': [{'toolUse': {
                'toolUseId': 'id', 'name': 'lookup', 'input': {}}}]},
              {'role': 'user', 'content': [result]}], None),
            ([{'role': 'user', 'content': [{'text': 'hello'}]}],
             [{'name': 'lookup', 'inputSchema': {'json': bad_json}}]),
        ]:
            model, transport = self.model(completion())
            with self.subTest(messages=messages, specs=specs), self.assertRaisesRegex(ValueError, 'JSON object keys'):
                collect(model, messages, tool_specs=specs)
            self.assertEqual(transport.requests, [])

    def test_response_is_fully_validated_before_any_event_and_body_always_closed(self):
        invalid = [b'not json', {}, completion(calls=[call(arguments='[]')], finish='tool_calls'),
                   completion(calls=[call(arguments='{"x":NaN}')], finish='tool_calls'),
                   completion(calls=[call(), call()], finish='tool_calls'),
                   completion(calls=[call(call_id='')], finish='tool_calls'),
                   completion(finish='unsupported'), completion(finish='tool_calls'),
                   completion(content={'text': 'wrong'}), completion(usage={'prompt_tokens': -1}),
                   completion(usage={'completion_tokens': True})]
        for response in invalid:
            with self.subTest(response=response):
                model, transport = self.model(response)
                seen = []
                async def run():
                    async for event in model.stream([{'role': 'user', 'content': [{'text': 'hello'}]}]):
                        seen.append(event)
                with self.assertRaises((ValueError, TypeError)):
                    asyncio.run(run())
                self.assertEqual(seen, [])
                self.assertTrue(transport.bodies[0].closed)
                self.assertIsNone(model.last_provider_usage)

    def test_malformed_empty_tool_calls_do_not_silently_disappear(self):
        for calls in [{}, '', False, 0]:
            model, transport = self.model(completion(calls=calls))
            with self.subTest(calls=calls), self.assertRaises(ValueError):
                collect(model)
            self.assertTrue(transport.bodies[0].closed)

    def test_body_is_closed_if_read_fails(self):
        class FailingBody(io.BytesIO):
            def read(self, *args):
                raise OSError('response interrupted')

        body = FailingBody(b'')
        class Client:
            def invoke_endpoint(self, **kwargs):
                return {'Body': body}

        with self.assertRaisesRegex(OSError, 'response interrupted'):
            collect(SageMakerModel('endpoint', client=Client()))
        self.assertTrue(body.closed)

    def test_reasoning_and_finish_reasons_use_strands_events(self):
        for finish, expected in [('stop', 'end_turn'), ('length', 'max_tokens')]:
            model, _ = self.model(completion('answer', reasoning='Think.', finish=finish))
            events = collect(model)
            self.assertIn({'contentBlockDelta': {'delta': {'reasoningContent': {'text': 'Think.'}}}}, events)
            self.assertIn({'messageStop': {'stopReason': expected}}, events)
            self.assertTrue(any(event.get('contentBlockDelta', {}).get('delta') == {'text': 'answer'} for event in events))

    def test_config_validation_is_atomic_detached_and_does_not_expose_client(self):
        model, transport = self.model(completion())
        original = model.get_config()
        for kwargs in [{'endpoint_name': ''}, {'temperature': float('nan')}, {'temperature': -1},
                       {'max_tokens': True}, {'max_tokens': 0}, {'context_window_limit': 0},
                       {'enable_thinking': 1}, {'model_id': ''}, {'unknown': 1},
                       {'inference_component_name': ''}, {'region_name': ''}, {'profile_name': ''}]:
            with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                model.update_config(**{**kwargs, 'max_tokens': kwargs.get('max_tokens', 99)})
            self.assertEqual(model.get_config(), original)
        detached = model.get_config()
        detached['endpoint_name'] = 'wrong'
        self.assertNotIn('client', original)
        self.assertEqual(model.get_config()['endpoint_name'], 'qwen-small')
        model.update_config(endpoint_name='qwen-large', model_id='Qwen/Qwen3.8-27B', max_tokens=128)
        collect(model)
        self.assertEqual(transport.requests[0]['EndpointName'], 'qwen-large')
        self.assertEqual(json.loads(transport.requests[0]['Body'])['model'], 'Qwen/Qwen3.8-27B')

    def test_default_boto_client_is_lazy_and_has_one_attempt(self):
        transport = Transport(completion())
        with patch('boto3.Session') as session:
            session.return_value.client.return_value = transport
            model = SageMakerModel('endpoint', region_name='us-east-1', profile_name='local-test')
            self.assertFalse(session.called)
            model.get_config()
            self.assertFalse(session.called)
            collect(model)
            session.assert_called_once_with(profile_name='local-test', region_name='us-east-1')
            args, kwargs = session.return_value.client.call_args
            self.assertEqual(args, ('sagemaker-runtime',))
            self.assertEqual(kwargs['config'].retries, {'total_max_attempts': 1})
            self.assertEqual(kwargs['config'].read_timeout, 70)
            self.assertEqual(kwargs['config'].connect_timeout, 10)

    def test_direct_structured_output_is_explicitly_unsupported(self):
        model, transport = self.model()
        async def run():
            return [item async for item in model.structured_output(dict, [])]
        with self.assertRaisesRegex(NotImplementedError, 'structured_output'):
            asyncio.run(run())
        self.assertEqual(transport.requests, [])


if __name__ == '__main__':
    unittest.main()
