from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.app.models import Customer, Order, OrderItem, Product
from conftest import confirm_apply, make_preview, query


def test_advanced_city_status_customer_product_filters(lab):
    client,_,_,_ = lab
    result = query(client,{'shipping_cities':['上海','北京'],'statuses':['paid','shipped'],'customer_name':'合成客户001',
                           'product_keyword':'商品01','product_category':'数码'})
    assert [r['id'] for r in result['rows']] == [1,2,3,4,5,9,10,12]
    assert result['total'] == 8
    assert query(client,{'order_id':1})['rows'][0]['id'] == 1
    assert query(client,{'product_keyword':'%_wildcard'})['total'] == 0


def test_product_filters_bind_to_same_item_and_never_duplicate_orders(lab):
    client,factory,_,_ = lab
    with factory.begin() as db:
        db.add(Product(id=1000,tenant_id='a',name='特殊办公用品',category='办公'))
        db.flush()
        db.add(OrderItem(tenant_id='a',order_id=1,product_id=1000,quantity=1,unit_price=Decimal('0.00')))
    assert query(client,{'product_keyword':'商品01','product_category':'办公'})['total'] == 0
    assert [r['id'] for r in query(client,{'product_keyword':'特殊','product_category':'办公'})['rows']] == [1]
    assert query(client,{'product_keyword':'商品01'})['total'] == 12


def test_cross_tenant_customer_or_item_metadata_is_not_exposed(lab):
    client,factory,_,_ = lab
    with factory.begin() as db:
        db.get(Customer,2).name = '外部秘密客户'
        db.get(Order,1).customer_id = 2
        db.get(Product,2).name = '外部秘密商品'
        for item in db.scalars(select(OrderItem).where(OrderItem.order_id == 1)):
            item.product_id = 2
    assert query(client,{'customer_name':'外部秘密客户'})['total'] == 0
    assert query(client,{'product_keyword':'外部秘密商品'})['total'] == 0
    detail = client.get('/api/orders/1/full').json()
    assert detail['customer'] is None and detail['items'] == []


def test_full_detail_contains_real_lines_and_shipment(lab):
    client,_,_,_ = lab
    detail = client.get('/api/orders/1/full').json()
    assert detail['customer']['name'] == '合成客户001'
    assert len(detail['items']) == 2
    assert sum(Decimal(item['line_total']) for item in detail['items']) == Decimal('300.00')
    assert detail['shipment'] is None
    shipped = client.get('/api/orders/5/full').json()
    assert shipped['shipment']['tracking_no'] == 'SYNTH000005'
    assert client.get('/api/orders/13/full').status_code == 404


def test_analytics_exact_amounts_boundaries_and_local_day(lab):
    client,_,_,_ = lab
    data = client.post('/api/analytics/orders',json={}).json()
    assert data['metrics'] == {'order_count':12,'total_amount':'4650.00','average_amount':'387.50','paid_amount':'4520.00',
                              'pending_amount':'80.00','pending_shipment_count':8,'overdue_48h_count':5,'priority_count':0,'review_count':0}
    assert [item['count'] for item in data['waiting_buckets']] == [2,1,2,3]
    assert data['cities'][0] == {'group':'上海','count':8,'total_amount':'3350.00'}
    assert next(item for item in data['trend'] if item['date'] == '2026-10-19') == {'date':'2026-10-19','count':3,'total_amount':'1800.00'}
    assert sum(item['count'] for item in data['trend']) == 11


def test_analytics_respects_result_scope_and_empty_filters(lab):
    client,_,_,_ = lab
    result = query(client,{'shipping_city':'北京'})
    data = client.post('/api/analytics/orders',json={'result_set_id':result['result_set_id']}).json()
    assert data['scope'] == 'result_set' and data['metrics']['order_count'] == 1
    assert data['metrics']['total_amount'] == '120.00'
    empty = client.post('/api/analytics/orders',json={'filters':{'shipping_city':'广州'}}).json()
    assert empty['metrics']['order_count'] == 0 and empty['metrics']['total_amount'] == '0.00'
    assert all(item['count'] == 0 for item in empty['trend'])


def save_view(client,result,name='上海待发货'):
    version = client.get('/api/session').json()['context_version']
    return client.post('/api/saved-views',json={'name':name,'result_set_id':result['result_set_id'],'expected_context_version':version})


def test_saved_query_survives_login_and_reruns_latest_data(lab):
    client,factory,_,_ = lab
    result = query(client,{'shipping_city':'上海','status':'paid'},page_size=2)
    view = save_view(client,result).json()
    assert view['query']['page'] == 1
    with factory.begin() as db:
        db.get(Order,2).status = 'cancelled'
        db.get(Order,2).version += 1
    client.post('/api/auth/demo',json={'actor':'operator_a'})
    assert client.get('/api/saved-views').json()[0]['id'] == view['id']
    applied = client.post(f"/api/saved-views/{view['id']}/apply",json={'expected_context_version':0}).json()
    assert applied['total'] == 5 and applied['query']['page_size'] == 2
    assert [r['id'] for r in applied['rows']] == [1,3]


@pytest.mark.parametrize('actor',['viewer_a','operator_b'])
def test_saved_views_are_private_to_actor_and_tenant(lab,actor):
    client,_,_,_ = lab
    view = save_view(client,query(client)).json()
    client.post('/api/auth/demo',json={'actor':actor})
    assert client.get('/api/saved-views').json() == []
    assert client.post(f"/api/saved-views/{view['id']}/apply",json={'expected_context_version':0}).status_code == 404
    assert client.post(f"/api/saved-views/{view['id']}/delete",json={'expected_version':1}).status_code == 404


def test_view_rename_duplicates_and_version_checked_delete(lab):
    client,_,_,_ = lab
    result = query(client)
    view = save_view(client,result).json()
    assert save_view(client,result).json()['code'] == 'VIEW_NAME_EXISTS'
    changed = client.post(f"/api/saved-views/{view['id']}/rename",json={'name':'我的查询','expected_version':1}).json()
    assert changed['version'] == 2
    assert client.post(f"/api/saved-views/{view['id']}/delete",json={'expected_version':1}).status_code == 409
    assert client.post(f"/api/saved-views/{view['id']}/delete",json={'expected_version':2}).json()['deleted']
    assert client.get('/api/saved-views').json() == []


def test_readonly_actor_can_save_but_cannot_modify_orders(lab):
    client,_,_,_ = lab
    client.post('/api/auth/demo',json={'actor':'viewer_a'})
    result = query(client,{'status':'paid'})
    assert save_view(client,result).status_code == 200
    assert make_preview(client,result).status_code == 403


def test_audit_contains_actual_changes_and_is_tenant_scoped(lab):
    client,_,_,_ = lab
    result = query(client,{'status':'paid','shipping_city':'上海'})
    preview = make_preview(client,result).json()
    assert confirm_apply(client,preview).status_code == 200
    audit = client.post('/api/audit/query',json={'order_id':1}).json()
    assert audit['total'] == 1
    assert audit['rows'][0]['before'] == {'priority':False} and audit['rows'][0]['after'] == {'priority':True}
    assert client.get('/api/orders/1/full').json()['audit']['total'] == 1
    client.post('/api/auth/demo',json={'actor':'viewer_a'})
    assert client.post('/api/audit/query',json={}).json()['total'] == 6
    client.post('/api/auth/demo',json={'actor':'operator_b'})
    assert client.post('/api/audit/query',json={}).json()['total'] == 0


def test_audit_pagination_does_not_repeat_or_skip_rows(lab):
    client,_,_,_ = lab
    assert confirm_apply(client,make_preview(client,query(client,{'status':'paid'})).json()).status_code == 200
    pages=[client.post('/api/audit/query',json={'page':page,'page_size':3}).json() for page in [1,2,3]]
    assert [page['total'] for page in pages] == [8,8,8]
    ids=[item['id'] for page in pages for item in page['rows']]
    assert len(ids) == len(set(ids)) == 8


def ask(client,message):
    version=client.get('/api/session').json()['context_version']
    return client.post('/api/assistant',json={'message':message,'expected_context_version':version}).json()


def test_assistant_can_open_scoped_analytics_and_audit(lab):
    client,_,_,_ = lab
    query(client,{'shipping_city':'北京'})
    result=ask(client,'查看这些订单统计看板')
    assert result['state']=='SUCCEEDED' and result['analytics']['metrics']['total_amount']=='120.00'
    audit=ask(client,'查看操作审计')
    assert audit['state']=='SUCCEEDED' and audit['audit']['total']==0
    order_audit=ask(client,'查看订单4的修改记录')
    assert order_audit['audit_order_id']==4 and order_audit['audit']['total']==0


def test_assistant_handles_product_customer_and_processing_filters(lab):
    client,_,_,_ = lab
    result=ask(client,'查询客户名称包含合成客户001的商品分类为数码的上海订单')
    assert result['state']=='SUCCEEDED'
    assert [item['id'] for item in result['ui_patch']['rows']]==[1,2,3,5,7,9,10,12]
    result=ask(client,'查询优先处理的订单')
    assert result['state']=='SUCCEEDED' and result['ui_patch']['total']==0


def test_analytics_rejects_another_sessions_result(lab):
    client,_,_,_ = lab
    result=query(client)
    client.post('/api/auth/demo',json={'actor':'operator_b'})
    assert client.post('/api/analytics/orders',json={'result_set_id':result['result_set_id']}).status_code==404
