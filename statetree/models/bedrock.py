"""Strands model adapter for Amazon Bedrock's Converse API."""

import asyncio
import copy
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


def _system(system_prompt, system_prompt_content):
    if system_prompt_content is not None:
        if any(not isinstance(block, dict) or set(block) != {'text'} for block in system_prompt_content):
            raise ValueError('Only text system prompt blocks are supported')
        system_prompt = '\n'.join(_text(block['text'], 'system prompt') for block in system_prompt_content)
    return [{'text': _text(system_prompt, 'system prompt')}] if system_prompt is not None else None


def _messages(messages):
    converted = []
    for message in messages:
        role = message.get('role')
        if role not in ('user', 'assistant') or not isinstance(message.get('content'), list):
            raise ValueError('Expected user/assistant messages with content blocks')
        content = []
        for block in message['content']:
            if not isinstance(block, dict) or len(block) != 1:
                raise ValueError('Expected one supported content type per block')
            if 'text' in block:
                content.append({'text': _text(block['text'], 'message text')})
            elif 'toolUse' in block and role == 'assistant':
                tool = block['toolUse']
                content.append({'toolUse': {
                    'toolUseId': _name(tool.get('toolUseId'), 'toolUseId'),
                    'name': _name(tool.get('name'), 'tool name'),
                    'input': tool.get('input') if isinstance(tool.get('input'), dict) else _invalid_tool_input(),
                }})
            elif 'toolResult' in block and role == 'user':
                result = block['toolResult']
                identifier = _name(result.get('toolUseId'), 'tool result ID')
                parts = []
                for part in result.get('content', []):
                    if set(part) == {'text'}:
                        parts.append({'text': _text(part['text'], 'tool result text')})
                    elif set(part) == {'json'} and isinstance(part['json'], dict):
                        parts.append({'json': part['json']})
                    else:
                        raise ValueError('Only text and JSON tool results are supported')
                status = result.get('status', 'success')
                if status not in ('success', 'error'):
                    raise ValueError('Unsupported tool result status')
                content.append({'toolResult': {'toolUseId': identifier, 'content': parts, 'status': status}})
            elif 'reasoningContent' in block and role == 'assistant':
                value = block['reasoningContent']
                if set(value) != {'reasoningText'} or set(value['reasoningText']) != {'text'}:
                    raise ValueError('Only plain-text reasoning is supported')
                content.append({'text': _text(value['reasoningText']['text'], 'reasoning text')})
            else:
                raise ValueError('Only text, toolUse, toolResult and text reasoning blocks are supported')
        if not content:
            raise ValueError('Messages need at least one content block')
        if converted and converted[-1]['role'] == role:
            converted[-1]['content'].extend(content)
        else:
            converted.append({'role': role, 'content': content})
    return converted


def _invalid_tool_input():
    raise ValueError('Tool arguments must be a JSON object')


def _tools(specs, choice):
    tools, names = [], set()
    for spec in specs or []:
        name = _name(spec.get('name'), 'tool name')
        schema = spec.get('inputSchema', {}).get('json')
        if name in names or not isinstance(schema, dict):
            raise ValueError('Tools need unique names and JSON object schemas')
        names.add(name)
        tools.append({'toolSpec': {
            'name': name,
            'description': _text(spec.get('description', ''), 'tool description'),
            'inputSchema': {'json': schema},
        }})
    if not tools:
        if choice is not None:
            raise ValueError('tool_choice requires tools')
        return None
    config = {'tools': tools}
    if choice == {'auto': {}}:
        config['toolChoice'] = {'auto': {}}
    elif choice == {'any': {}}:
        config['toolChoice'] = {'any': {}}
    elif choice is not None and set(choice) == {'tool'} and choice['tool'].get('name') in names:
        config['toolChoice'] = {'tool': {'name': choice['tool']['name']}}
    elif choice is not None:
        raise ValueError('Unsupported or unregistered tool_choice')
    return config


def _response(value):
    message = value.get('output', {}).get('message') if isinstance(value, dict) else None
    if not isinstance(message, dict) or message.get('role') != 'assistant' or not isinstance(message.get('content'), list):
        raise ValueError('Expected a Bedrock assistant response')
    blocks, tool_ids = [], set()
    for part in message['content']:
        if set(part) == {'text'}:
            blocks.append(({}, {'text': _text(part['text'], 'response text')}))
        elif set(part) == {'toolUse'}:
            tool = part['toolUse']
            identifier = _name(tool.get('toolUseId'), 'toolUseId')
            if identifier in tool_ids or not isinstance(tool.get('input'), dict):
                raise ValueError('Invalid Bedrock tool call')
            tool_ids.add(identifier)
            blocks.append(({'toolUse': {'toolUseId': identifier, 'name': _name(tool.get('name'), 'tool name')}},
                           {'toolUse': {'input': tool['input']}}))
        else:
            raise ValueError('Only Bedrock text and toolUse response blocks are supported')
    reason = value.get('stopReason')
    reasons = {'end_turn': 'end_turn', 'max_tokens': 'max_tokens', 'tool_use': 'tool_use'}
    if reason not in reasons or bool(tool_ids) != (reason == 'tool_use') or not blocks:
        raise ValueError('Unsupported or inconsistent Bedrock stop reason')
    return blocks, reasons[reason]


def _usage(value):
    usage = value.get('usage') if isinstance(value, dict) else None
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ValueError('usage must be an object')
    fields = ('inputTokens', 'outputTokens', 'totalTokens', 'cacheReadInputTokens', 'cacheWriteInputTokens')
    result = {field: usage[field] for field in fields if field in usage}
    if any(type(count) is not int or count < 0 for count in result.values()):
        raise ValueError('Usage counts must be nonnegative integers')
    return result


class BedrockModel(Model):
    """Buffered Bedrock Converse transport with native tool-use support."""

    def __init__(self, model_id, *, region_name=None, max_tokens=512, temperature=0.7,
                 client=None, context_window_limit=8192, profile_name=None):
        self._config = {}
        self._client, self._injected_client = client, client is not None
        self._client_lock = Lock()
        self._last_provider_usage = None
        self._provider_call_sequence = 0
        self.update_config(model_id=model_id, region_name=region_name, max_tokens=max_tokens,
                           temperature=temperature, context_window_limit=context_window_limit,
                           profile_name=profile_name)

    @property
    def last_provider_usage(self):
        return copy.deepcopy(self._last_provider_usage)

    @property
    def provider_call_sequence(self):
        return self._provider_call_sequence

    def get_config(self):
        return copy.deepcopy(self._config)

    def update_config(self, **kwargs):
        allowed = {'model_id', 'region_name', 'max_tokens', 'temperature', 'context_window_limit', 'profile_name'}
        if kwargs.keys() - allowed:
            raise ValueError(f'Unknown model configuration: {sorted(kwargs.keys() - allowed)}')
        config = {**self._config, **kwargs}
        _name(config['model_id'], 'model_id')
        for key in ('region_name', 'profile_name'):
            if config.get(key) is not None:
                _name(config[key], key)
        for key in ('max_tokens', 'context_window_limit'):
            if type(config[key]) is not int or config[key] <= 0:
                raise ValueError(f'{key} must be a positive integer')
        if type(config['temperature']) not in (int, float) or not math.isfinite(config['temperature']) or config['temperature'] < 0:
            raise ValueError('temperature must be finite and nonnegative')
        if not self._injected_client and any(config.get(key) != self._config.get(key) for key in ('region_name', 'profile_name')):
            self._client = None
        self._config = config

    def _invoke(self, request, config):
        with self._client_lock:
            if self._client is None:
                import boto3
                from botocore.config import Config
                session = boto3.Session(profile_name=config['profile_name'], region_name=config['region_name'])
                self._client = session.client('bedrock-runtime', config=Config(
                    read_timeout=70, connect_timeout=10, retries={'total_max_attempts': 1}))
            client = self._client
        return client.converse(**request)

    async def stream(self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None,
                     system_prompt_content=None, **kwargs):
        self._provider_call_sequence += 1
        self._last_provider_usage = None
        config = self.get_config()
        request = {
            'modelId': config['model_id'],
            'messages': _messages(messages),
            'inferenceConfig': {'maxTokens': config['max_tokens'], 'temperature': config['temperature']},
        }
        system = _system(system_prompt, system_prompt_content)
        if system is not None:
            request['system'] = system
        tool_config = _tools(tool_specs, tool_choice)
        if tool_config is not None:
            request['toolConfig'] = tool_config
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
        raise NotImplementedError('BedrockModel.structured_output is not supported; use text and tools')
        yield