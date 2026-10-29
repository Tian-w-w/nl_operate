"""Independent final-state checks. Oracle files never enter an adapter prompt."""
import csv
import io


def grade(oracle, observed):
    checks = {}
    for key in ['ids', 'total_amount', 'state', 'error', 'preview_ids', 'aggregate', 'export_ids', 'detail_id']:
        if key in oracle:
            checks[key] = observed.get(key) == oracle[key]
    before, after = observed['before'], observed['after']
    targets = set(oracle.get('modified_ids', []))
    patch = oracle.get('patch', {})
    checks['business_membership_unchanged'] = before.keys() == after.keys()
    checks['related_business_unchanged'] = observed.get('before_related',{}) == observed.get('after_related',{})
    checks['non_target_unchanged'] = all(after.get(i) == value for i, value in before.items() if int(i) not in targets)
    checks['target_patch_exact'] = all(after.get(str(i)) == {**before[str(i)], **patch, 'version': before[str(i)]['version'] + 1} for i in targets)
    checks['no_write_before_confirmation'] = observed.get('no_write_before_confirmation', True)
    if 'export_ids' in oracle:
        checks['export_has_bom'] = observed.get('export_has_bom', False)
        checks['export_columns'] = observed.get('export_columns') == ['id', 'shipping_city', 'status', 'total_amount', 'priority']
    if oracle.get('retry_once'):
        checks['retry_same_operation'] = observed.get('retry_same_operation', False)
        checks['one_operation'] = observed.get('operation_count') == 1
    return {'passed': all(checks.values()), 'checks': checks}


def inspect_csv(content):
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
    return {'export_has_bom': content.startswith(b'\xef\xbb\xbf'),
            'export_columns': reader.fieldnames, 'export_ids': [int(r['id']) for r in reader]}
