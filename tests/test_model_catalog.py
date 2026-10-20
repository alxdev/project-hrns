import json

from harness.model_catalog import ModelCatalog, build_provider_for_choice


def test_catalog_combines_local_and_cloud_models(tmp_path):
    (tmp_path / "local_models.json").write_text(
        json.dumps(
            {
                "provider": {
                    "litert": {
                        "name": "LiteRT",
                        "options": {"baseURL": "http://localhost:9379/v1"},
                        "models": {"local-model": {"name": "Local model"}},
                    }
                },
                "model": "litert/local-model",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "cloud_models.json").write_text(
        json.dumps({"provider": {"openai": {"models": {"cloud-model": {"name": "Cloud model"}}}}}),
        encoding="utf-8",
    )

    catalog = ModelCatalog.load(tmp_path)

    assert [choice.key for choice in catalog.choices] == [
        "local/litert/local-model",
        "openai/cloud-model",
    ]
    assert catalog.find("openai/cloud-model").name == "Cloud model"
    assert "local/litert/local-model" in catalog.describe()


def test_cloud_model_api_key_env_is_used(monkeypatch):
    from harness.model_catalog import ModelChoice

    monkeypatch.setenv("TEST_OPENAI_KEY", "secret")
    provider = build_provider_for_choice(
        ModelChoice(
            key="openai/model",
            provider="openai",
            model="model",
            name="Model",
            options={"apiKeyEnv": "TEST_OPENAI_KEY"},
        )
    )

    assert provider.model == "model"


def test_catalog_statuses_report_missing_cloud_credentials(monkeypatch):
    from harness.model_catalog import ModelCatalog, ModelChoice

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    catalog = ModelCatalog(
        (
            ModelChoice(
                key="openai/gpt-4o-mini",
                provider="openai",
                model="gpt-4o-mini",
                name="GPT-4o Mini",
                options={},
            ),
        )
    )

    status = catalog.statuses()[0]

    assert not status.available
    assert status.reason == "OPENAI_API_KEY is not set"
    assert "unavailable" in status.label()
