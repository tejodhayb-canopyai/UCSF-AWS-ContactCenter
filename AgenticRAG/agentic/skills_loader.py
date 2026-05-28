"""Cold-start skill discovery + caching.

The container packs ``skills/*.md`` alongside this code. We load and
validate them once when the container starts and keep them in a module
global; every Lambda invocation thereafter just does a dict lookup.

Splitting the cache out of ``skills.loader`` (which is plain
file-system + pydantic) keeps the loader itself unit-testable without
implicit container state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from skills import Skill, load_skills, resolve_skill

# The ``skills`` package sits next to ``agentic`` in the Lambda task
# root (see Dockerfile ``COPY skills/ ...``). Going up one level from
# this file lands at the task root.
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

_SKILLS_CACHE: Dict[str, Skill] = load_skills(_SKILLS_DIR)


def all_skills() -> Dict[str, Skill]:
    """Return the cached, validated skill registry."""
    return _SKILLS_CACHE


def get_skill(lang_code: str, default: str = "en") -> Skill:
    """Resolve an inbound locale code to a loaded Skill.

    Identical semantics to ``skills.resolve_skill`` -- this wrapper just
    binds the module-level cache so callers don't have to thread the dict
    through every layer.
    """

    return resolve_skill(_SKILLS_CACHE, lang_code, default=default)
