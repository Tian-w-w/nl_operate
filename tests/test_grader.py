from copy import deepcopy

from evaluation.grader import grade


def sample():
    oracle = {'ids':[3,1], 'modified_ids':[3], 'patch':{'priority':True}, 'state':'SUCCEEDED', 'preview_ids':[3]}
    before = {'1':{'id':1,'priority':False,'version':1}, '3':{'id':3,'priority':False,'version':1}, '13':{'id':13,'priority':False,'version':1}}
    after = deepcopy(before)
    after['3'] = {'id':3,'priority':True,'version':2}
    return oracle, {'ids':[3,1], 'preview_ids':[3], 'state':'SUCCEEDED', 'before':before, 'after':after}


def test_positive_oracle_and_deliberately_wrong_results():
    oracle, observed = sample()
    assert grade(oracle,observed)['passed']
    for key, value in [('ids',[1,3]), ('ids',[3]), ('ids',[3,1,13]), ('preview_ids',[1]), ('state','FAILED')]:
        wrong = deepcopy(observed)
        wrong[key] = value
        assert not grade(oracle,wrong)['passed']
    wrong = deepcopy(observed)
    wrong['after']['13']['priority'] = True
    assert not grade(oracle,wrong)['checks']['non_target_unchanged']
    wrong = deepcopy(observed)
    wrong['after']['3']['version'] = 3
    assert not grade(oracle,wrong)['checks']['target_patch_exact']


def test_empty_query_cannot_pass_missing_ui_oracle_and_wrong_export():
    oracle, observed = sample()
    oracle.update({'ids':[], 'export_ids':[3,1]})
    observed.update({'ids':[3,1], 'export_ids':[1], 'export_has_bom':True, 'export_columns':['id']})
    result = grade(oracle,observed)
    assert not result['checks']['ids']
    assert not result['checks']['export_ids']
    assert not result['checks']['export_columns']


def test_related_business_table_corruption_is_detected():
    oracle,observed = sample()
    observed['before_related'] = {'order_items':[{'id':1,'quantity':1}]}
    observed['after_related'] = {'order_items':[{'id':1,'quantity':2}]}
    assert not grade(oracle,observed)['checks']['related_business_unchanged']
