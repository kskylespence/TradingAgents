"""CLI model selection for Ollama.

The fork dropped the static Ollama model list (the web discovers models
from the endpoint, because local tags 404'd against Ollama Cloud), but the
CLI still looked the provider up in that list: choosing Ollama crashed with
``KeyError: 'ollama'``. The CLI now offers the endpoint's own model list,
and falls back to typing an ID when the endpoint cannot be reached.
"""

from unittest import mock

import pytest

from cli import prompts


def _asks(value):
    return mock.Mock(ask=mock.Mock(return_value=value))


@pytest.mark.unit
class TestOllamaModelSelect:
    @pytest.mark.parametrize("mode", ["quick", "deep"])
    def test_offers_the_endpoints_models(self, mode):
        captured = {}

        def fake_select(message, choices, **kwargs):
            captured["message"] = message
            captured["values"] = [c.value for c in choices]
            return _asks("glm-5.3")

        with mock.patch.object(prompts, "_fetch_ollama_models", return_value=["glm-5.3", "glm-5.3-flash"]), \
             mock.patch.object(prompts.questionary, "select", side_effect=fake_select):
            out = prompts._select_model("ollama", mode)

        assert out == "glm-5.3"
        assert captured["values"] == ["glm-5.3", "glm-5.3-flash"]
        assert mode.title() in captured["message"]

    def test_preselects_the_previous_choice(self):
        captured = {}

        def fake_select(message, choices, **kwargs):
            captured["default"] = kwargs.get("default")
            return _asks("glm-5.3-flash")

        with mock.patch.object(prompts, "_fetch_ollama_models", return_value=["glm-5.3", "glm-5.3-flash"]), \
             mock.patch.object(prompts.questionary, "select", side_effect=fake_select):
            prompts._select_model("ollama", "quick", default="glm-5.3-flash")

        assert captured["default"] == "glm-5.3-flash"

    def test_unreachable_endpoint_falls_back_to_typing_an_id(self):
        with mock.patch.object(prompts, "_fetch_ollama_models", return_value=[]), \
             mock.patch.object(prompts, "_require_text", return_value="qwen3:30b") as typed:
            out = prompts._select_model("ollama", "deep")

        assert out == "qwen3:30b"
        assert typed.called


@pytest.mark.unit
class TestFetchOllamaModels:
    def test_reads_ids_from_the_models_endpoint_with_the_bearer_key(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
        response = mock.Mock(status_code=200)
        response.json.return_value = {"data": [{"id": "glm-5.3"}, {"id": "kimi-k2.6"}]}
        response.raise_for_status.return_value = None

        with mock.patch("requests.get", return_value=response) as get:
            ids = prompts._fetch_ollama_models("https://ollama.com/v1")

        assert ids == ["glm-5.3", "kimi-k2.6"]
        url = get.call_args.args[0]
        assert url == "https://ollama.com/v1/models"
        assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer test-key"}

    def test_no_key_sends_no_auth_header(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        response = mock.Mock(status_code=200)
        response.json.return_value = {"data": []}
        response.raise_for_status.return_value = None

        with mock.patch("requests.get", return_value=response) as get:
            prompts._fetch_ollama_models("http://localhost:11434/v1/")

        assert get.call_args.args[0] == "http://localhost:11434/v1/models"
        assert get.call_args.kwargs["headers"] == {}

    def test_failure_returns_empty(self):
        with mock.patch("requests.get", side_effect=OSError("connection refused")):
            assert prompts._fetch_ollama_models("http://localhost:11434/v1") == []
