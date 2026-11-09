import logging
from types import SimpleNamespace

import pytest

from elice_knowledge import prompts
from elice_knowledge.prompts import (
    ANSWER,
    PLANNER,
    PROMPT_NAMES,
    PromptError,
    PromptUnavailable,
    bundled,
    bundled_template,
    from_profile,
    parse_selectors,
    resolve,
    template_digest,
    validate_template,
)
from elice_knowledge.settings import Settings

REGISTRY = Settings(mlflow_tracking_uri="https://mlflow.private.test", prompt_alias="development")


class FakeRegistry:
    def __init__(self, versions=None, fail=False):
        # name -> list of templates (index + 1 == version); aliases name -> {alias: version}
        self.versions = versions if versions is not None else {}
        self.aliases = {}
        self.fail = fail
        self.loaded = []

    def load_prompt(self, uri, allow_missing=False, cache_ttl_seconds=None):
        self.loaded.append(uri)
        if self.fail:
            raise ConnectionError("https://mlflow.private.test refused")
        name, _, selector = uri.removeprefix("prompts:/").replace("@", "/").partition("/")
        history = self.versions.get(name, [])
        if selector == "latest":
            version = len(history) or None
        elif selector.isdigit():
            version = int(selector) if int(selector) <= len(history) else None
        else:
            version = self.aliases.get(name, {}).get(selector)
        if version is None:
            return None
        return SimpleNamespace(version=version, template=history[version - 1])

    def register_prompt(self, name, template, commit_message=None, tags=None):
        self.versions.setdefault(name, []).append(template)
        return SimpleNamespace(version=len(self.versions[name]), template=template)

    def set_prompt_alias(self, name, alias, version):
        self.aliases.setdefault(name, {})[alias] = version


@pytest.fixture
def registry(monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(prompts, "registry_client", lambda settings: fake)
    return fake


def test_bundled_templates_satisfy_the_code_contract():
    for name in PROMPT_NAMES:
        prompt = bundled(name)
        assert [m["role"] for m in prompt.template] == ["system", "user"]
        assert prompt.digest == template_digest(bundled_template(name))
    assert "{{max_calls}}" in bundled(PLANNER).template[0]["content"]


def test_render_is_single_pass_and_requires_exact_variables():
    messages = bundled(ANSWER).render(payload='{"text": "{{output_schema}}"}', output_schema="S")
    assert '{"text": "{{output_schema}}"}' in messages[1].content
    with pytest.raises(PromptError):
        bundled(ANSWER).render(payload="p")


@pytest.mark.parametrize(
    "template",
    [
        [{"role": "user", "content": "{{payload}} {{output_schema}}"}],
        [{"role": "system", "content": "x"}, {"role": "user", "content": "{{payload}}"}],
        [
            {"role": "system", "content": "{{secret}}"},
            {"role": "user", "content": "{{payload}} {{output_schema}}"},
        ],
        "plain text {{payload}} {{output_schema}}",
    ],
)
def test_templates_must_keep_chat_shape_and_variables(template):
    with pytest.raises(PromptError):
        validate_template(ANSWER, template)


def test_mlflow_disabled_uses_bundled_and_records_reason():
    resolved = resolve(Settings(mlflow_enabled=False))
    assert {p.source for p in resolved.values()} == {"bundled"}
    assert resolved[PLANNER].fallback_reason == "mlflow_disabled"
    with pytest.raises(PromptUnavailable):
        resolve(Settings(mlflow_enabled=False), {PLANNER: "3"})


def test_alias_resolves_to_a_pinned_registry_version(registry):
    prompts.register(REGISTRY, "development")
    resolved = resolve(REGISTRY)
    assert registry.loaded[-2:] == [f"prompts:/{n}@development" for n in PROMPT_NAMES]
    assert resolved[PLANNER].source == "registry"
    assert resolved[PLANNER].uri == f"prompts:/{PLANNER}/1" and resolved[PLANNER].version == 1
    assert resolved[PLANNER].digest == bundled(PLANNER).digest


def test_unregistered_or_unreachable_registry_falls_back_with_reason(registry, caplog):
    assert resolve(REGISTRY)[ANSWER].fallback_reason == "prompt_not_registered"
    registry.fail = True
    with caplog.at_level(logging.WARNING):
        resolved = resolve(REGISTRY)
    assert resolved[ANSWER].source == "bundled"
    assert resolved[ANSWER].fallback_reason == "registry_unavailable"
    assert "ConnectionError" in caplog.text and "private" not in caplog.text


def test_explicit_experiment_selectors_never_fall_back(registry):
    with pytest.raises(PromptUnavailable):
        resolve(REGISTRY, {PLANNER: "2"})
    registry.fail = True
    with pytest.raises(PromptUnavailable) as error:
        resolve(REGISTRY, {PLANNER: "development"})
    assert "private" not in str(error.value)


def test_experiment_version_overrides_only_its_prompt(registry):
    prompts.register(REGISTRY, "development")
    changed = [dict(m) for m in bundled_template(PLANNER)]
    changed[0]["content"] += "\n검색은 두 번까지만 하세요."
    registry.register_prompt(PLANNER, changed)
    resolved = resolve(REGISTRY, {PLANNER: "2"})
    assert (
        resolved[PLANNER].version == 2 and "두 번까지만" in resolved[PLANNER].template[0]["content"]
    )
    assert resolved[ANSWER].version == 1
    assert f"prompts:/{PLANNER}/2" in registry.loaded


def test_invalid_registry_template_is_rejected_not_replaced(registry):
    registry.versions[ANSWER] = [[{"role": "user", "content": "broken"}]]
    registry.aliases[ANSWER] = {"development": 1}
    with pytest.raises(PromptError, match="chat template"):
        resolve(REGISTRY)


def test_register_is_idempotent_and_versions_only_changed_templates(registry, monkeypatch):
    first = prompts.register(REGISTRY, "development")
    assert [r["created"] for r in first] == [True, True]
    again = prompts.register(REGISTRY, "production")
    assert [r["created"] for r in again] == [False, False]
    assert registry.aliases[PLANNER] == {"development": 1, "production": 1}
    edited = [dict(m) for m in bundled_template(PLANNER)]
    edited[0]["content"] += "\n추가 지시"
    original = prompts.bundled_template
    monkeypatch.setattr(
        prompts, "bundled_template", lambda name: edited if name == PLANNER else original(name)
    )
    third = prompts.register(REGISTRY, "development")
    assert [(r["name"], r["version"], r["created"]) for r in third] == [
        (PLANNER, 2, True),
        (ANSWER, 1, False),
    ]


def test_set_alias_checks_version_and_contract(registry):
    prompts.register(REGISTRY, "development")
    assert prompts.set_alias(REGISTRY, PLANNER, "production", 1)["version"] == 1
    with pytest.raises(PromptUnavailable):
        prompts.set_alias(REGISTRY, PLANNER, "production", 9)
    with pytest.raises(PromptError):
        prompts.set_alias(REGISTRY, PLANNER, "latest", 1)


def test_selector_parsing_and_profile_round_trip():
    assert parse_selectors([f"{PLANNER}=7", f"{ANSWER}=development"]) == {
        PLANNER: "7",
        ANSWER: "development",
    }
    for value in ["unknown=1", f"{PLANNER}=", f"{PLANNER}=Bad Alias"]:
        with pytest.raises(PromptError):
            parse_selectors([value])
    config = {"prompts": {n: bundled(n).model_dump() for n in PROMPT_NAMES}}
    assert from_profile(config)[PLANNER].digest == bundled(PLANNER).digest
    with pytest.raises(PromptError):
        from_profile({})
