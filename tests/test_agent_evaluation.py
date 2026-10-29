from copy import deepcopy
import pytest

from backend.app.config import Settings
from evaluation.agent_runner import equivalent_query, evaluate_case
from evaluation.tool_contracts import cases, oracles


@pytest.mark.parametrize('task', cases(), ids=lambda task: task['task_id'])
def test_agent_loop_against_prewritten_labels(task, tmp_path):
    result = evaluate_case(task, oracles()[task['task_id']], tmp_path, Settings(model_adapter='rules', model_enabled=False))
    assert result['passed'], result['checks']
    assert result['mode'] == 'scripted' and result['input_tokens'] == result['output_tokens'] == 0


def test_real_grading_accepts_redundancy_but_rejects_additional_constraints():
    expected = oracles()['branch_01']['query']
    actual = deepcopy(expected)
    actual['filters']['status'] = 'paid'
    actual['sort'].append({'field': 'id', 'direction': 'asc'})
    assert equivalent_query(actual, expected)
    actual['filters']['amount_gte'] = '1000.00'
    assert not equivalent_query(actual, expected)


def test_successful_chat_state_cannot_hide_a_wrong_export_branch(tmp_path, monkeypatch):
    from evaluation import agent_runner
    original = agent_runner.reference_decisions
    def wrong_branch(task, oracle):
        decisions = original(task, oracle)
        return [decisions[0], decisions[-1]]  # Finish without the required export.
    monkeypatch.setattr(agent_runner, 'reference_decisions', wrong_branch)
    task = cases()[0]
    result = evaluate_case(task, oracles()[task['task_id']], tmp_path, Settings(model_adapter='rules', model_enabled=False))
    assert result['run']['state'] == 'SUCCEEDED'
    assert not result['passed'] and not result['checks']['export_count']
    assert result['failure_category'] == 'branch'
