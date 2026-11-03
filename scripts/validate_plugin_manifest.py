#!/usr/bin/env python3
"""Repository-owned structural check for .codex-plugin/plugin.json.

Used when the Codex system plugin validator is not installed on the host.
It checks only the rules this repository relies on; it does not claim parity
with the Codex system validator.
"""

import json
import re
import sys
from pathlib import Path

import yaml

MANIFEST_RELATIVE = Path(".codex-plugin") / "plugin.json"
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
REQUIRED_STRINGS = ("name", "version", "description", "license")
INTERFACE_STRINGS = (
    "displayName",
    "shortDescription",
    "longDescription",
    "developerName",
    "category",
)
MAX_DEFAULT_PROMPTS = 3
MAX_PROMPT_LENGTH = 128


def _is_text(value):
    return isinstance(value, str) and value.strip() != ""


def _check_string_list(errors, owner, key, value):
    if not isinstance(value, list) or not value:
        errors.append(f"{owner}.{key} must be a non-empty list")
        return []
    if not all(_is_text(item) for item in value):
        errors.append(f"{owner}.{key} entries must be non-empty strings")
        return []
    if len(set(value)) != len(value):
        errors.append(f"{owner}.{key} entries must be unique")
    return value


def _skill_frontmatter_name(skill_md):
    match = FRONTMATTER_PATTERN.match(skill_md.read_text(encoding="utf-8"))
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return data.get("name") if isinstance(data, dict) else None


def _check_skills(errors, root, skills_value):
    if not _is_text(skills_value) or not skills_value.startswith("./"):
        errors.append("skills must be a relative path starting with './'")
        return
    skills_dir = (root / skills_value).resolve()
    if root.resolve() not in skills_dir.parents and skills_dir != root.resolve():
        errors.append("skills path must stay inside the repository")
        return
    if not skills_dir.is_dir():
        errors.append(f"skills directory not found: {skills_value}")
        return
    skill_dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())
    if not skill_dirs:
        errors.append(f"skills directory has no skills: {skills_value}")
    for skill_dir in skill_dirs:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            errors.append(f"missing SKILL.md in skills/{skill_dir.name}")
            continue
        name = _skill_frontmatter_name(skill_md)
        if name != skill_dir.name:
            errors.append(
                f"skills/{skill_dir.name}/SKILL.md frontmatter name "
                f"{name!r} must match its directory name"
            )


def _check_interface(errors, interface):
    if not isinstance(interface, dict):
        errors.append("interface must be an object")
        return
    for key in INTERFACE_STRINGS:
        if not _is_text(interface.get(key)):
            errors.append(f"interface.{key} must be a non-empty string")
    _check_string_list(errors, "interface", "capabilities", interface.get("capabilities"))
    prompts = _check_string_list(
        errors, "interface", "defaultPrompt", interface.get("defaultPrompt")
    )
    if len(prompts) > MAX_DEFAULT_PROMPTS:
        errors.append(f"interface.defaultPrompt allows at most {MAX_DEFAULT_PROMPTS} prompts")
    for prompt in prompts:
        if len(prompt) > MAX_PROMPT_LENGTH:
            errors.append(
                f"interface.defaultPrompt entry exceeds {MAX_PROMPT_LENGTH} characters"
            )


def validate_plugin(root):
    """Return a list of error strings; empty means the manifest is valid."""
    root = Path(root)
    manifest_path = root / MANIFEST_RELATIVE
    if not manifest_path.is_file():
        return [f"manifest not found: {MANIFEST_RELATIVE}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"manifest is not valid JSON: {exc}"]
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]

    errors = []
    for key in REQUIRED_STRINGS:
        if not _is_text(manifest.get(key)):
            errors.append(f"{key} must be a non-empty string")
    if _is_text(manifest.get("name")) and not NAME_PATTERN.match(manifest["name"]):
        errors.append("name must be lowercase kebab-case")
    if _is_text(manifest.get("version")) and not SEMVER_PATTERN.match(manifest["version"]):
        errors.append("version must be semantic (MAJOR.MINOR.PATCH)")
    author = manifest.get("author")
    if not isinstance(author, dict) or not _is_text(author.get("name")):
        errors.append("author.name must be a non-empty string")
    if "keywords" in manifest:
        _check_string_list(errors, "manifest", "keywords", manifest["keywords"])
    _check_skills(errors, root, manifest.get("skills"))
    _check_interface(errors, manifest.get("interface"))
    return errors


def main(argv):
    if len(argv) != 2:
        print("usage: validate_plugin_manifest.py <plugin-root>", file=sys.stderr)
        return 2
    errors = validate_plugin(argv[1])
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 1
    print("Plugin manifest is valid (repository-owned check).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
