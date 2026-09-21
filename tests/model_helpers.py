import copy
from strands.models import Model


class ScriptedModel(Model):
    """Real SDK streaming protocol with deterministic, explicitly simulated usage."""

    def __init__(self, responses=None):
        self.responses = list(responses or ['done'])
        self.requests = []
        self.config = {'model_id': 'offline-scripted', 'max_tokens': 100, 'context_window_limit': 200000}

    def update_config(self, **kwargs):
        self.config.update(kwargs)

    def get_config(self):
        return self.config

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError('Not used in these integration tests')
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.requests.append(copy.deepcopy({'messages': messages, 'system_prompt': system_prompt}))
        response = self.responses.pop(0) if self.responses else 'done'
        if isinstance(response, Exception):
            raise response
        yield {'messageStart': {'role': 'assistant'}}
        if isinstance(response, dict):
            import json
            yield {'contentBlockStart': {'start': {'toolUse': {
                'toolUseId': response['id'], 'name': response['tool'],
            }}}}
            yield {'contentBlockDelta': {'delta': {'toolUse': {'input': json.dumps(response.get('input', {}))}}}}
            stop = 'tool_use'
        else:
            yield {'contentBlockDelta': {'delta': {'text': response}}}
            stop = 'end_turn'
        yield {'contentBlockStop': {}}
        yield {'messageStop': {'stopReason': stop}}
        yield {'metadata': {'usage': {'inputTokens': 100, 'outputTokens': 10, 'totalTokens': 110},
                            'metrics': {'latencyMs': 1}}}
