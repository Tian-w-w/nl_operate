from dataclasses import replace
import json

import httpx
import pytest

from backend.app.agent import RulesAdapter
from backend.app.agent_runtime import drive_run, get_run, start_run
from backend.app.conversation import context_for
from backend.app.knowledge import chunks, retrieve
from backend.app.model_adapter import LocalModelAdapter, ModelReply, OpenAIAdapter
from backend.app.models import Session
from backend.app.service import BusinessError

FIRST = '查询上海等待发货超过48小时的订单，按金额降序，只展示列表和数量，不要导出。'
FOLLOWUP = '把已发货的也一并查出来'


def tell(client, message):
    version = client.get('/api/session').json()['context_version']
    response = client.post('/api/assistant', json={'message': message, 'expected_context_version': version})
    assert response.status_code == 200, response.text
    return response.json()


def current(client):
    return client.get('/api/session').json()['current_result']


def test_status_addition_preserves_city_sort_and_overdue_branch(lab):
    client = lab[0]
    first = tell(client, FIRST)
    assert first['state'] == 'SUCCEEDED' and first['ui_patch']['total'] == 4
    result = tell(client, FOLLOWUP)
    assert result['state'] == 'SUCCEEDED'
    data = current(client)
    assert [row['id'] for row in data['rows']] == [12, 3, 5, 1, 10]
    assert data['query']['filters']['shipping_city'] == '上海'
    assert data['query']['filters']['waiting_hours_gt'] == 48
    assert data['query']['filters']['additional_statuses'] == ['shipped']
    assert data['query']['sort'][0] == {'field': 'total_amount', 'direction': 'desc'}
    assert result.get('export') is None


def test_addition_keeps_amount_and_replacement_clears_incompatible_wait(lab):
    client = lab[0]
    tell(client, '查询上海等待发货超过48小时且金额不低于500元的订单，按金额降序')
    tell(client, FOLLOWUP)
    assert [row['id'] for row in current(client)['rows']] == [12, 3, 5]
    tell(client, '那只看已发货的')
    data = current(client)
    assert [row['id'] for row in data['rows']] == [5]
    assert data['query']['filters']['shipping_city'] == '上海'
    assert data['query']['filters']['waiting_hours_gt'] is None
    assert data['query']['filters']['amount_gte'] == '500'


def test_addition_without_previous_query_requests_a_scope(lab):
    answer = tell(lab[0], FOLLOWUP)
    assert answer['state'] == 'FAILED' and answer['error'] == 'MISSING_RESULT'
    assert 'current_result' not in lab[0].get('/api/session').json()


def test_model_gets_persisted_history_and_query_after_refresh(lab):
    client, factory, _, _ = lab
    tell(client, FIRST)
    captured = []
    class ContextAdapter(RulesAdapter):
        def parse(self, instruction, reference, context=None):
            captured.append(context)
            # A model proposing only shipped, without the city, cannot erase it on an explicit addition.
            return {'intent': 'query', 'filters': {'status': 'shipped'}}
    client.app.state.adapter = ContextAdapter()
    history = client.get('/api/conversation').json()
    assert history[0]['text'] == FIRST
    tell(client, FOLLOWUP)
    assert captured[0]['history'][-1]['user'] == FIRST
    assert captured[0]['query']['filters']['shipping_city'] == '上海'
    assert len(current(client)['rows']) == 5
    with factory() as db:
        context = context_for(db, db.get(Session, client.cookies.get('llm4web_session')), '继续')
        assert '忽略用户指令' not in json.dumps(context, ensure_ascii=False)
    client.post('/api/auth/demo', json={'actor': 'operator_b'})
    assert client.get('/api/conversation').json() == []


def test_model_revises_one_condition_without_losing_the_others(lab):
    client = lab[0]
    tell(client, FIRST)
    class ReviseAdapter(RulesAdapter):
        def parse(self, *args):
            return {'intent': 'query', 'context_mode': 'revise', 'filters': {'status': 'shipped'}}
    client.app.state.adapter = ReviseAdapter()
    tell(client, '改成已发货')
    data = current(client)
    assert [row['id'] for row in data['rows']] == [5]
    assert data['query']['filters']['shipping_city'] == '上海'
    assert data['query']['sort'][0]['direction'] == 'desc'


@pytest.mark.parametrize('question', ['统计一下一共有多少个收货城市', '统计一下一共有多少个收获城市', '收货城市的数量是多少'])
def test_distinct_city_count_is_not_an_order_query(lab, question):
    client = lab[0]
    tell(client, FIRST)
    before = client.get('/api/session').json()
    result = tell(client, question)
    assert result['state'] == 'SUCCEEDED' and result['statistic']['distinct_count'] == 4
    assert result['statistic']['order_count'] == 12
    assert '4个不同的收货城市' in result['message']
    assert result.get('ui_patch') is None
    after = client.get('/api/session').json()
    assert after['context_version'] == before['context_version']
    assert after['current_result'] == before['current_result']


def test_count_current_and_empty_results_and_read_only_role(lab):
    client = lab[0]
    client.post('/api/auth/demo', json={'actor': 'viewer_a'})
    tell(client, FIRST)
    result = tell(client, '这些订单有几个收货城市')
    assert result['statistic']['scope'] == 'current' and result['statistic']['distinct_count'] == 1
    tell(client, '查询广州订单')
    result = tell(client, '当前结果有多少个收货城市')
    assert result['statistic']['distinct_count'] == 0 and result['statistic']['order_count'] == 0


def test_clear_count_question_corrects_model_query_intent_without_changing_page(lab):
    client = lab[0]
    tell(client, FIRST)
    class WrongIntent(RulesAdapter):
        def parse(self, *args):
            return {'intent': 'query', 'filters': {'shipping_city': '上海'}}
    client.app.state.adapter = WrongIntent()
    result = tell(client, '统计一下一共有多少个收获城市')
    assert result['statistic']['distinct_count'] == 4
    assert current(client)['total'] == 4


def test_city_count_keeps_explicit_date_conditions(lab):
    client = lab[0]
    class DateCountAdapter(RulesAdapter):
        def parse(self, *args):
            return {'intent': 'query', 'filters': {'created_at_gte': '2026-10-25T00:00:00+08:00'}}
    client.app.state.adapter = DateCountAdapter()
    result = tell(client, '统计2026年10月25日及以后的订单有多少个收货城市')
    assert result['statistic']['distinct_count'] == 2 and result['statistic']['order_count'] == 3


@pytest.mark.parametrize('question,fact', [('规定的发货时间是多少', '48小时'), ('默认发什么快递', '顺丰速运')])
def test_offline_knowledge_has_demo_sources_and_no_order_effects(lab, question, fact):
    client = lab[0]
    before = client.get('/api/session').json()['context_version']
    result = tell(client, question)
    assert result['state'] == 'SUCCEEDED' and fact in result['message']
    assert '演示' in result['message']
    assert result['knowledge']['mode'] == 'retrieval_only'
    assert result['knowledge']['sources'] and all(item['version'] for item in result['knowledge']['sources'])
    assert client.get('/api/session').json()['context_version'] == before
    assert client.get('/api/conversation').json()[-1]['knowledge'] == result['knowledge']


def test_unknown_knowledge_does_not_make_up_a_policy(lab):
    tell(lab[0], '默认发什么快递')
    result = tell(lab[0], '公司的退货政策是什么')
    assert result['knowledge']['mode'] == 'no_evidence' and not result['knowledge']['grounded']
    assert not result['knowledge']['sources']
    assert '没有足够依据' in result['message']


@pytest.mark.parametrize('valid', [True, False])
def test_model_answer_requires_retrieved_citations(lab, valid):
    client = lab[0]
    class GroundedAdapter(RulesAdapter):
        name = 'model-knowledge-test'
        def parse(self, instruction, reference, context=None):
            assert context['knowledge']
            return {'intent': 'knowledge', 'answer': '普通现货订单付款后48小时内发货。',
                    'citation_ids': [context['knowledge'][0]['source_id'] if valid else 'invented']}
    client.app.state.adapter = GroundedAdapter()
    result = tell(client, '发货时限是什么')
    if valid:
        assert result['knowledge']['mode'] == 'retrieval_augmented_generation'
        assert result['knowledge']['sources'] and '演示' in result['message']
    else:
        assert result['state'] == 'FAILED' and result['error'] == 'KNOWLEDGE_CITATION_INVALID'


def test_knowledge_documents_are_tenant_scoped_versioned_and_path_guarded(tmp_path):
    (tmp_path/'a.md').write_text('# A\n\n## 发货时限\n\n租户A付款后48小时发货。', encoding='utf-8')
    (tmp_path/'b.md').write_text('# B\n\n## 私有资料\n\n租户B秘密承运商：专属快递。', encoding='utf-8')
    docs = [{'id': 'a', 'file': 'a.md', 'title': 'A规则', 'tenants': ['a'], 'is_demo': False},
            {'id': 'b', 'file': 'b.md', 'title': 'B规则', 'tenants': ['b'], 'is_demo': False}]
    manifest = tmp_path/'manifest.json'
    manifest.write_text(json.dumps({'documents': docs}), encoding='utf-8')
    visible = retrieve('发货快递', 'a', tmp_path)
    assert visible and all(item['title'] == 'A规则' for item in visible)
    assert '专属快递' not in json.dumps(visible, ensure_ascii=False)
    first = chunks('a', tmp_path)[0]
    (tmp_path/'a.md').write_text('# A\n\n## 发货时限\n\n租户A付款后72小时发货。', encoding='utf-8')
    assert chunks('a', tmp_path)[0]['version'] != first['version']
    docs[0]['file'] = '../outside.md'
    manifest.write_text(json.dumps({'documents': docs}), encoding='utf-8')
    with pytest.raises(BusinessError, match='知识库'):
        chunks('a', tmp_path)


def run_goal(lab, goal, decide):
    client, factory, settings, _ = lab
    class Adapter:
        name = 'scripted-extended-test'
        billable = False
        def decide(self, goal, reference, state, timeout_seconds=None):
            return ModelReply({'decision': decide(state)}, 0, 0)
    token = client.cookies.get('llm4web_session')
    version = client.get('/api/session').json()['context_version']
    adapter = Adapter()
    run = start_run(factory, token, goal, version, settings, adapter)
    drive_run(factory, token, run['id'], settings, adapter, 'extended-test')
    return get_run(factory, token, run['id'])


def test_agent_receives_previous_turn_and_expands_only_status(lab):
    tell(lab[0], FIRST)
    def decide(state):
        if not state['observations']:
            assert state['conversation']['query']['filters']['shipping_city'] == '上海'
            assert state['conversation']['history'][-1]['user'] == FIRST
            return {'kind': 'call_tool', 'tool': 'query_orders', 'arguments': {'filters': {'status': 'shipped'}}}
        output = state['observations'][-1]['output']
        return {'kind': 'finish', 'result_set_id': output['result_set_id'], 'total': output['total'], 'export_id': None}
    final = run_goal(lab, FOLLOWUP, decide)
    assert final['state'] == 'SUCCEEDED' and final['tool_calls'] == 1
    assert [row['id'] for row in current(lab[0])['rows']] == [12, 3, 5, 1, 10]


@pytest.mark.parametrize('correct_id', [True, False])
def test_agent_city_count_uses_real_observation_and_does_not_query_orders(lab, correct_id):
    tell(lab[0], FIRST)
    before = current(lab[0])
    def decide(state):
        if not state['observations']:
            # The explicit count intent guard corrects a valid but misclassified query.
            return {'kind': 'call_tool', 'tool': 'query_orders', 'arguments': {}}
        output = state['observations'][-1]['output']
        return {'kind': 'finish_statistic', 'statistic_id': output['statistic_id'] if correct_id else 'invented'}
    result = run_goal(lab, '统计一下一共有多少个收获城市', decide)
    assert current(lab[0]) == before
    if correct_id:
        assert result['state'] == 'SUCCEEDED' and result['statistic']['distinct_count'] == 4
        assert result['steps'][0]['observation']['tool'] == 'count_order_values'
    else:
        assert result['state'] == 'FAILED' and result['error'] == 'COMPLETION_EVIDENCE'


def test_agent_rag_generates_with_retrieved_evidence(lab):
    def decide(state):
        source = state['conversation']['knowledge'][0]
        return {'kind': 'answer_knowledge', 'answer': '普通订单默认使用顺丰速运。', 'citation_ids': [source['source_id']]}
    final = run_goal(lab, '默认发什么快递', decide)
    assert final['state'] == 'SUCCEEDED' and final['knowledge']['sources']
    assert final['model_calls'] == 1 and final['tool_calls'] == 0
    assert '演示' in final['message']


def test_knowledge_question_cannot_execute_an_order_operation(lab):
    result = run_goal(lab, '默认发什么快递', lambda _: {'kind': 'call_tool', 'tool': 'query_orders', 'arguments': {}})
    assert result['state'] == 'FAILED' and result['error'] == 'KNOWLEDGE_DECISION_REQUIRED'
    assert result['tool_calls'] == 0


def test_query_followup_cannot_inherit_an_unrequested_export(lab):
    tell(lab[0], FIRST)
    def decide(state):
        if not state['observations']:
            return {'kind': 'call_tool', 'tool': 'query_orders', 'arguments': {'filters': {'status': 'shipped'}}}
        return {'kind': 'call_tool', 'tool': 'export_orders', 'arguments': {'result_set_id': state['observations'][-1]['output']['result_set_id']}}
    result = run_goal(lab, FOLLOWUP, decide)
    assert result['state'] == 'FAILED' and result['error'] == 'UNREQUESTED_EXPORT'
    assert result['tool_calls'] == 1 and result['export'] is None


def test_model_payload_stays_bounded_with_context_and_new_decisions(lab):
    client, factory, settings, _ = lab
    for _ in range(8):
        tell(client, '一共有多少个收货城市')
    with factory() as db:
        context = context_for(db, db.get(Session, client.cookies.get('llm4web_session')), '默认发什么快递')
    assert len(context['history']) == 6
    adapter = OpenAIAdapter(replace(settings, model_name='test-only'))
    assert len(json.dumps(adapter.decision_payload('默认发什么快递', settings.reference, {'conversation': context})).encode()) < 50000


def test_compatible_model_request_contains_retrieved_evidence_and_restores_usage(lab):
    client, _, settings, _ = lab
    captured = []
    def handler(request):
        body = json.loads(request.content)
        context = json.loads(body['messages'][1]['content'])['context']
        captured.append(context)
        references = context['knowledge']
        action = {'intent': 'knowledge', 'answer': '默认使用顺丰速运。', 'citation_ids': [references[0]['source_id']]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [
            {'function': {'name': 'propose_action', 'arguments': json.dumps(action)}}]}}],
            'usage': {'prompt_tokens': 200, 'completion_tokens': 30}})
    remote = replace(settings, model_adapter='local', model_enabled=True, model_name='mock-only',
                     local_model_endpoint_url='https://gateway.example.com/v3/openai/model')
    client.app.state.adapter = LocalModelAdapter(remote, httpx.MockTransport(handler))
    result = tell(client, '默认发什么快递')
    assert result['knowledge']['mode'] == 'retrieval_augmented_generation'
    assert 'knowledge' in captured[0] and captured[0]['knowledge']
    restored = client.get('/api/conversation').json()[-1]
    assert restored['usage']['real_model'] is True and restored['usage']['input_tokens'] == 200
    assert restored['knowledge'] == result['knowledge']
