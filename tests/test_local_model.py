from dataclasses import replace
import json

import httpx
import pytest

from backend.app.agent_runtime import available, drive_run, get_run, start_run
from backend.app.config import Settings
from backend.app.model_adapter import LocalModelAdapter, ModelError, create_adapter
from evaluation.agent_runner import run_agent_evaluation
from test_agent_runtime import QUERY


@pytest.mark.parametrize('protocol', ['chat_completions', 'responses'])
@pytest.mark.parametrize('complete_endpoint', [False, True])
def test_local_model_protocol_uses_configured_url_and_separate_key(lab, protocol, complete_endpoint):
    client, factory, settings, _ = lab
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(request)
        payload = json.loads(body['messages'][1]['content'] if protocol == 'chat_completions' else body['input'])
        query = next((item['output'] for item in payload['state']['observations'] if item['tool'] == 'query_orders'), None)
        decision = QUERY if query is None else {'kind': 'finish', 'result_set_id': query['result_set_id'], 'total': query['total'], 'export_id': None}
        args = json.dumps({'decision': decision})
        if protocol == 'chat_completions':
            return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [{'function': {'name': 'decide_next', 'arguments': args}}]}}],
                                            'usage': {'prompt_tokens': 120, 'completion_tokens': 30}})
        return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'function_call', 'name': 'decide_next', 'arguments': args}],
                                        'usage': {'input_tokens': 120, 'output_tokens': 30}})
    settings = replace(settings, model_adapter='local', model_enabled=True, model_name='local-test', local_model_protocol=protocol,
                       local_model_base_url='' if complete_endpoint else 'http://127.0.0.1:9876/v1',
                       local_model_endpoint_url='https://gateway.example.com/v3/openai/model' if complete_endpoint else '',
                       local_model_api_key='local-secret', model_api_key='cloud-secret')
    adapter = LocalModelAdapter(settings, transport=httpx.MockTransport(respond))
    token = client.cookies.get('llm4web_session')
    run = start_run(factory, token, '上海等待超过48小时，只展示', 0, settings, adapter)
    drive_run(factory, token, run['id'], settings, adapter, 'local-mock')
    final = get_run(factory, token, run['id'])
    assert final['state'] == 'SUCCEEDED' and final['input_tokens'] == 240
    suffix = '/v1/chat/completions' if protocol == 'chat_completions' else '/v1/responses'
    expected_url = settings.local_model_endpoint_url if complete_endpoint else 'http://127.0.0.1:9876'+suffix
    assert len(requests) == 2
    assert all(str(request.url) == expected_url and request.headers['authorization'] == 'Bearer local-secret' for request in requests)
    assert 'cloud-secret' not in str(requests) and final['estimated_cost_usd'] == '0'


def test_blank_local_configuration_stays_disabled_and_never_sends(lab):
    settings = replace(lab[2], model_adapter='local', model_enabled=False, local_model_base_url='', local_model_api_key='', model_name='')
    adapter = create_adapter(settings)
    assert not available(adapter, settings)
    with pytest.raises(ModelError, match='未启用'):
        adapter.decide('目标', settings.reference, {})
    assert 'local_model_api_key=' not in repr(settings)


@pytest.mark.parametrize('url', ['', 'file:///secret', 'http://user:pass@localhost/v1', 'https://localhost/v1?key=secret'])
@pytest.mark.parametrize('field', ['local_model_base_url', 'local_model_endpoint_url'])
def test_enabled_local_configuration_rejects_invalid_url(lab, url, field):
    urls = {'local_model_base_url': '', 'local_model_endpoint_url': ''}
    urls[field] = url
    with pytest.raises(ValueError):
        replace(lab[2], model_adapter='local', model_enabled=True, model_name='local', **urls)


def test_complete_endpoint_from_environment_takes_precedence(monkeypatch):
    endpoint = 'https://gateway.example.com/v3/openai/model/'
    monkeypatch.setenv('LOCAL_MODEL_ENDPOINT_URL', endpoint)
    settings = Settings(model_adapter='local', model_enabled=True, model_name='local-test',
                        local_model_base_url='http://127.0.0.1:9876/v1')
    assert create_adapter(settings).endpoint() == endpoint


def test_real_evaluation_does_not_silently_fall_back_to_scripts(monkeypatch, tmp_path):
    monkeypatch.setenv('MODEL_ADAPTER', 'rules')
    monkeypatch.setenv('MODEL_CALLS_ENABLED', 'false')
    with pytest.raises(ValueError, match='未发起请求'):
        run_agent_evaluation(tmp_path/'unused', real=True)
    assert not (tmp_path/'unused').exists()
