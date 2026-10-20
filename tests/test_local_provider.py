import json

from harness.provider.local_models.local_openai import LocalOpenAICompatibleProvider


def test_local_provider_reads_endpoint_key_and_model_from_json(tmp_path):
    config_path = tmp_path / "local_models.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "server": {
                        "options": {
                            "baseURL": "http://localhost:9000/v1",
                            "apiKey": "json-secret",
                        },
                        "models": {"model-a": {"name": "Model A"}},
                    }
                },
                "model": "server/model-a",
            }
        ),
        encoding="utf-8",
    )

    provider = LocalOpenAICompatibleProvider(config_path=config_path)

    assert provider.model == "model-a"
    assert provider.base_url == "http://localhost:9000/v1"
    assert provider._headers()["Authorization"] == "Bearer json-secret"


def test_local_provider_omits_auth_for_keyless_servers(tmp_path):
    config_path = tmp_path / "local_models.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "server": {
                        "options": {"baseURL": "http://localhost:9000/v1"},
                        "models": {"model-a": {}},
                    }
                },
                "model": "server/model-a",
            }
        ),
        encoding="utf-8",
    )

    provider = LocalOpenAICompatibleProvider(config_path=config_path)

    assert provider._headers() == {"Content-Type": "application/json"}
