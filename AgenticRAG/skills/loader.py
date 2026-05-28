"""Skill-file loader.

Discovers ``skills/<lang>.md`` files, parses each as YAML-frontmatter +
markdown body, validates with :class:`skills.schema.Skill`, and returns
ready-to-use Skill objects.

Design notes
------------
- Discovery is filesystem-driven. Adding a new language is literally
  "drop a new ``.md`` file in this folder, rebuild the container".
  No registry to update, no Python import to wire.
- Validation failures raise ``pydantic.ValidationError`` (or
  ``ValueError`` for I/O / format problems). The container init should
  let these propagate so a bad deploy crashes at cold start rather than
  serving garbled answers on a live call.
- ``resolve_skill`` does the locale-code -> Skill lookup the Lambda needs
  per turn (it gets values like ``en_US`` from the Connect flow, not the
  short ``en`` code).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable

import frontmatter

from .schema import Skill

DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent


def load_skill(path: Path) -> Skill:
    """Load and validate a single skill file.

    Parameters
    ----------
    path:
        Absolute or relative path to a ``.md`` skill file.

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If the file has no YAML frontmatter or no markdown body.
    pydantic.ValidationError
        If the frontmatter is missing required fields or contains
        invalid values (bad regex, missing Bedrock placeholders, etc.).
    """

    if not path.exists():
        raise FileNotFoundError(f"Skill file not found: {path}")

    post = frontmatter.load(path)

    if not post.metadata:
        raise ValueError(
            f"Skill file '{path.name}' has no YAML frontmatter. Each skill "
            "must start with a '---' fenced metadata block."
        )

    body = (post.content or "").strip()
    if not body:
        raise ValueError(
            f"Skill file '{path.name}' has an empty markdown body. The body "
            "becomes the Bedrock prompt template and cannot be empty."
        )

    payload = dict(post.metadata)
    payload["prompt_template"] = body
    return Skill.model_validate(payload)


def load_skills(directory: Path = DEFAULT_SKILLS_DIR) -> Dict[str, Skill]:
    """Load every ``*.md`` file in ``directory`` and return a dict
    keyed by short language code (e.g. ``{"en": Skill(...), "es": ...}``).

    Duplicate language codes raise ``ValueError``. This is intentional --
    if two files claim ``language: en`` we cannot deterministically pick
    one and a silent winner would be confusing.
    """

    if not directory.exists() or not directory.is_dir():
        raise FileNotFoundError(f"Skills directory not found: {directory}")

    skills: Dict[str, Skill] = {}
    for md_path in sorted(directory.glob("*.md")):
        skill = load_skill(md_path)
        if skill.language in skills:
            existing = skills[skill.language]
            raise ValueError(
                f"Duplicate skill for language '{skill.language}': "
                f"{existing.locale_codes[0]} already loaded, but "
                f"{md_path.name} also declares it."
            )
        skills[skill.language] = skill

    if not skills:
        raise ValueError(
            f"No skill files (*.md) found in {directory}. The Lambda needs "
            "at least one skill to operate."
        )

    return skills


def resolve_skill(
    skills: Dict[str, Skill],
    lang_code: str,
    default: str = "en",
) -> Skill:
    """Map an inbound language code (e.g. ``en_US``, ``es-MX``) to a
    loaded :class:`Skill`.

    Resolution order:
        1. Case-insensitive exact match against any ``locale_codes`` entry.
        2. Prefix match on the short ISO 639-1 code (``en_US`` -> ``en``).
        3. ``default`` (which must itself be a loaded skill, else KeyError).

    This mirrors the production ``_extract_lang_code`` semantics: an
    unrecognised code never leaves the caller without a skill -- we fall
    back to English so a misconfigured Connect flow can't break the call.
    """

    if not skills:
        raise ValueError("skills dict is empty; nothing to resolve against")

    if not lang_code:
        return skills[default]

    needle = lang_code.strip().lower()

    for skill in skills.values():
        if needle in {code.lower() for code in skill.locale_codes}:
            return skill

    short = needle.split("_", 1)[0].split("-", 1)[0]
    if short in skills:
        return skills[short]

    if default in skills:
        return skills[default]

    raise KeyError(
        f"No skill matches lang_code='{lang_code}' and default '{default}' "
        f"is not loaded. Available: {sorted(skills.keys())}"
    )


def available_languages(skills: Iterable[Skill]) -> Dict[str, str]:
    """Convenience for /diagnostics-style endpoints. Returns
    ``{language: persona_name}``."""

    return {skill.language: skill.persona.name for skill in skills}
