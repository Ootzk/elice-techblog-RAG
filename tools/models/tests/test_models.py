import importlib.util
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

spec = importlib.util.spec_from_file_location(
    "model_profiles", Path(__file__).parents[1] / "models.py"
)
models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(models)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".env*\n")
    monkeypatch.setattr(models, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_capture_and_switch_preserves_secret_without_output(repo, capsys):
    env = repo / ".env.dev"
    env.write_text(
        "GENERATION_MODEL=terra\nGENERATION_BASE_URL=https://example.com/v1\nGENERATION_API_KEY=private-key\nELICE_API_KEY=private-key\n"
    )
    models.capture("answer", "terra", env, repo / ".env.models.dev")
    assert "private-key" not in (repo / ".env.models.dev").read_text()
    env.write_text(env.read_text().replace("terra", "qwen"))
    models.capture("answer", "qwen", env, repo / ".env.models.dev")
    models.select("answer", "terra", repo / ".env.models.dev", env)
    result = dotenv_values(env)
    assert result["GENERATION_MODEL"] == "terra"
    assert result["GENERATION_API_KEY"] == "private-key"
    assert "private-key" not in capsys.readouterr().out
    assert (repo / ".env.models.dev").stat().st_mode & 0o077 == 0


def test_embedding_switch_restores_its_index(repo):
    env = repo / ".env.dev"
    env.write_text(
        "EMBEDDING_BACKEND=ollama\nEMBEDDING_BASE_URL=http://localhost:11434\nEMBEDDING_MODEL=bge-m3\nEMBEDDING_DIMENSIONS=1024\nQA_ACTIVE_INDEX_KEY=old-index\n"
    )
    models.capture("embedding", "old", env, repo / ".env.models.dev")
    env.write_text(
        env.read_text()
        .replace("bge-m3", "other")
        .replace("QA_ACTIVE_INDEX_KEY=old-index", "QA_ACTIVE_INDEX_KEY=new-index")
    )
    models.select("embedding", "old", repo / ".env.models.dev", env)
    assert dotenv_values(env)["QA_ACTIVE_INDEX_KEY"] == "old-index"


def test_tracked_env_is_rejected(repo):
    p = repo / ".env.dev"
    p.write_text("TOKEN=secret\n")
    subprocess.run(["git", "add", "-f", ".env.dev"], check=True)
    with pytest.raises(ValueError, match="추적"):
        models.safe_path(p)


def test_incomplete_profile_does_not_modify_env(repo):
    p = repo / ".env.dev"
    p.write_text("GENERATION_MODEL=old\n")
    models.write_catalog(
        repo / ".env.models.dev",
        {"answer": {"bad": {"model": "bad"}}, "embedding": {}, "judge": {}},
    )
    with pytest.raises(ValueError):
        models.select("answer", "bad", repo / ".env.models.dev", p)
    assert dotenv_values(p)["GENERATION_MODEL"] == "old"


def test_json_profiles_can_contain_any_independent_judge_names(repo):
    from elice_knowledge.judges import selected_raters
    from elice_knowledge.settings import Settings

    catalog = repo / ".env.models.dev.json"
    data = {
        "answer": {},
        "embedding": {},
        "judge": {
            "rater-a": {
                "model": "vendor/model-a",
                "base_url": "https://example.test/v1",
            },
            "rater-b": {
                "model": "vendor/model-b",
                "base_url": "https://example.test/v1",
            },
        },
    }
    models.write_catalog(catalog, data)
    assert set(models.read_catalog(catalog)["judge"]) == {"rater-a", "rater-b"}
    assert selected_raters(Settings(evaluation_judge_raters="rater-a,rater-b")) == [
        "rater-a",
        "rater-b",
    ]
    with pytest.raises(ValueError):
        selected_raters(Settings(evaluation_judge_raters="rater-a,rater-a"))


def test_shared_key_is_taken_from_environment(repo):
    data = {
        "answer": {"first": {"model": "m", "base_url": "https://example.test/v1"}},
        "embedding": {},
        "judge": {},
    }
    profile = models.resolve_profile(
        data, "answer", "first", {"ELICE_API_KEY": "some-private-key"}
    )
    assert profile["api_key"] == "some-private-key"
    data["answer"]["first"]["api_key"] = "not-allowed"
    with pytest.raises(ValueError):
        models.resolve_profile(data, "answer", "first", {})


def test_public_catalog_rejects_credentials_and_direct_endpoints(repo):
    for extra in (
        {"api_key": "test-key"},
        {"password": "test"},
        {"base_url": "https://user:secret@example.test/v1"},
    ):
        data = {
            "answer": {"one": {"model": "model", **extra}},
            "embedding": {},
            "judge": {},
        }
        with pytest.raises(ValueError):
            models.write_catalog(repo / "models.json", data)
