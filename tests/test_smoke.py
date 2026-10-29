from evaluation.runner import run_smoke


def test_twenty_smoke_tasks(tmp_path,monkeypatch):
    # Smoke acceptance must remain offline even if the user's environment enables paid models.
    monkeypatch.setenv('MODEL_ADAPTER','openai')
    monkeypatch.setenv('MODEL_CALLS_ENABLED','true')
    report = run_smoke(tmp_path/'smoke')
    assert report['passed'] == report['total'] == 20, report
