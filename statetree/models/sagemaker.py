"""Strands text/tool transport for SageMaker AI's OpenAI-compatible vLLM DLC.

Uses boto3 InvokeEndpoint with an OpenAI chat-completion JSON body at
``/invocations``; the container must support that contract. See
https://docs.aws.amazon.com/sagemaker/latest/dg/realtime-endpoints-openai-compatible.html
Regular InvokeEndpoint has a 60-second container-response limit. This adapter
buffers one nonstreaming response before emitting Strands events; it does not
provide token-by-token network streaming. Keep generation within that limit.
The default client is created on the first request, with SDK retries disabled.
Injected clients retain their caller-configured transport and retry policy.
"""

import asyncio
import copy
import json
import math
from threading import Lock
from time import perf_counter

from strands.models import Model


def _text(value, label):
    if not isinstance(value, str):
        raise ValueError(f'{label} must be text')
    return value


def _name(value, label):
    if not _text(value, label).strip():
        raise ValueError(f'{label} must not be empty')
    return value


def _json(value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    # The encoder catches cycles/nonfinite values, but silently coerces integer
    # object keys. Check the original tree before handing that JSON to a model.
    def check_keys(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError('JSON object keys must be strings')
            for child in item.values():
                check_keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check_keys(child)
    check_keys(value)
    return encoded


def _invalid_constant(value):
    raise ValueError(f'Invalid JSON constant: {value}')


def _loads(value):
    return json.loads(value, parse_constant=_invalid_constant)


def _tool_call(value, ids):
    identifier = _name(value.get('toolUseId'), 'toolUseId')
    if identifier in ids:
        raise ValueError('Duplicate tool call ID')
    ids.add(identifier)
    arguments = value.get('input')
    if not isinstance(arguments, dict):
        raise ValueError('Tool arguments must be a JSON object')
    return {'id': identifier, 'type': 'function', 'function': {
        'name': _name(value.get('name'), 'tool name'), 'arguments': _json(arguments)}}


def _messages(messages, system_prompt, system_prompt_content):
    result, pending, seen = [], set(), set()
    if system_prompt_content is not None:
        if any(not isinstance(block, dict) or set(block) != {'text'} for block in system_prompt_content):
            raise ValueError('Only text system prompt blocks are supported')
        system_prompt = '\n'.join(_text(block['text'], 'system prompt') for block in system_prompt_content)
    if system_prompt is not None:
        result.append({'role': 'system', 'content': _text(system_prompt, 'system prompt')})
    for message in messages:
        role = message.get('role')
        if role not in ('user', 'assistant') or not isinstance(message.get('content'), list):
            raise ValueError('Expected user/assistant messages with content blocks')
        texts, calls, reasoning, tool_results = [], [], [], []
        for block in message['content']:
            if not isinstance(block, dict) or len(block) != 1:
                raise ValueError('Expected one supported content type per block')
            if 'text' in block:
                texts.append(_text(block['text'], 'message text'))
            elif 'toolUse' in block and role == 'assistant':
                calls.append(_tool_call(block['toolUse'], seen))
            elif 'toolResult' in block and role == 'user':
                value = block['toolResult']
                identifier = _name(value.get('toolUseId'), 'tool result ID')
                if identifier not in pending:
                    raise ValueError('Tool result has no unmatched tool call')
                pending.remove(identifier)
                parts = []
                for part in value.get('content', []):
                    if set(part) == {'text'}:
                        parts.append(_text(part['text'], 'tool result text'))
                    elif set(part) == {'json'}:
                        parts.append(_json(part['json']))
                    else:
                        raise ValueError('Only text and JSON tool results are supported')
                if value.get('status', 'success') not in ('success', 'error'):
                    raise ValueError('Unsupported tool result status')
                content = '\n'.join(parts)
                if value.get('status') == 'error':
                    content = 'Error: ' + content
                tool_results.append({'role': 'tool', 'tool_call_id': identifier, 'content': content})
            elif 'reasoningContent' in block and role == 'assistant':
                value = block['reasoningContent']
                if set(value) != {'reasoningText'} or set(value['reasoningText']) != {'text'}:
                    raise ValueError('Only plain-text reasoning is supported')
                reasoning.append(_text(value['reasoningText']['text'], 'reasoning text'))
            else:
                raise ValueError('Only text, toolUse, toolResult and text reasoning blocks are supported')
        if pending and (role == 'assistant' or texts):
            raise ValueError('Tool calls must have results before the next message')
        result.extend(tool_results)
        if texts or calls or reasoning or not tool_results:
            converted = {'role': role, 'content': '\n'.join(texts) if texts else None}
            if calls:
                converted['tool_calls'] = calls
                pending.update(item['id'] for item in calls)
            if reasoning:
                converted['reasoning_content'] = '\n'.join(reasoning)
            result.append(converted)
    if pending:
        raise ValueError('Conversation has tool calls without results')
    return result


def _tools(specs, choice):
    result, names = {}, set()
    converted = []
    for spec in specs or []:
        name = _name(spec.get('name'), 'tool name')
        schema = spec.get('inputSchema', {}).get('json')
        if name in names or not isinstance(schema, dict):
            raise ValueError('Tools need unique names and JSON object schemas')
        names.add(name)
        converted.append({'type': 'function', 'function': {
            'name': name, 'description': _text(spec.get('description', ''), 'tool description'),
            'parameters': schema}})
    if converted:
        result['tools'] = converted
    if choice is not None:
        if not names or not isinstance(choice, dict) or len(choice) != 1:
            raise ValueError('tool_choice requires tools and one selection')
        if choice == {'auto': {}}:
            result['tool_choice'] = 'auto'
        elif choice == {'any': {}}:
            result['tool_choice'] = 'required'
        elif set(choice) == {'tool'} and choice['tool'].get('name') in names:
            result['tool_choice'] = {'type': 'function', 'function': {'name': choice['tool']['name']}}
        else:
            raise ValueError('Unsupported or unregistered tool_choice')
    return result


def _response(value):
    choices = value.get('choices') if isinstance(value, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError('Expected an OpenAI chat completion with choices')
    choice = choices[0]
    message = choice.get('message')
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        raise ValueError('Expected an assistant response')
    blocks, ids = [], set()
    for field, kind in [('reasoning_content', 'reasoningContent'), ('content', 'text')]:
        content = message.get(field)
        if content is not None:
            content = _text(content, field)
            blocks.append(({}, {kind: {'text': content} if kind == 'reasoningContent' else content}))
    calls = message.get('tool_calls')
    if calls is None:
        calls = []
    if not isinstance(calls, list):
        raise ValueError('tool_calls must be a list')
    for item in calls:
        if not isinstance(item, dict) or item.get('type') != 'function' or not isinstance(item.get('function'), dict):
            raise ValueError('Only function tool calls are supported')
        function = item['function']
        arguments = _loads(_text(function.get('arguments'), 'tool arguments'))
        normalized = _tool_call({'toolUseId': item.get('id'), 'name': function.get('name'), 'input': arguments}, ids)
        blocks.append(({'toolUse': {'toolUseId': normalized['id'], 'name': function['name']}},
                       {'toolUse': {'input': normalized['function']['arguments']}}))
    reason = choice.get('finish_reason')
    if reason not in ('stop', 'length', 'tool_calls') or bool(calls) != (reason == 'tool_calls'):
        raise ValueError('Unsupported or inconsistent finish_reason')
    if not blocks:
        raise ValueError('Assistant response contains neither text nor tool calls')
    return blocks, {'stop': 'end_turn', 'length': 'max_tokens', 'tool_calls': 'tool_use'}[reason]


def _usage(value):
    usage = value.get('usage') if isinstance(value, dict) else None
    if usage is not None:
        if not isinstance(usage, dict):
            raise ValueError('usage must be an object')
        mapping = {'prompt_tokens': 'inputTokens', 'completion_tokens': 'outputTokens', 'total_tokens': 'totalTokens'}
        normalized = {target: usage[source] for source, target in mapping.items() if source in usage}
        details = usage.get('prompt_tokens_details')
        if details is not None:
            if not isinstance(details, dict):
                raise ValueError('prompt_tokens_details must be an object')
            if 'cached_tokens' in details:
                normalized['cacheReadInputTokens'] = details['cached_tokens']
        if any(type(count) is not int or count < 0 for count in normalized.values()):
            raise ValueError('Usage counts must be nonnegative integers')
        usage = normalized
    return usage


class SageMakerModel(Model):
    """Buffered OpenAI chat transport; credentials are resolved only on request.

    ``last_provider_usage`` preserves valid counts even if response content is
    malformed, since generation still consumed tokens. Missing/malformed counts
    stay unavailable. ``provider_call_sequence`` increments when each stream
    begins, letting hooks reject stale usage if preparation fails beforehand.
    Cached prompt tokens are
    included in vLLM's prompt count: use ``input_cache_convention='included'``.
    ``context_window_limit`` describes the deployed server limit, not a tokenizer.
    """

    def __init__(self, endpoint_name, *, region_name=None, model_id='Qwen/Qwen3.5-4B',
                 max_tokens=512, temperature=0.7, enable_thinking=False, client=None,
                 context_window_limit=8192, inference_component_name=None, profile_name=None):
        self._config = {}
        self._client, self._injected_client = client, client is not None
        self._client_lock = Lock()
        self._last_provider_usage = None
        self._provider_call_sequence = 0
        self.update_config(endpoint_name=endpoint_name, region_name=region_name, model_id=model_id,
                           max_tokens=max_tokens, temperature=temperature, enable_thinking=enable_thinking,
                           context_window_limit=context_window_limit,
                           inference_component_name=inference_component_name, profile_name=profile_name)

    @property
    def last_provider_usage(self):
        return copy.deepcopy(self._last_provider_usage)

    @property
    def provider_call_sequence(self):
        return self._provider_call_sequence

    def get_config(self):
        return copy.deepcopy(self._config)

    def update_config(self, **kwargs):
        allowed = {'endpoint_name', 'region_name', 'model_id', 'max_tokens', 'temperature',
                   'enable_thinking', 'context_window_limit', 'inference_component_name', 'profile_name'}
        if kwargs.keys() - allowed:
            raise ValueError(f'Unknown model configuration: {sorted(kwargs.keys() - allowed)}')
        config = {**self._config, **kwargs}
        for key in ('endpoint_name', 'model_id'):
            _name(config[key], key)
        for key in ('region_name', 'inference_component_name', 'profile_name'):
            if config.get(key) is not None:
                _name(config[key], key)
        for key in ('max_tokens', 'context_window_limit'):
            if type(config[key]) is not int or config[key] <= 0:
                raise ValueError(f'{key} must be a positive integer')
        temperature = config['temperature']
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or temperature < 0:
            raise ValueError('temperature must be finite and nonnegative')
        if type(config['enable_thinking']) is not bool:
            raise ValueError('enable_thinking must be a boolean')
        if not self._injected_client and any(config.get(key) != self._config.get(key)
                                             for key in ('region_name', 'profile_name')):
            self._client = None
        self._config = config

    def _invoke(self, request, config):
        with self._client_lock:
            if self._client is None:
                import boto3
                from botocore.config import Config
                session = boto3.Session(profile_name=config['profile_name'], region_name=config['region_name'])
                self._client = session.client('sagemaker-runtime', config=Config(
                    read_timeout=70, connect_timeout=10, retries={'total_max_attempts': 1}))
            client = self._client
        response = client.invoke_endpoint(**request)
        body = response['Body']
        try:
            return _loads(body.read())
        finally:
            body.close()

    async def stream(self, messages, tool_specs=None, system_prompt=None, *,
                     tool_choice=None, system_prompt_content=None, **kwargs):
        self._provider_call_sequence += 1
        self._last_provider_usage = None
        config = self.get_config()
        payload = {'model': config['model_id'],
                   'messages': _messages(messages, system_prompt, system_prompt_content),
                   'max_tokens': config['max_tokens'], 'temperature': config['temperature'], 'stream': False,
                   'chat_template_kwargs': {'enable_thinking': config['enable_thinking']},
                   **_tools(tool_specs, tool_choice)}
        request = {'EndpointName': config['endpoint_name'], 'ContentType': 'application/json',
                   'Accept': 'application/json', 'Body': _json(payload).encode('utf-8')}
        if config['inference_component_name'] is not None:
            request['InferenceComponentName'] = config['inference_component_name']
        started = perf_counter()
        response = await asyncio.to_thread(self._invoke, request, config)
        usage = _usage(response)
        self._last_provider_usage = copy.deepcopy(usage)
        blocks, reason = _response(response)
        yield {'messageStart': {'role': 'assistant'}}
        for start, delta in blocks:
            yield {'contentBlockStart': {'start': start}}
            yield {'contentBlockDelta': {'delta': delta}}
            yield {'contentBlockStop': {}}
        yield {'messageStop': {'stopReason': reason}}
        metadata = {'metrics': {'latencyMs': int((perf_counter() - started) * 1000)}}
        if usage is not None:
            metadata['usage'] = usage
        yield {'metadata': metadata}

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError('SageMakerModel.structured_output is not supported; use text and tools')
        yield  # Keep the Strands async-generator interface.
