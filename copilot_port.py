#!/usr/bin/env python3
"""Port local Agent Skills and Claude/Codex agents to GitHub Copilot."""

import argparse
import configparser
import filecmp
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    import yaml
except ModuleNotFoundError:
    sys.exit("Install PyYAML with: python -m pip install -r requirements.txt")


__version__ = "0.1.0"

SKILL_FIELDS = (
    "license", "compatibility", "metadata", "argument-hint",
    "user-invocable", "disable-model-invocation", "context",
)
CLAUDE_TOOLS = {
    "Read": "read",
    "NotebookRead": "read",
    "Grep": "search",
    "Glob": "search",
    "Bash": "execute",
    "PowerShell": "execute",
    "Edit": "edit",
    "MultiEdit": "edit",
    "Write": "edit",
    "NotebookEdit": "edit",
    "WebFetch": "web",
    "WebSearch": "web",
    "TodoWrite": "todo",
    "Agent": "agent",
    "Task": "agent",
}
DISCOVERY_SKIP = {".git", ".venv", "node_modules", "__pycache__"}
HIDDEN_ALLOWED = {".claude", ".codex", ".agents", ".github"}
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", re.M | re.S)
PROVIDER_SYNTAX = re.compile(
    r"\$ARGUMENTS\b|\$\{(?:CLAUDE|CODEX)_[^}]+\}|!`|```!|\.(?:claude|codex)/"
)
RESERVED_NAMES = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])\Z")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class PortError(ValueError):
    """A source or destination needs attention."""


def unique_mapping(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise PortError(f"mapping keys must be unique strings (found {key!r})")
        result[key] = value
    return result


class FrontmatterLoader(yaml.SafeLoader):
    # Frontmatter uses YAML 1.2 spelling for implicit booleans and treats dates as text.
    yaml_implicit_resolvers = {
        initial: [
            (tag, pattern) for tag, pattern in resolvers
            if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}
        ]
        for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def construct_mapping(self, node, deep=False):
        return unique_mapping(self.construct_pairs(node, deep=deep))


FrontmatterLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"(?:true|false)\Z", re.I), "tTfF"
)


@dataclass
class Definition:
    source: Path
    kind: str
    metadata: dict
    body: str
    name: str
    slug: str
    warnings: list[str] = field(default_factory=list)
    body_line: int | None = None
    openai_omitted: bool = False


@dataclass
class Output:
    definition: Definition
    path: Path
    content: bytes
    resources: dict[Path, Path] = field(default_factory=dict)

    @property
    def is_skill(self):
        return self.definition.kind == "skill"


def is_link(path):
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            return path.lstat().st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
        except FileNotFoundError:
            return False
    return False


def walk_error(error):
    raise error


def contains_skill(path):
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(
            name for name in dirs
            if name not in DISCOVERY_SKIP and not is_link(Path(root) / name)
        )
        if "SKILL.md" in files:
            return True
    return False


def discover(inputs, github):
    """Find whole skill bundles and source agents in conventional agents directories."""
    results = set()
    skipped = set()
    excluded = {github / "skills", github / "agents"}
    for path in inputs:
        if is_link(path):
            raise PortError(f"{path}: select the real source instead of a link")
        path = path.resolve(strict=True)
        if path.is_file():
            if path.suffix.lower() not in {".md", ".toml"}:
                raise PortError(f"{path}: expected SKILL.md, a Claude .md agent, or a Codex .toml agent")
            results.add(path)
            continue
        if not path.is_dir():
            raise PortError(f"{path}: expected a regular file or directory")
        for root, dirs, files in os.walk(path, onerror=walk_error):
            directory = Path(root)
            if (directory in excluded or
                    directory.parts[-2:] == (".github", "agents")):
                dirs.clear()
                continue
            if "SKILL.md" in files:
                results.add(directory / "SKILL.md")
                dirs.clear()
                continue
            keep = []
            for name in dirs:
                child = directory / name
                if name in DISCOVERY_SKIP or is_link(child):
                    continue
                if name.startswith(".") and name not in HIDDEN_ALLOWED:
                    # Hidden directories are opt-in; report ones that bundle a skill.
                    if contains_skill(child):
                        skipped.add(child)
                    continue
                keep.append(name)
            dirs[:] = sorted(keep)
            if "agents" in directory.parts:
                for name in files:
                    file = directory / name
                    if (file.suffix.lower() in {".md", ".toml"} and
                            file.stem.lower() not in {"readme", "agents", "license", "changelog"} and
                            not name.lower().endswith(".agent.md")):
                        results.add(file)
    if not results:
        raise PortError("no SKILL.md bundles or Claude/Codex agents found")
    return sorted(results), sorted(skipped)


def required_text(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise PortError(f"{field_name} must be a nonempty string")
    return value


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64].rstrip("-")
    if not slug:
        raise PortError("name must contain at least one ASCII letter or digit")
    return f"port-{slug}" if RESERVED_NAMES.fullmatch(slug) else slug


def read_definition(path):
    if is_link(path) or not path.is_file():
        raise PortError("expected a regular source file")
    text = path.read_bytes().decode("utf-8-sig")
    if path.suffix.lower() == ".toml":
        kind = "codex"
        metadata = tomllib.loads(text)
        body = required_text(metadata.get("developer_instructions"), "developer_instructions")
        body_line = None
    else:
        kind = "skill" if path.name == "SKILL.md" else "claude"
        match = FRONTMATTER.match(text)
        if not match and (kind != "skill" or text.startswith("---")):
            raise PortError("expected YAML frontmatter delimited by ---")
        metadata = yaml.load(match[1], Loader=FrontmatterLoader) if match else {}
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise PortError("frontmatter must be a YAML mapping")
        body = text[match.end():] if match else text
        body_line = match[0].count("\n") + 1 if match else 1

    default_name = path.parent.name if kind == "skill" else None
    name = required_text(metadata.get("name", default_name), "name")
    definition = Definition(path, kind, metadata, body, name, slugify(name), body_line=body_line)
    if definition.slug != name:
        definition.warnings.append(f"normalized name {name!r} to {definition.slug!r}")
    locations = []
    more = 0
    for match in PROVIDER_SYNTAX.finditer(definition.body):
        if len(locations) == 5:
            more += 1
            continue
        line = definition.body.count("\n", 0, match.start()) + 1
        if definition.body_line is None:
            locations.append(f"developer_instructions line {line} `{match[0]}`")
        else:
            locations.append(f"line {definition.body_line + line - 1} `{match[0]}`")
    if locations:
        suffix = f" (+{more} more)" if more else ""
        definition.warnings.append(
            "body contains Claude/Codex syntax or paths that may need edits: "
            + ", ".join(locations) + suffix
        )
    return definition


def description(definition):
    metadata = definition.metadata
    if definition.kind == "skill" and "description" not in metadata:
        value = next((line.strip() for line in definition.body.splitlines() if line.strip()), "")
        definition.warnings.append("used the first nonempty body line as the skill description")
    else:
        value = metadata.get("description")
    value = required_text(value, "description").strip()
    if definition.kind == "skill":
        if "when_to_use" in metadata:
            value += " " + required_text(metadata["when_to_use"], "when_to_use").strip()
        # VS Code measures the description in UTF-16 code units.
        utf16 = value.encode("utf-16-le")
        if len(utf16) > 2048:
            definition.warnings.append("shortened description to the Copilot limit of 1024 characters")
            value = utf16[:2048].decode("utf-16-le", errors="ignore").rstrip()
    return value


def ignored_fields(definition, handled):
    ignored = sorted(definition.metadata.keys() - handled)
    if ignored:
        definition.warnings.append("review unsupported frontmatter or settings: " + ", ".join(ignored))


def skill_fields(definition):
    fields = {}
    for key in SKILL_FIELDS:
        if key not in definition.metadata:
            continue
        value = definition.metadata[key]
        if key == "metadata":
            if not isinstance(value, dict):
                raise PortError("metadata must be a mapping")
            if any(not isinstance(item, str) for item in value.values()):
                definition.warnings.append("omitted non-string values from skill metadata")
            value = {name: item for name, item in sorted(value.items()) if isinstance(item, str)}
        elif key in {"user-invocable", "disable-model-invocation"}:
            # Claude accepts these scalar forms for boolean skill fields.
            boolean = str(value).lower()
            if boolean not in {"true", "false", "yes", "no", "on", "off", "0", "1"}:
                raise PortError(f"{key} must be a boolean")
            value = boolean in {"true", "yes", "on", "1"}
        else:
            required_text(value, key)
        if key == "context":
            if value != "fork":
                raise PortError("context must be fork when specified")
            definition.warnings.append(
                "context: fork requires experimental skill tool support in VS Code"
            )
        fields[key] = value
    ignored_fields(definition, {"name", "description", "when_to_use", *SKILL_FIELDS})
    return fields


def string_list(value, field_name):
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise PortError(f"{field_name} must be a list of nonempty strings")
    return [item.strip() for item in value]


def read_overrides(path):
    if path is None:
        return {}
    overrides = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_mapping)
    if not isinstance(overrides, dict):
        raise PortError("overrides must map generated agent IDs to objects")
    for name, override in overrides.items():
        if not isinstance(override, dict) or override.keys() - {"tools", "model"}:
            raise PortError(f"override for {name!r} may contain tools and model")
        if "tools" in override:
            override["tools"] = string_list(override["tools"], f"{name}.tools")
        if "model" in override:
            required_text(override["model"], f"{name}.model")
    return overrides


def claude_tools(value):
    if not isinstance(value, str):
        return string_list(value, "tools")
    # Split outside parentheses. A scoped entry stays intact and never matches an alias.
    entries = []
    depth = start = 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char == ",":
            entries.append(value[start:index].strip())
            start = index + 1
        if depth < 0:
            raise PortError("unbalanced parentheses in tools")
    if depth:
        raise PortError("unbalanced parentheses in tools")
    entries.append(value[start:].strip())
    return [entry for entry in entries if entry]


def agent_fields(definition, override, skills):
    metadata = definition.metadata
    fields = {"target": "github-copilot"}
    handled = {"name", "description"}
    if metadata.get("model") == "inherit":
        handled.add("model")
    if "model" in override:
        fields["model"] = override["model"]
        handled.add("model")

    features = metadata.get("features")
    shell_disabled = isinstance(features, dict) and features.get("shell_tool") is False
    if "tools" in override:
        fields["tools"] = override["tools"]
        handled.update(("tools", "disallowedTools"))
    elif metadata.get("disallowedTools") or shell_disabled:
        raise PortError("source restricts tools; supply a reviewed tools list in --overrides")
    elif definition.kind == "claude" and "tools" in metadata:
        source_tools = claude_tools(metadata["tools"])
        mapped = [CLAUDE_TOOLS[name] for name in source_tools if name in CLAUDE_TOOLS]
        fields["tools"] = list(dict.fromkeys(mapped))
        handled.add("tools")
        if mapped:
            definition.warnings.append("mapped Claude tools to Copilot aliases; review tool scope")
        unmapped = [name for name in source_tools if name not in CLAUDE_TOOLS]
        if unmapped:
            definition.warnings.append("no tool mapping for " + ", ".join(map(repr, unmapped)))

    if definition.kind == "claude" and "skills" in metadata:
        names = string_list(metadata["skills"], "skills")
        fields["skills"] = list(dict.fromkeys(skills.get(name, name) for name in names))
        handled.add("skills")
        external = [name for name in names if name not in skills]
        if external:
            definition.warnings.append("verify skill names in Copilot: " + ", ".join(external))
    if definition.kind == "codex":
        handled.add("developer_instructions")
    ignored_fields(definition, handled)
    return fields


def bundle_files(root):
    resources = {}
    for parent, dirs, files in os.walk(root, onerror=walk_error):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in dirs + sorted(files):
            if name in {".git", ".DS_Store"}:
                continue
            path = Path(parent) / name
            if is_link(path) or not (path.is_dir() or path.is_file()):
                raise PortError(f"{path}: skill bundles must contain regular files and directories")
            if path.is_file():
                resources[path.relative_to(root)] = path
    return resources


def openai_settings(path):
    try:
        settings = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError):
        return None
    return settings if isinstance(settings, dict) else None


def convert(definition, github, overrides, skills):
    fields = {"name": definition.slug, "description": description(definition)}
    resources = {}
    if definition.kind == "skill":
        path = github / "skills" / definition.slug
        if path.is_relative_to(definition.source.parent):
            raise PortError("destination falls inside the source skill bundle")
        skill = skill_fields(definition)
        resources = bundle_files(definition.source.parent)
        resources.pop(Path("SKILL.md"))
        openai = resources.pop(Path("agents/openai.yaml"), None)
        if openai is not None:
            definition.openai_omitted = True
            settings = openai_settings(openai)
            if settings is None:
                definition.warnings.append(
                    "could not read agents/openai.yaml; review Codex settings")
            else:
                policy = settings.get("policy")
                if (isinstance(policy, dict) and
                        policy.get("allow_implicit_invocation") is False):
                    if skill.get("disable-model-invocation") is False:
                        definition.warnings.append(
                            "agents/openai.yaml disallows implicit invocation but frontmatter "
                            "sets disable-model-invocation: false; kept frontmatter")
                    elif "disable-model-invocation" not in skill:
                        # Keep the injected field in its normal SKILL_FIELDS position.
                        skill = {
                            key: skill.get(key, True)
                            for key in SKILL_FIELDS
                            if key in skill or key == "disable-model-invocation"
                        }
                        definition.warnings.append(
                            "mapped agents/openai.yaml allow_implicit_invocation: false "
                            "to disable-model-invocation: true")
                if "dependencies" in settings:
                    definition.warnings.append(
                        "agents/openai.yaml declares dependencies; review tool and MCP "
                        "availability in Copilot")
        fields.update(skill)
    else:
        path = github / "agents" / f"{definition.slug}.agent.md"
        fields.update(agent_fields(definition, overrides.get(definition.slug, {}), skills))
    header = yaml.safe_dump(fields, allow_unicode=True, sort_keys=False, width=4096)
    content = f"---\n{header}---\n{definition.body}".encode("utf-8")
    return Output(definition, path, content, resources)


def preflight(output):
    path = output.path
    for parent in (path.parent.parent, path.parent):
        if is_link(parent) or (parent.exists() and not parent.is_dir()):
            raise PortError(f"{parent}: destination parent must be a real directory")
    if path.parent.is_dir():
        names = {path.name.casefold()}
        if not output.is_skill:
            names.add(f"{output.definition.slug}.md")
        for existing in sorted(path.parent.iterdir()):
            if existing.name.casefold() in names and existing.name != path.name:
                raise PortError(f"{existing}: name conflicts with {path.name}")
    if is_link(path):
        raise PortError(f"{path}: destination is a link")
    if not path.exists():
        return "CREATE"
    if output.is_skill and path.is_dir():
        existing_files = bundle_files(path)
        manifest = existing_files.pop(Path("SKILL.md"), None)
        if (manifest and manifest.read_bytes() == output.content and
                existing_files.keys() == output.resources.keys() and
                all(filecmp.cmp(source, existing_files[relative], shallow=False)
                    and executable_bits(source) == executable_bits(existing_files[relative])
                    for relative, source in output.resources.items())):
            return "UNCHANGED"
    if not output.is_skill and path.is_file() and path.read_bytes() == output.content:
        return "UNCHANGED"
    raise PortError(f"{path}: destination already exists with different content")


def executable_bits(path):
    return path.stat().st_mode & 0o111


def publish(path, content):
    # A same-directory hard link publishes a complete file and fails if the name exists.
    with tempfile.TemporaryDirectory(prefix=".copilot-port-", dir=path.parent) as directory:
        staged = Path(directory) / "content"
        staged.write_bytes(content)
        os.link(staged, path)


def output_digest(output):
    if not output.is_skill:
        return hashlib.sha256(output.content).hexdigest()
    digest = hashlib.sha256()
    contents = {relative: source.read_bytes()
                for relative, source in output.resources.items()}
    contents[Path("SKILL.md")] = output.content
    for relative in sorted(contents, key=Path.as_posix):
        digest.update(relative.as_posix().encode() + b"\0" +
                      hashlib.sha256(contents[relative]).digest())
    return digest.hexdigest()


def git_checkout(source):
    """Return (gitdir, work tree) for a source inside a checkout, else (None, None)."""
    for tree in source.parents:
        marker = tree / ".git"
        try:
            if marker.is_dir():
                return marker, tree
            if marker.is_file():
                match = re.match(r"gitdir:[ \t]*(.+?)[ \t]*$", marker.read_text(errors="replace"), re.M)
                return ((tree / match[1]).resolve(), tree) if match else (None, None)
        except OSError:
            continue
    return None, None


def git_common(gitdir):
    try:
        commondir = gitdir / "commondir"
        if commondir.is_file():
            return (gitdir / commondir.read_text().strip()).resolve()
    except OSError:
        pass
    return gitdir


def git_head(gitdir, common):
    try:
        head = (gitdir / "HEAD").read_text(errors="replace").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head if COMMIT.fullmatch(head) else None
    ref = head[4:].strip()
    for loose in (gitdir / ref, common / ref):
        try:
            if loose.is_file():
                commit = loose.read_text(errors="replace").strip()
                if COMMIT.fullmatch(commit):
                    return commit
        except OSError:
            pass
    try:
        packed = (common / "packed-refs").read_text(errors="replace")
    except OSError:
        return None
    for line in packed.splitlines():
        if line.startswith(("#", "^")):
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref and COMMIT.fullmatch(sha):
            return sha
    return None


def git_remote(common):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(common / "config", encoding="utf-8")
        url = parser.get('remote "origin"', "url")
        parts = urlsplit(url)
    except (OSError, ValueError, configparser.Error):
        return None
    if "@" in parts.netloc:
        url = urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))
    return url


def manifest_entry(output):
    definition = output.definition
    entry = {"kind": definition.kind, "sha256": output_digest(output)}
    gitdir, tree = git_checkout(definition.source)
    if gitdir is None:
        try:
            entry["source"] = "~/" + definition.source.relative_to(Path.home()).as_posix()
        except ValueError:
            entry["source"] = definition.source.as_posix()
        return entry
    common = git_common(gitdir)
    commit = git_head(gitdir, common)
    if commit is None:
        definition.warnings.append(
            "could not resolve the git commit for the source checkout")
    else:
        entry["commit"] = commit
    url = git_remote(common)
    if url:
        entry["repository"] = url
    entry["source"] = definition.source.relative_to(tree).as_posix()
    return entry


def read_manifest(path):
    if is_link(path) or (path.exists() and not path.is_file()):
        raise PortError(f"{path}: copilot-port.json must be a regular file")
    if not path.exists():
        return {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PortError(f"{path}: copilot-port.json is not valid JSON") from error
    entries = manifest.get("customizations", {}) if isinstance(manifest, dict) else None
    if not isinstance(entries, dict):
        raise PortError(f"{path}: copilot-port.json must map customization paths to objects")
    return entries


def replace(path, content):
    # A same-directory rename publishes a complete file and replaces the old name.
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".copilot-port-")
    try:
        with os.fdopen(fd, "wb") as staged:
            staged.write(content)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def install(output):
    path = output.path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not output.is_skill:
        publish(path, output.content)
        return
    path.mkdir()
    try:
        for relative, source in output.resources.items():
            destination = path / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(destination.stat().st_mode | executable_bits(source))
        publish(path / "SKILL.md", output.content)
    except BaseException:
        shutil.rmtree(path)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path, help="source files, bundles, or repository checkouts")
    parser.add_argument("--target", type=Path, default=Path.cwd(), help="Copilot workspace root (default: cwd)")
    parser.add_argument("--overrides", type=Path, help="JSON file with reviewed per-agent tools and models")
    parser.add_argument("--write", action="store_true", help="create planned files (default: preview)")
    parser.add_argument("--strict", action="store_true", help="treat migration warnings as errors")
    args = parser.parse_args()

    try:
        target = args.target.resolve()
        if target.exists() and not target.is_dir():
            raise PortError(f"{target}: target must be a directory")
        github = target / ".github"
        overrides = read_overrides(args.overrides)
        manifest_path = github / "copilot-port.json"
        customizations = read_manifest(manifest_path)
        sources, skipped = discover(args.sources, github)
    except (OSError, ValueError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1

    for path in skipped:
        print(f"NOTE skipped hidden directory {path}; pass it explicitly to include",
              file=sys.stderr)

    definitions = []
    failed = False
    for path in sources:
        try:
            definitions.append(read_definition(path))
        except (OSError, ValueError, yaml.YAMLError) as error:
            print(f"ERROR {path}: {error}", file=sys.stderr)
            failed = True

    skills = {item.name: item.slug for item in definitions if item.kind == "skill"}
    outputs = {}
    pending = []
    for definition in definitions:
        try:
            output = convert(definition, github, overrides, skills)
            if output.path in outputs:
                raise PortError(f"{output.path}: also generated from {outputs[output.path]}")
            outputs[output.path] = definition.source
            action = preflight(output)
            print(f"{action} {output.path.relative_to(target)} <- {definition.source}")
            customizations[output.path.relative_to(target).as_posix()] = \
                manifest_entry(output)
            if action == "CREATE":
                pending.append(output)
        except (OSError, ValueError, yaml.YAMLError) as error:
            print(f"ERROR {definition.source}: {error}", file=sys.stderr)
            failed = True
        for warning in definition.warnings:
            print(f"WARNING {definition.source}: {warning}", file=sys.stderr)
            failed |= args.strict

    omitted = sum(item.openai_omitted for item in definitions)
    if omitted:
        print(f"NOTE omitted Codex-only agents/openai.yaml from {omitted} skill(s)",
              file=sys.stderr)
    if failed:
        return 1

    manifest_bytes = (json.dumps(
        {"customizations": customizations,
         "generator": f"copilot-port {__version__}"},
        ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if not manifest_path.exists():
        manifest_action = "CREATE"
    elif manifest_path.read_bytes() == manifest_bytes:
        manifest_action = "UNCHANGED"
    else:
        manifest_action = "UPDATE"
    print(f"{manifest_action} {manifest_path.relative_to(target)}")

    if args.write:
        created = []
        failure = None
        for output in pending:
            try:
                install(output)
                created.append(output.path)
            except OSError as error:
                failure = output.path, error
                break
        if failure is None and manifest_action != "UNCHANGED":
            try:
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                replace(manifest_path, manifest_bytes)
            except OSError as error:
                failure = manifest_path, error
        if failure is not None:
            path, error = failure
            for created_path in reversed(created):
                try:
                    if created_path.is_dir():
                        shutil.rmtree(created_path)
                    else:
                        created_path.unlink()
                except OSError as rollback_error:
                    print(f"ERROR rollback {created_path}: {rollback_error}",
                          file=sys.stderr)
            print(f"ERROR {path}: {error}", file=sys.stderr)
            print(f"Rolled back {len(created)} customizations.", file=sys.stderr)
            return 1
        print(f"Created {len(pending)} of {len(outputs)} customizations.")
    else:
        print(f"Previewed {len(outputs)} customizations. Pass --write to create {len(pending)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
