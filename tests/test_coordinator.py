from datetime import timedelta
from decimal import Decimal
import json

import httpx
import pytest
from sqlalchemy import select

from backend.app.agent import RulesAdapter
from backend.app.config import Settings, now
from backend.app.model_adapter import ModelError, OpenAIAdapter
from backend.app.models import ModelCall, Order, Plan, Session


def chat(client, message='找出上海已付款订单', version=None):
    if version is None:
        version = client.get('/api/session').json()['context_version']
    return client.post('/api/assistant',json={'message':message,'expected_context_version':version})


def test_parser_runs_after_committing_intent_and_releasing_connection(lab):
    client, factory, _, _ = lab
    token = client.cookies.get('llm4web_session')
    class ChangingAdapter(RulesAdapter):
        def parse(self, instruction, reference, context=None):
            with factory.begin() as db:
                # An independent transaction sees the plan and can modify the same session.
                assert db.scalar(select(Plan)).state == 'RECEIVED'
                db.get(Session,token).context_version += 1
            return super().parse(instruction,reference,context)
    client.app.state.adapter = ChangingAdapter()
    result = chat(client).json()
    assert result['state'] == 'FAILED' and result['error'] == 'STALE_CONTEXT'
    assert 'current_result' not in client.get('/api/session').json()
    with factory() as db:
        assert db.get(Plan,result['plan_id']).state == 'FAILED'
        assert all(o.version == 1 for o in db.scalars(select(Order)))


def test_invalid_proposal_cannot_add_identity_or_confirmation(lab):
    client, factory, _, _ = lab
    class Invalid(RulesAdapter):
        def parse(self,*args):
            return {'intent':'query','tenant_id':'b','confirmed':True}
    client.app.state.adapter = Invalid()
    result = chat(client).json()
    assert result['error'] == 'MODEL_INVALID_ACTION'
    assert 'current_result' not in client.get('/api/session').json()
    with factory() as db:
        assert db.scalar(select(ModelCall)).error_code == 'MODEL_INVALID_ACTION'


@pytest.mark.parametrize('error',[ModelError('MODEL_TIMEOUT','模型超时'),RuntimeError('private-provider-error')])
def test_parser_failure_is_persisted_without_business_write(lab,error):
    client, factory, _, _ = lab
    class Failing(RulesAdapter):
        def parse(self,*args):
            raise error
    client.app.state.adapter = Failing()
    response = chat(client)
    result = response.json()
    assert result['state'] == 'FAILED'
    assert 'private-provider-error' not in response.text
    assert response.headers['X-Request-ID']
    with factory() as db:
        assert db.get(Plan,result['plan_id']).result['error'] in {'MODEL_TIMEOUT','PARSER_ERROR'}
        assert all(o.version == 1 for o in db.scalars(select(Order)))


def test_interrupted_attempt_expires_and_does_not_execute(lab):
    client, factory, _, _ = lab
    token = client.cookies.get('llm4web_session')
    with factory.begin() as db:
        plan = Plan(session_id=token,instruction='old',state='RECEIVED',intent='unparsed',payload={'trace':['RECEIVED']})
        db.add(plan)
        db.flush()
        plan_id = plan.id
        db.add(ModelCall(plan_id=plan_id,session_id=token,request_id='interrupted',adapter='fake',billable=False,
                         state='RUNNING',reserved_cost=Decimal(0),prompt_hash='a'*64,expires_at=now()-timedelta(seconds=1)))
    assert client.get('/api/session').status_code == 200
    with factory() as db:
        assert db.get(Plan,plan_id).state == 'FAILED'
        assert db.scalar(select(ModelCall)).state == 'UNKNOWN'


def model_settings(**overrides):
    return Settings(model_adapter='openai',model_enabled=True,model_name='configured-test-model',model_api_key='test-key-not-real',
                    model_budget_usd=Decimal('1'),model_project_budget_usd=Decimal('2'),model_input_rate=Decimal('1'),
                    model_output_rate=Decimal('2'),**overrides)


def model_response(action, **extra):
    return {'status':'completed','output':[{'type':'function_call','name':'propose_action','arguments':json.dumps(action)}],
            'usage':{'input_tokens':100,'output_tokens':20},**extra}


def test_openai_transport_validates_action_and_records_actual_usage(lab):
    client, factory, settings, _ = lab
    remote_settings = model_settings()
    settings.model_input_rate,settings.model_output_rate = Decimal('1'),Decimal('2')
    settings.model_budget_usd,settings.model_project_budget_usd = Decimal('1'),Decimal('2')
    captured = []
    def handler(request):
        body = json.loads(request.content)
        captured.append(body)
        assert body['store'] is False and body['parallel_tool_calls'] is False
        assert body['tools'][0]['strict'] is True
        assert 'tenant_id' not in body['input'] and 'llm4web_session' not in body['input']
        return httpx.Response(200,json=model_response({'intent':'query','filters':{'shipping_city':'上海','status':'paid'}}))
    client.app.state.adapter = OpenAIAdapter(remote_settings,httpx.MockTransport(handler))
    result = chat(client).json()
    assert result['state'] == 'SUCCEEDED'
    assert len(captured) == 1
    usage = client.get('/api/model-usage').json()
    assert usage['model_calls'] == 1 and usage['unknown_cost_calls'] == 0
    assert Decimal(usage['committed_usd']) == Decimal('.00014')
    assert usage['calls'][0]['input_tokens'] == 100
    assert 'test-key-not-real' not in json.dumps(usage)


def test_global_budget_cannot_be_reset_by_creating_another_session(lab):
    client, _, settings, _ = lab
    class BudgetAdapter(RulesAdapter):
        billable = True
        def estimate_reservation(self,*args):
            return Decimal('.6')
    settings.model_budget_usd,settings.model_project_budget_usd = Decimal('1'),Decimal('1')
    client.app.state.adapter = BudgetAdapter()
    assert chat(client).json()['state'] == 'SUCCEEDED'
    client.post('/api/auth/demo',json={'actor':'operator_b'})
    response = chat(client)
    assert response.status_code == 429 and response.json()['code'] == 'MODEL_BUDGET_EXCEEDED'


@pytest.mark.parametrize('kind',['timeout','rate_limit','incomplete','invalid_json','unregistered_tool'])
def test_remote_failures_are_bounded_and_never_execute(lab,kind):
    client, factory, settings, _ = lab
    settings.model_budget_usd,settings.model_project_budget_usd = Decimal('1'),Decimal('2')
    calls = []
    def handler(request):
        calls.append(1)
        if kind == 'timeout':
            raise httpx.ReadTimeout('private-key',request=request)
        if kind == 'rate_limit':
            return httpx.Response(429,json={'secret':'private-key'})
        if kind == 'invalid_json':
            return httpx.Response(200,text='invalid')
        body = model_response({'intent':'query'})
        if kind == 'incomplete':
            body['status'] = 'incomplete'
        else:
            body['output'][0]['name'] = 'apply_order_update'
        return httpx.Response(200,json=body)
    client.app.state.adapter = OpenAIAdapter(model_settings(),httpx.MockTransport(handler))
    response = chat(client)
    assert response.json()['state'] == 'FAILED' and len(calls) == 1
    assert 'private-key' not in response.text
    with factory() as db:
        assert all(o.version == 1 for o in db.scalars(select(Order)))


def test_settings_read_environment_on_instantiation_and_validate(monkeypatch):
    monkeypatch.setenv('RESULT_TTL_SECONDS','123')
    assert Settings().result_ttl == 123
    assert 'secret-value' not in repr(Settings(model_api_key='secret-value'))
    with pytest.raises(ValueError):
        Settings(model_adapter='openai',model_enabled=True)


def test_openai_disabled_makes_no_network_attempt(lab):
    client, factory, settings, _ = lab
    calls = []
    def unexpected(request):
        calls.append(request)
        raise AssertionError('disabled adapter reached network')
    client.app.state.adapter = OpenAIAdapter(Settings(model_adapter='openai'),httpx.MockTransport(unexpected))
    response = chat(client)
    assert response.status_code == 503 and response.json()['code'] == 'MODEL_DISABLED'
    assert calls == []
    with factory() as db:
        assert list(db.scalars(select(ModelCall))) == []


def test_call_limit_counts_failed_attempts(lab):
    client, _, settings, _ = lab
    class FailPaid(RulesAdapter):
        billable = True
        def estimate_reservation(self,*args):
            return Decimal('.1')
        def parse(self,*args):
            raise ModelError('MODEL_TIMEOUT','timeout')
    settings.model_max_calls = 1
    settings.model_budget_usd = settings.model_project_budget_usd = Decimal('1')
    client.app.state.adapter = FailPaid()
    assert chat(client).json()['error'] == 'MODEL_TIMEOUT'
    assert chat(client).status_code == 429


def test_readiness_checks_actual_schema(lab,tmp_path):
    from fastapi.testclient import TestClient
    from backend.app.db import make_engine
    from backend.app.main import create_app
    client,_,_,_ = lab
    assert client.get('/api/ready').status_code == 200
    settings = Settings(database_url=f"sqlite:///{(tmp_path/'missing.db').as_posix()}")
    engine = make_engine(settings.database_url)
    with TestClient(create_app(settings,engine)) as empty:
        assert empty.get('/api/health').status_code == 200
        assert empty.get('/api/ready').status_code == 503
    engine.dispose()


def test_untrusted_host_cannot_enable_same_origin_writes(lab):
    client,_,_,_ = lab
    response = client.post('/api/auth/demo',json={'actor':'operator_a'},headers={'Host':'untrusted.example','Origin':'http://untrusted.example'})
    assert response.status_code == 400
