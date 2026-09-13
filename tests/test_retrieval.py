import json

from backend.app.rag.retrieval import retrieve


def test_relevant_cases_and_deterministic_budget():
    summary = [{'query': {'page': '1'}, 'response_shape': {'items': [{'id': '<integer>'}], 'has_next': '<boolean>', 'total': '<integer>'}}]
    result = retrieve(summary)
    assert result == retrieve(summary)
    assert result['cases'][0]['id'] == 'page_items'
    assert len(result['cases']) <= 2
    assert len(json.dumps(result).encode()) < 6000
    assert len(result['corpus_sha256']) == 64


def test_empty_unrelated_and_disabled_have_no_examples():
    assert retrieve([])['cases'] == []
    assert retrieve([{'response_shape': {'unmatched_xyz': '<string>'}}])['cases'] == []
    assert retrieve([{'query': {'page': '1'}}], enabled=False)['cases'] == []


def test_query_uses_keys_not_values():
    value = retrieve([{'query': {'page': 'private-secret'}, 'response_shape': {'items': [{'id': 'private-person'}]}}])
    assert 'private' not in json.dumps(value)


def test_api_exposes_strict_rag_toggle():
    from backend.app.main import TaskInput
    import pytest
    assert TaskInput().rag_enabled is True
    assert TaskInput(rag_enabled=False).rag_enabled is False
    with pytest.raises(ValueError):
        TaskInput(rag_enabled='false')
