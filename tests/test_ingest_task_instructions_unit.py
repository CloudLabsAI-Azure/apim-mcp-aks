import os
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts import ingest_task_instructions as ingest


def test_parse_project_info_api_projects_url():
    url = "https://eastus.api.cognitive.microsoft.com/api/projects/12345"
    info = ingest._parse_project_info(url)
    assert info["base"] == "https://eastus.api.cognitive.microsoft.com"
    assert info["project_id"] == "12345"


def test_parse_project_info_projects_url():
    url = "https://eastus.api.cognitive.microsoft.com/projects/abcde"
    info = ingest._parse_project_info(url)
    assert info["base"] == "https://eastus.api.cognitive.microsoft.com"
    assert info["project_id"] == "abcde"


def test_parse_project_info_invalid():
    assert ingest._parse_project_info("") is None
    assert ingest._parse_project_info("https://example.com/foo") is None


def test_ensure_knowledge_base_skips_when_no_endpoint(monkeypatch):
    monkeypatch.setattr(ingest, "FOUNDRY_PROJECT_ENDPOINT", "")
    # Should not raise
    ingest.ensure_knowledge_base(credential=mock.Mock())


def test_ensure_knowledge_base_handles_http_errors(monkeypatch):
    # Provide a parsable endpoint
    monkeypatch.setattr(
        ingest,
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://eastus.api.cognitive.microsoft.com/api/projects/12345",
    )
    # Mock token fetch
    fake_token = SimpleNamespace(token="fake-token")
    cred = mock.Mock()
    cred.get_token.return_value = fake_token

    # Mock requests.get/post to simulate API shape
    with mock.patch.object(ingest.requests, "get") as mock_get, mock.patch.object(
        ingest.requests, "post"
    ) as mock_post:
        # First call returns non-200 to trigger creation path
        mock_get.return_value = SimpleNamespace(status_code=404, text="not found", json=lambda: {})
        mock_post.return_value = SimpleNamespace(status_code=500, text="boom")
        # Should not raise even if KB creation fails
        ingest.ensure_knowledge_base(credential=cred)
        mock_get.assert_called_once()
        mock_post.assert_called_once()


def test_create_search_index_calls_create_or_update(monkeypatch):
    # Patch constants for test
    monkeypatch.setattr(ingest, "AZURE_SEARCH_INDEX_NAME", "unit-test-index")
    fake_client = mock.Mock()
    fake_client.create_or_update_index.return_value = SimpleNamespace(name="unit-test-index")
    ingest.create_search_index(fake_client)
    fake_client.create_or_update_index.assert_called_once()


def test_embedding_provider_flags(monkeypatch):
    # none / skip
    monkeypatch.setattr(ingest, "SKIP_EMBEDDINGS", True)
    fn = ingest.get_embedding_client()
    out = fn(["hello"])
    assert out == [[]]

    # azure_openai
    monkeypatch.setattr(ingest, "SKIP_EMBEDDINGS", False)
    monkeypatch.setattr(ingest, "EMBEDDING_PROVIDER", "azure_openai")
    monkeypatch.setattr(ingest, "AZURE_OPENAI_ENDPOINT", "https://dummy")
    monkeypatch.setattr(ingest, "AZURE_OPENAI_API_KEY", "dummy")
    with mock.patch("openai.AzureOpenAI") as mock_client:
        mock_inst = mock.Mock()
        mock_inst.embeddings.create.return_value = SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])])
        mock_client.return_value = mock_inst
        fn = ingest.get_embedding_client()
        res = fn(["hello"])
        assert res[0] == [0.1, 0.2]


def test_fallback_to_cognitiveservices(monkeypatch):
    # Simulate services.ai endpoint -> fallback to cognitiveservices
    monkeypatch.setattr(ingest, "SKIP_EMBEDDINGS", False)
    monkeypatch.setattr(ingest, "EMBEDDING_PROVIDER", "foundry")
    monkeypatch.setattr(
        ingest,
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://cog-xyz.services.ai.azure.com/api/projects/proj-default",
    )
    fake_credential = mock.Mock()
    fake_credential.get_token.return_value = SimpleNamespace(token="fake-token")

    # First call raises 403, second fallback call succeeds
    # Patch ingest.AzureOpenAI to avoid real network calls
    with mock.patch("scripts.ingest_task_instructions.AzureOpenAI") as mock_client:
        primary_client = mock.Mock()
        secondary_client = mock.Mock()
        primary_client.embeddings.create.side_effect = Exception("403 Public access is disabled")
        secondary_client.embeddings.create.return_value = SimpleNamespace(data=[SimpleNamespace(embedding=[0.9])])
        mock_client.side_effect = [primary_client, secondary_client]

        fn = ingest.get_embedding_client()
        res = fn(["hello world"])
        assert res[0] == [0.9]
        assert primary_client.embeddings.create.call_count == 1
        assert secondary_client.embeddings.create.call_count == 1

def test_sanitize_for_search_normalizes_arrays(monkeypatch):
    doc = {
        "id": "doc1",
        "title": "t",
        "description": "d",
        "content": "hello world",
        "keywords": ["a", 1, {"x": 2}],
        "related_tasks": "single",
        "steps": [{"step": 1}],
        "estimated_effort": [1, 2],
        "created_at": None,
        "embedding": None,
    }
    sanitized = ingest.sanitize_for_search(doc)
    assert isinstance(sanitized["estimated_effort"], str)
    assert isinstance(sanitized["related_tasks"], str)
    assert sanitized["keywords"] == ["a", "1", "{'x': 2}"]
    assert sanitized["embedding"] == []
    assert isinstance(sanitized["steps"], str)


def test_prepare_documents_gracefully_handles_embedding_errors(monkeypatch):
    documents = [
        {
            "id": "doc1",
            "title": "t",
            "description": "d",
            "content": "hello world",
            "steps": [{"step": 1}],
        }
    ]

    def failing_embed(texts):
        raise RuntimeError("boom")

    indexed = ingest.prepare_documents_for_indexing(documents, failing_embed)
    assert indexed[0]["embedding"] == []  # should continue without embeddings
    assert isinstance(indexed[0]["steps"], str)
