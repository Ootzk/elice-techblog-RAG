"""Versioned QA prompts: MLflow Prompt Registry first, bundled repository templates as fallback.

The repository templates under data/prompts are the reviewed source and the
fallback when MLflow is disabled, unreachable or has no registered prompt yet.
A QA profile freezes the resolved template content, so conversations and
evaluations never re-read the registry and an alias change only affects new
profiles. Server-specific version numbers differ; the content digest identifies
the same prompt across dev and submission MLflow servers.
"""

import logging
import re
from pathlib import Path
from typing import Literal

from llama_index.core.llms import ChatMessage, MessageRole
from pydantic import BaseModel

from elice_knowledge.corpus import digest
from elice_knowledge.settings import Settings

PLANNER = "elice-agentic-planner"
ANSWER = "elice-grounded-answer"
REQUIRED_VARIABLES = {
    PLANNER: {"max_calls", "payload", "output_schema"},
    ANSWER: {"payload", "output_schema"},
}
PROMPT_NAMES = tuple(REQUIRED_VARIABLES)
BUNDLED_DIR = Path(__file__).parent / "data" / "prompts"
ROLES = ("system", "user")
SELECTOR = re.compile(r"^(?:[1-9][0-9]{0,8}|[a-z][a-z0-9_-]{0,62})$")
_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
DIGEST_TAG = "elice.prompt.digest"
logger = logging.getLogger(__name__)


class PromptError(ValueError):
    code = "prompt_invalid"


class PromptUnavailable(PromptError):
    code = "prompt_unavailable"


class PromptRef(BaseModel):
    source: Literal["registry", "bundled"]
    uri: str | None = None
    version: int | None = None
    digest: str
    fallback_reason: str | None = None


class ResolvedPrompt(PromptRef):
    name: str
    template: list[dict[str, str]]

    def ref(self) -> PromptRef:
        return PromptRef(**self.model_dump(include=set(PromptRef.model_fields)))

    def render(self, **values) -> list[ChatMessage]:
        if set(values) != REQUIRED_VARIABLES[self.name]:
            raise PromptError(f"{self.name} needs exactly {sorted(REQUIRED_VARIABLES[self.name])}")

        # Single pass: inserted values (JSON, sources) are never re-scanned for braces.
        def fill(text):
            return _VARIABLE.sub(lambda m: str(values[m.group(1)]), text)

        return [
            ChatMessage(role=MessageRole(message["role"]), content=fill(message["content"]))
            for message in self.template
        ]


def validate_template(name: str, template) -> list[dict[str, str]]:
    """Accept only the chat shape and variables the QA code supplies."""
    if not (
        isinstance(template, list)
        and [m.get("role") if isinstance(m, dict) else None for m in template] == list(ROLES)
        and all(isinstance(m.get("content"), str) and m["content"].strip() for m in template)
    ):
        raise PromptError(f"{name} must be a [system, user] chat template")
    found = {v for m in template for v in _VARIABLE.findall(m["content"])}
    if found != REQUIRED_VARIABLES[name]:
        raise PromptError(
            f"{name} variables {sorted(found)} != required {sorted(REQUIRED_VARIABLES[name])}"
        )
    return [{"role": m["role"], "content": m["content"]} for m in template]


def template_digest(template) -> str:
    return digest(template)


def bundled_template(name: str) -> list[dict[str, str]]:
    return validate_template(
        name,
        [
            {
                "role": role,
                "content": (BUNDLED_DIR / name / f"{role}.txt")
                .read_text(encoding="utf-8")
                .removesuffix("\n"),
            }
            for role in ROLES
        ],
    )


def bundled(name: str, reason: str | None = None) -> ResolvedPrompt:
    template = bundled_template(name)
    return ResolvedPrompt(
        name=name,
        source="bundled",
        digest=template_digest(template),
        template=template,
        fallback_reason=reason,
    )


def registry_client(settings: Settings):
    from mlflow import MlflowClient

    uri = settings.mlflow_tracking_uri.get_secret_value()
    return MlflowClient(tracking_uri=uri, registry_uri=uri)


def registry_enabled(settings: Settings) -> bool:
    return settings.mlflow_enabled and settings.mlflow_tracking_uri is not None


def _uri(name: str, selector: str) -> str:
    if not SELECTOR.match(selector):
        raise PromptError(f"Invalid prompt selector: {selector}")
    return f"prompts:/{name}/{selector}" if selector.isdigit() else f"prompts:/{name}@{selector}"


def parse_selectors(values: list[str] | None) -> dict[str, str]:
    """Parse repeated NAME=SELECTOR options (selector: version number or alias)."""
    selectors = {}
    for value in values or []:
        name, _, selector = value.partition("=")
        if name not in PROMPT_NAMES or not selector:
            raise PromptError(f"Use NAME=VERSION|ALIAS with NAME in {', '.join(PROMPT_NAMES)}")
        _uri(name, selector)
        selectors[name] = selector
    return selectors


def resolve(
    settings: Settings, selectors: dict[str, str] | None = None
) -> dict[str, ResolvedPrompt]:
    """Resolve every QA prompt to concrete content.

    The configured alias falls back to the bundled template when MLflow is
    disabled, unreachable or the prompt is not registered, and records why.
    Explicit selectors are experiments: they never fall back.
    """
    selectors = selectors or {}
    for name, selector in selectors.items():
        if name not in PROMPT_NAMES:
            raise PromptError(f"Unknown prompt: {name}")
        _uri(name, selector)
    if not registry_enabled(settings):
        if selectors:
            raise PromptUnavailable("Prompt selectors require MLflow")
        return {name: bundled(name, "mlflow_disabled") for name in PROMPT_NAMES}
    try:
        client = registry_client(settings)
    except Exception as exc:
        if selectors:
            raise PromptUnavailable("MLflow Prompt Registry is unavailable") from None
        logger.warning("Prompt registry unavailable; using bundled (%s)", type(exc).__name__)
        return {name: bundled(name, "registry_unavailable") for name in PROMPT_NAMES}
    resolved = {}
    for name in PROMPT_NAMES:
        explicit = name in selectors
        uri = _uri(name, selectors.get(name, settings.prompt_alias))
        try:
            # Aliases must take effect for the next profile; versions are immutable.
            prompt = client.load_prompt(uri, allow_missing=True, cache_ttl_seconds=0)
        except Exception as exc:
            if explicit:
                raise PromptUnavailable(f"Could not load {uri}") from None
            # Exception text can carry the tracking URI; log the type only.
            logger.warning("Prompt %s unavailable; using bundled (%s)", name, type(exc).__name__)
            resolved[name] = bundled(name, "registry_unavailable")
            continue
        if prompt is None:
            if explicit:
                raise PromptUnavailable(f"{uri} is not registered")
            resolved[name] = bundled(name, "prompt_not_registered")
            continue
        template = validate_template(name, prompt.template)
        resolved[name] = ResolvedPrompt(
            name=name,
            source="registry",
            uri=f"prompts:/{name}/{prompt.version}",
            version=int(prompt.version),
            digest=template_digest(template),
            template=template,
        )
    return resolved


def register(settings: Settings, alias: str, message: str | None = None) -> list[dict]:
    """Publish the bundled templates to the configured MLflow and point `alias` at them.

    Idempotent: an unchanged template reuses the latest version instead of
    creating a new one. Run it against each server (dev, submission) separately.
    """
    if not registry_enabled(settings):
        raise PromptUnavailable("Enable MLflow and set MLFLOW_TRACKING_URI to register prompts")
    if not SELECTOR.match(alias) or alias.isdigit() or alias == "latest":
        raise PromptError("Alias must be a lowercase name other than 'latest'")
    client = registry_client(settings)
    results = []
    for name in PROMPT_NAMES:
        template = bundled_template(name)
        content = template_digest(template)
        latest = client.load_prompt(f"prompts:/{name}@latest", allow_missing=True)
        created = latest is None or template_digest(latest.template) != content
        if created:
            latest = client.register_prompt(
                name=name,
                template=template,
                commit_message=message,
                tags={
                    DIGEST_TAG: content,
                    "elice.prompt.source": "repository",
                    "elice.build_revision": settings.qa_build_revision,
                },
            )
        client.set_prompt_alias(name, alias, int(latest.version))
        results.append(
            {
                "name": name,
                "version": int(latest.version),
                "digest": content,
                "alias": alias,
                "created": created,
            }
        )
    return results


def set_alias(settings: Settings, name: str, alias: str, version: int) -> dict:
    if name not in PROMPT_NAMES:
        raise PromptError(f"Unknown prompt: {name}")
    if not SELECTOR.match(alias) or alias.isdigit() or alias == "latest":
        raise PromptError("Alias must be a lowercase name other than 'latest'")
    if not registry_enabled(settings):
        raise PromptUnavailable("Enable MLflow and set MLFLOW_TRACKING_URI to set aliases")
    client = registry_client(settings)
    prompt = client.load_prompt(f"prompts:/{name}/{version}", allow_missing=True)
    if prompt is None:
        raise PromptUnavailable(f"prompts:/{name}/{version} is not registered")
    template = validate_template(name, prompt.template)
    client.set_prompt_alias(name, alias, version)
    return {"name": name, "version": version, "alias": alias, "digest": template_digest(template)}


def from_profile(config: dict) -> dict[str, ResolvedPrompt]:
    prompts = config.get("prompts") or {}
    if set(prompts) != set(PROMPT_NAMES):
        raise PromptError("QA profile is missing resolved prompts")
    return {name: ResolvedPrompt.model_validate(prompts[name]) for name in PROMPT_NAMES}
