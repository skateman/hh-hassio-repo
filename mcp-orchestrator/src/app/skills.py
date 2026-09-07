from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

import yaml

logger = logging.getLogger(__name__)

_BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
_DEFAULT_SKILLS_DIR = Path("/config/skills")
_SIGNALS = {"entity_unresolved", "entity_lookup", "multi_site_request"}
_OUTCOMES = {"tool_error"}
_CHANNELS = {"voice", "telegram"}
_ACTIVATION_KEYS = {
    "mcp-orchestrator-priority",
    "mcp-orchestrator-channels",
    "mcp-orchestrator-keywords",
    "mcp-orchestrator-followup-keywords",
    "mcp-orchestrator-signals",
    "mcp-orchestrator-outcomes",
}
_FRONTMATTER_KEYS = {
    "name", "description", "license", "compatibility", "metadata", "allowed-tools",
}


def _words(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    return tuple(re.findall(r"[^\W_]+", normalized))


def _has_phrase(words: tuple[str, ...], phrase: str) -> bool:
    tokens = _words(phrase)
    return any(
        words[index:index + len(tokens)] == tokens
        for index in range(len(words) - len(tokens) + 1)
    )


def _string_list(
    metadata: dict[str, str],
    key: str,
    *,
    choices: set[str] | None = None,
) -> tuple[str, ...]:
    try:
        value = json.loads(metadata.get(key, "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{key} must contain a JSON array") from exc
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{key} must contain a JSON array of non-empty strings")
    if choices is not None and any(item not in choices for item in value):
        raise ValueError(f"{key} contains an unsupported value")
    if choices is None and any(not _words(item) for item in value):
        raise ValueError(f"{key} phrases must contain words")
    return tuple(dict.fromkeys(value))


@dataclass(frozen=True)
class SkillContext:
    user_message: str
    channel: str = "voice"
    has_previous_turn: bool = False
    signals: frozenset[str] = frozenset()
    outcomes: frozenset[str] = frozenset()

    @classmethod
    def for_request(
        cls,
        user_message: str,
        system_messages: Iterable[str],
        *,
        has_previous_turn: bool,
        multi_site_request: bool,
        entity_unresolved: bool,
    ) -> SkillContext:
        signals = set()
        if multi_site_request:
            signals.add("multi_site_request")
        if entity_unresolved:
            signals.add("entity_unresolved")
        return cls(
            user_message=user_message,
            channel=(
                "telegram"
                if any("telegram" in _words(text) for text in system_messages)
                else "voice"
            ),
            has_previous_turn=has_previous_turn,
            signals=frozenset(signals),
        )

    def after_tool(self, name: str, result: str, *, failed: bool) -> SkillContext:
        signals = set(self.signals)
        outcomes = set(self.outcomes)
        if failed:
            outcomes.add("tool_error")
        tool_name = name.rsplit("__", 1)[-1]
        if tool_name in {"SearchEntities", "GetLiveContext"}:
            signals.add("entity_lookup")
        elif tool_name == "GetEntityState":
            try:
                payload = json.loads(result)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("success") is False:
                signals.add("entity_unresolved")
        return replace(
            self, signals=frozenset(signals), outcomes=frozenset(outcomes)
        )


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str
    instructions: str
    priority: int
    channels: tuple[str, ...]
    keywords: tuple[str, ...]
    followup_keywords: tuple[str, ...]
    signals: tuple[str, ...]
    outcomes: tuple[str, ...]

    def match(self, context: SkillContext) -> str | None:
        if context.channel not in self.channels:
            return None
        for outcome in self.outcomes:
            if outcome in context.outcomes:
                return outcome
        for signal in self.signals:
            if signal in context.signals:
                return signal
        words = _words(context.user_message)
        if context.has_previous_turn and any(
            _has_phrase(words, phrase) for phrase in self.followup_keywords
        ):
            return "followup"
        if any(_has_phrase(words, phrase) for phrase in self.keywords):
            return "keyword"
        return None

    def message(self) -> dict[str, str]:
        return {
            "role": "system",
            "content": (
                f"Runtime skill: {self.name} (version {self.version}). "
                "Follow the core instructions and the supplied tool scope; "
                "this guidance grants no additional permissions.\n\n"
                + self.instructions
            ),
        }


@dataclass(frozen=True)
class SkillRegistry:
    skills: tuple[Skill, ...] = ()

    @classmethod
    def from_environment(
        cls, *, default_directory: Path | None = None
    ) -> SkillRegistry:
        enabled = os.environ.get("SKILLS_ENABLED", "true").strip().lower()
        if enabled not in {"true", "false"}:
            raise ValueError("SKILLS_ENABLED must be 'true' or 'false'")
        if enabled == "false":
            return cls()
        directory = os.environ.get(
            "SKILLS_DIRECTORY", str(default_directory or _DEFAULT_SKILLS_DIR)
        ).strip()
        if not directory:
            raise ValueError("SKILLS_DIRECTORY must not be empty")
        active_directory = Path(directory).expanduser().absolute()
        cls.initialize_directory(active_directory)
        return cls.load(active_directory)

    @classmethod
    def load(cls, directory: Path) -> SkillRegistry:
        skills = sorted(
            cls._load_directory(directory),
            key=lambda skill: (-skill.priority, skill.name),
        )
        logger.info(
            "Loaded %d runtime skills from %s: %s",
            len(skills), directory, ", ".join(s.name for s in skills),
        )
        return cls(tuple(skills))

    @classmethod
    def initialize_directory(
        cls, directory: Path, *, templates_directory: Path | None = None
    ) -> None:
        directory = directory.expanduser().absolute()
        if directory.is_symlink():
            raise ValueError("The active skills directory must not be a root or symlink")
        directory = directory.resolve()
        if directory.parent == directory:
            raise ValueError("The active skills directory must not be a root or symlink")
        # Keep the marker outside the skills folder so deleting skills never
        # causes an update or restart to repopulate them.
        marker = directory.parent / f".{directory.name}.initialized"
        if marker.is_symlink():
            raise ValueError(f"Skill initialization marker must not be a symlink: {marker}")
        if marker.exists():
            if not marker.is_file():
                raise ValueError(f"Invalid skill initialization marker: {marker}")
            return

        templates = templates_directory or _BUNDLED_SKILLS_DIR
        defaults = cls._load_directory(templates)
        if not defaults:
            raise ValueError(f"No bundled skill templates found in {templates}")
        directory.mkdir(parents=True, exist_ok=True)
        for skill in defaults:
            destination = directory / skill.name / "SKILL.md"
            if destination.parent.is_symlink():
                raise ValueError(f"Cannot seed a symlinked skill directory: {destination.parent}")
            destination.parent.mkdir(exist_ok=True)
            source = templates / skill.name / "SKILL.md"
            if cls._write_if_missing(destination, source.read_bytes()):
                logger.info("Seeded default skill: %s", skill.name)
        cls._write_if_missing(marker, b"1\n")

    @staticmethod
    def _write_if_missing(destination: Path, content: bytes) -> bool:
        if destination.exists() or destination.is_symlink():
            return False
        # Publish a complete file atomically without replacing a user's file,
        # even if another initializer/editor creates it concurrently.
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".skill-") as temp:
            temp.write(content)
            temp.flush()
            os.fchmod(temp.fileno(), 0o644)
            os.fsync(temp.fileno())
            try:
                os.link(temp.name, destination)
            except FileExistsError:
                return False
        return True

    @classmethod
    def _load_directory(cls, directory: Path) -> list[Skill]:
        try:
            root = directory.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"Skill directory does not exist: {directory}") from exc
        if not root.is_dir():
            raise ValueError(f"Skill path is not a directory: {directory}")
        paths = []
        for child in sorted(root.iterdir()):
            path = child / "SKILL.md"
            if child.is_dir() and (path.exists() or path.is_symlink()):
                paths.append(path)
        skills: list[Skill] = []
        for path in paths:
            try:
                if not path.resolve().is_relative_to(root):
                    raise ValueError(
                        "Skill symlink must stay inside the configured skills directory"
                    )
                skills.append(cls._load_skill(path))
            except (ValueError, yaml.YAMLError) as exc:
                raise ValueError(f"Invalid skill {path}: {exc}") from exc
        return skills

    @staticmethod
    def _load_skill(path: Path) -> Skill:
        if not path.is_file():
            raise ValueError("SKILL.md must be a regular file")
        if path.stat().st_size > 64 * 1024:
            raise ValueError("SKILL.md must not exceed 64 KiB")
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not lines or lines[0] != "---":
            raise ValueError("SKILL.md must begin with YAML frontmatter")
        try:
            end = lines.index("---", 1)
        except ValueError as exc:
            raise ValueError("Missing closing frontmatter delimiter") from exc
        frontmatter = yaml.safe_load("\n".join(lines[1:end]))
        if not isinstance(frontmatter, dict):
            raise ValueError("Frontmatter must be a mapping")
        if set(frontmatter) - _FRONTMATTER_KEYS:
            raise ValueError("Use metadata for nonstandard frontmatter fields")

        name = frontmatter.get("name")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 64
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None
            or name != path.parent.name
        ):
            raise ValueError("name must be a lowercase slug matching its directory")
        description = frontmatter.get("description")
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > 1024
        ):
            raise ValueError("description must contain 1-1024 characters")
        for key in ("license", "compatibility", "allowed-tools"):
            if key in frontmatter and (
                not isinstance(frontmatter[key], str) or not frontmatter[key].strip()
            ):
                raise ValueError(f"{key} must be a non-empty string")
        if len(frontmatter.get("compatibility", "")) > 500:
            raise ValueError("compatibility must not exceed 500 characters")

        metadata = frontmatter.get("metadata", {})
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        ):
            raise ValueError("metadata must map string keys to string values")
        if any(
            key.startswith("mcp-orchestrator-") and key not in _ACTIVATION_KEYS
            for key in metadata
        ):
            raise ValueError("Unknown mcp-orchestrator activation field")
        version = metadata.get("version", "")
        if not version.strip():
            raise ValueError("metadata.version is required for runtime skills")
        priority_text = metadata.get("mcp-orchestrator-priority", "0")
        if re.fullmatch(r"[0-9]{1,4}", priority_text) is None:
            raise ValueError(
                "mcp-orchestrator-priority must be an integer string from 0 to 1000"
            )
        priority = int(priority_text)
        if priority > 1000:
            raise ValueError("mcp-orchestrator-priority must be between 0 and 1000")
        channels = _string_list(metadata, "mcp-orchestrator-channels", choices=_CHANNELS)
        if "mcp-orchestrator-channels" in metadata and not channels:
            raise ValueError("mcp-orchestrator-channels must not be empty")
        keywords = _string_list(metadata, "mcp-orchestrator-keywords")
        followups = _string_list(metadata, "mcp-orchestrator-followup-keywords")
        signals = _string_list(metadata, "mcp-orchestrator-signals", choices=_SIGNALS)
        outcomes = _string_list(metadata, "mcp-orchestrator-outcomes", choices=_OUTCOMES)
        if not any((keywords, followups, signals, outcomes)):
            raise ValueError("At least one activation trigger is required")
        instructions = "\n".join(lines[end + 1:]).strip()
        if not instructions:
            raise ValueError("The Markdown body must contain instructions")

        return Skill(
            name=name,
            description=description,
            version=version,
            instructions=instructions,
            priority=priority,
            channels=channels or ("voice", "telegram"),
            keywords=keywords,
            followup_keywords=followups,
            signals=signals,
            outcomes=outcomes,
        )


@dataclass(frozen=True)
class SkillActivation:
    skill: Skill
    iteration: int
    reason: str


@dataclass
class SkillSession:
    """Activation state belongs to one request, never to a client or site."""

    registry: SkillRegistry
    activations: list[SkillActivation] = field(default_factory=list)
    _injected_count: int = 0

    def activate(self, context: SkillContext, *, iteration: int = 1) -> bool:
        names = {activation.skill.name for activation in self.activations}
        changed = False
        for skill in self.registry.skills:
            if skill.name in names:
                continue
            reason = skill.match(context)
            if reason:
                self.activations.append(SkillActivation(skill, iteration, reason))
                logger.debug(
                    "Activated skill %s v%s at iteration %d (%s)",
                    skill.name, skill.version, iteration, reason,
                )
                changed = True
        return changed

    def inject(self, messages: list[dict[str, Any]], *, offset: int) -> None:
        active = sorted(
            self.activations,
            key=lambda activation: (-activation.skill.priority, activation.skill.name),
        )
        messages[offset:offset + self._injected_count] = [
            activation.skill.message() for activation in active
        ]
        self._injected_count = len(active)

    def records(self) -> list[dict[str, str | int]]:
        return [
            {
                "name": activation.skill.name,
                "version": activation.skill.version,
                "iteration": activation.iteration,
                "reason": activation.reason,
            }
            for activation in self.activations
        ]
