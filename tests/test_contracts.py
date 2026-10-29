from scripts.generate_contracts import OUTPUT, generate


def test_generated_contracts_match_openapi():
    assert OUTPUT.read_text(encoding='utf-8') == generate()


def test_query_roundtrip_preserves_full_filters(lab):
    client, _, _, _ = lab
    filters = {'shipping_city':'上海', 'status':'paid', 'waiting_hours_gt':72, 'amount_gte':'0.00', 'amount_lte':'1000.00',
               'paid_at_gte':'2026-10-19T00:00:00+08:00', 'paid_at_lt':'2026-10-26T00:00:00+08:00'}
    first = client.post('/api/orders/query',json={'filters':filters, 'expected_context_version':0}).json()
    current = client.get('/api/session').json()['current_result']
    assert current['query']['filters'] == first['query']['filters']
    assert current['query']['filters']['waiting_hours_gt'] == 72
    again = client.post('/api/orders/query',json={**current['query'], 'expected_context_version':current['context_version']}).json()
    assert [r['id'] for r in first['rows']] == [r['id'] for r in again['rows']]
    assert again['query']['filters'] == current['query']['filters']
