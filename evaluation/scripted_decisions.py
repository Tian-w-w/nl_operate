"""Explicit test double. Never selected by production configuration."""
from copy import deepcopy

from backend.app.model_adapter import ModelError, ModelReply


class ScriptedDecisions:
    name = 'scripted-test-only'
    billable = False

    def __init__(self, decisions, hook=None):
        self.decisions, self.hook, self.states = decisions, hook, []

    def decide(self, goal, reference, state, timeout_seconds=None):
        index = len(self.states)
        self.states.append(deepcopy(state))
        if self.hook:
            self.hook(index, state)
        if index >= len(self.decisions):
            raise ModelError('SCRIPT_EXHAUSTED', '测试决策序列已用尽。')
        queries = [item['output'] for item in state['observations'] if item['tool'] == 'query_orders' and item['status'] == 'SUCCEEDED']
        exports = [item['output'] for item in state['observations'] if item['tool'] == 'export_orders' and item['status'] == 'SUCCEEDED']
        bindings = {'$result_set': queries[-1]['result_set_id'] if queries else None,
                    '$total': queries[-1]['total'] if queries else None, '$export': exports[-1]['export_id'] if exports else None}
        def resolve(value):
            if isinstance(value, str):
                return bindings.get(value, value)
            if isinstance(value, dict):
                return {key: resolve(item) for key, item in value.items()}
            if isinstance(value, list):
                return [resolve(item) for item in value]
            return value
        return ModelReply({'decision': resolve(deepcopy(self.decisions[index]))}, 0, 0)
