import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import copilot_port


def write_skill(root, name, frontmatter="", body="# Instructions\n"):
    directory = Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    header = f"name: {name}\ndescription: {name} does things.\n{frontmatter}"
    path.write_text(f"---\n{header}---\n{body}", encoding="utf-8")
    return path


def write_agent(root, name, frontmatter="", body="# Role\n"):
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    header = f"name: {name}\ndescription: {name} agent.\n{frontmatter}"
    path.write_text(f"---\n{header}---\n{body}", encoding="utf-8")
    return path


def write_codex(root, name, instructions="Do the thing.\n"):
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(
        f'name = "{name}"\ndescription = "{name} agent."\n'
        f'developer_instructions = """{instructions}"""\n',
        encoding="utf-8")
    return path


class CopilotPortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "src"
        self.root.mkdir()
        self.target = Path(self.temp.name) / "target"
        self.target.mkdir()
        self.github = self.target / ".github"

    def run_port(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "copilot_port.py"), *map(str, args)],
            capture_output=True, text=True)

    def header_of(self, path):
        match = copilot_port.FRONTMATTER.match(path.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, path)
        return match[1]

    def manifest_of(self, target=None):
        path = (target or self.target) / ".github" / "copilot-port.json"
        return json.loads(path.read_text(encoding="utf-8"))["customizations"]

    def make_checkout(self, worktree, head, *, loose=None, packed=None,
                      config=None):
        git = worktree / ".git"
        git.mkdir(parents=True)
        (git / "HEAD").write_text(head)
        for ref, sha in (loose or {}).items():
            path = git / ref
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sha + "\n")
        if packed is not None:
            (git / "packed-refs").write_text(packed)
        if config is not None:
            (git / "config").write_text(config)
        return git

    def test_skill_conversion(self):
        skill = write_skill(
            self.root, "helper",
            frontmatter="license: MIT\nwhen_to_use: Use it when testing.\n",
            body="# Helper\n\nBody stays.\n")
        script = skill.parent / "scripts" / "run.sh"
        script.parent.mkdir()
        script.write_bytes(b"#!/bin/sh\necho hi\n")
        script.chmod(0o755)

        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.github / "skills" / "helper" / "SKILL.md"
        self.assertTrue(output.is_file())
        text = output.read_text(encoding="utf-8")
        self.assertIn("name: helper\n", text)
        self.assertIn("description: helper does things. Use it when testing.\n", text)
        self.assertTrue(text.startswith("---\n"))
        self.assertTrue(output.read_bytes().endswith(b"# Helper\n\nBody stays.\n"))
        copied = output.parent / "scripts" / "run.sh"
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_bytes(), script.read_bytes())
        self.assertEqual(copied.stat().st_mode & 0o111, 0o111)

    def test_name_normalization(self):
        write_skill(self.root, "Fancy Skill!!")
        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING", result.stderr)
        self.assertIn("normalized name 'Fancy Skill!!' to 'fancy-skill'", result.stderr)
        self.assertTrue((self.github / "skills" / "fancy-skill" / "SKILL.md").is_file())

    def test_reserved_name(self):
        write_skill(self.root, "con")
        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.github / "skills" / "port-con" / "SKILL.md").is_file())

    def test_hidden_directories(self):
        write_skill(self.root / ".system", "x")
        write_skill(self.root / ".agents" / "skills", "y")
        write_agent(self.root / ".claude" / "agents", "a")
        write_skill(self.root, "plain")
        (self.root / ".empty").mkdir()

        sources, skipped = copilot_port.discover([self.root], self.github)
        self.assertEqual(sorted(p.name for p in sources),
                         ["SKILL.md", "SKILL.md", "a.md"])
        self.assertEqual(skipped, [self.root.resolve() / ".system"])

        result = self.run_port(self.root, "--target", self.target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"NOTE skipped hidden directory {self.root.resolve() / '.system'}; "
            "pass it explicitly to include", result.stderr)
        self.assertNotIn(".empty", result.stderr)
        self.assertNotIn(".system", result.stdout)
        self.assertIn("skills/plain", result.stdout)
        self.assertIn("skills/y", result.stdout)
        self.assertIn("agents/a.agent.md", result.stdout)

        explicit = self.run_port(self.root / ".system", "--target", self.target,
                                 "--strict")
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertIn("skills/x", explicit.stdout)
        self.assertNotIn("NOTE skipped hidden directory", explicit.stderr)

    def test_hidden_note_alone_passes_strict(self):
        write_skill(self.root / ".system", "x")
        write_skill(self.root, "plain")
        result = self.run_port(self.root, "--target", self.target, "--strict")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NOTE skipped hidden directory", result.stderr)

    def test_provider_syntax_lines(self):
        body = ("first\n$ARGUMENTS here\nmiddle\nuse .claude/ paths\n"
                + "$ARGUMENTS\n" * 5)
        path = write_skill(self.root, "locate", body=body)
        definition = copilot_port.read_definition(path)
        warning = next(w for w in definition.warnings if "syntax or paths" in w)
        # Frontmatter occupies lines 1-4, so the body starts at source line 5.
        self.assertIn("line 6 `$ARGUMENTS`", warning)
        self.assertIn("line 8 `.claude/`", warning)
        self.assertEqual(warning.count("line "), 5)
        self.assertTrue(warning.endswith("(+2 more)"))

    def test_provider_syntax_codex(self):
        path = write_codex(self.root, "coded",
                           instructions="one\ntwo\nsee .codex/ for details\n")
        definition = copilot_port.read_definition(path)
        warning = next(w for w in definition.warnings if "syntax or paths" in w)
        self.assertIn("developer_instructions line 3 `.codex/`", warning)

    def test_openai_yaml_mapped(self):
        skill = write_skill(
            self.root, "coded", frontmatter="license: MIT\n",
            body="# Coded\n")
        agents = skill.parent / "agents"
        agents.mkdir()
        (agents / "openai.yaml").write_text(
            "interface:\n  display_name: Coded\n"
            "policy:\n  allow_implicit_invocation: false\n")
        (agents / "notes.txt").write_text("keep me\n")

        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "mapped agents/openai.yaml allow_implicit_invocation: false to "
            "disable-model-invocation: true", result.stderr)
        self.assertIn(
            "NOTE omitted Codex-only agents/openai.yaml from 1 skill(s)",
            result.stderr)
        output = self.github / "skills" / "coded"
        self.assertFalse((output / "agents" / "openai.yaml").exists())
        self.assertEqual((output / "agents" / "notes.txt").read_text(), "keep me\n")
        header = self.header_of(output / "SKILL.md")
        self.assertIn("disable-model-invocation: true\n", header)
        self.assertLess(header.index("license"), header.index("disable-model-invocation"))
        # Re-running reports UNCHANGED since the omission is deterministic.
        again = self.run_port(self.root, "--target", self.target)
        self.assertIn("UNCHANGED", again.stdout)

    def test_openai_yaml_conflict_and_dependencies(self):
        first = write_skill(
            self.root / "a", "kept",
            frontmatter="disable-model-invocation: false\n")
        (first.parent / "agents").mkdir()
        (first.parent / "agents" / "openai.yaml").write_text(
            "policy:\n  allow_implicit_invocation: false\n"
            "dependencies: [codex-cli]\n")
        second = write_skill(self.root / "b", "broken")
        (second.parent / "agents").mkdir()
        (second.parent / "agents" / "openai.yaml").write_text("interface: [unclosed\n")

        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "agents/openai.yaml disallows implicit invocation but frontmatter sets "
            "disable-model-invocation: false; kept frontmatter", result.stderr)
        self.assertIn(
            "agents/openai.yaml declares dependencies; review tool and MCP "
            "availability in Copilot", result.stderr)
        self.assertIn(
            "could not read agents/openai.yaml; review Codex settings", result.stderr)
        self.assertIn(
            "NOTE omitted Codex-only agents/openai.yaml from 2 skill(s)",
            result.stderr)
        header = self.header_of(self.github / "skills" / "kept" / "SKILL.md")
        self.assertIn("disable-model-invocation: false\n", header)

    def test_openai_yaml_no_warning_without_policy(self):
        skill = write_skill(self.root, "quiet")
        (skill.parent / "agents").mkdir()
        (skill.parent / "agents" / "openai.yaml").write_text(
            "interface:\n  display_name: Quiet\n")
        result = self.run_port(self.root, "--target", self.target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("WARNING", result.stderr)
        self.assertIn(
            "NOTE omitted Codex-only agents/openai.yaml from 1 skill(s)",
            result.stderr)

    def test_claude_tools(self):
        agent = write_agent(self.root, "reviewer",
                            frontmatter='tools: "Read, Grep, Bash(git:*)"\n')
        result = self.run_port(agent, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no tool mapping for 'Bash(git:*)'", result.stderr)
        header = self.header_of(self.github / "agents" / "reviewer.agent.md")
        self.assertIn("tools:\n- read\n- search\n", header)

    def test_disallowed_tools_requires_overrides(self):
        agent = write_agent(self.root, "locked",
                            frontmatter="disallowedTools:\n- Write\n")
        result = self.run_port(agent, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 1)
        self.assertIn("source restricts tools", result.stderr)
        self.assertFalse(self.github.exists())

        overrides = Path(self.temp.name) / "overrides.json"
        overrides.write_text(json.dumps(
            {"locked": {"tools": ["read"], "model": "gpt-5"}}))
        result = self.run_port(agent, "--target", self.target, "--write",
                               "--overrides", overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        header = self.header_of(self.github / "agents" / "locked.agent.md")
        self.assertIn("tools:\n- read\n", header)
        self.assertIn("model: gpt-5\n", header)

    def test_codex_toml_agent(self):
        path = write_codex(self.root, "codexer",
                           instructions="First line.\nSecond line.\n")
        result = self.run_port(path, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.github / "agents" / "codexer.agent.md"
        text = output.read_text(encoding="utf-8")
        self.assertIn("target: github-copilot\n", text)
        self.assertTrue(text.endswith("First line.\nSecond line.\n"))

    def test_preview_write_unchanged_and_conflicts(self):
        skill = write_skill(self.root / "one", "alpha", body="# Alpha\n")
        write_skill(self.root / "two", "beta", body="# Beta\n")

        preview = self.run_port(self.root, "--target", self.target)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertFalse(self.github.exists())
        self.assertIn("Previewed 2 customizations. Pass --write to create 2.",
                      preview.stdout)

        written = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertTrue((self.github / "skills" / "alpha" / "SKILL.md").is_file())

        rerun = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertIn("UNCHANGED", rerun.stdout)
        self.assertIn("Created 0 of 2", rerun.stdout)

        skill.write_text(skill.read_text() + "\nchanged\n")
        conflict = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(conflict.returncode, 1)
        self.assertIn("already exists with different content", conflict.stderr)

    def test_no_partial_writes_on_conflict(self):
        stale = write_skill(self.root / "one", "alpha", body="# Alpha\n")
        write_skill(self.root / "two", "beta", body="# Beta\n")
        first = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(first.returncode, 0, first.stderr)

        write_skill(self.root / "three", "gamma", body="# Gamma\n")
        stale.write_text(stale.read_text() + "\nchanged\n")
        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists with different content", result.stderr)
        self.assertFalse((self.github / "skills" / "gamma").exists())

    def test_case_only_name_conflict(self):
        write_skill(self.root, "foo")
        (self.github / "skills").mkdir(parents=True)
        (self.github / "skills" / "Foo").mkdir()
        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 1)
        self.assertIn("name conflicts with foo", result.stderr)

    def test_description_truncated(self):
        write_skill(self.root, "longdesc",
                    frontmatter="", body="# x\n")
        # Rewrite with an oversized description.
        path = self.root / "longdesc" / "SKILL.md"
        path.write_text(
            "---\nname: longdesc\ndescription: " + "x" * 1100 + "\n---\nbody\n")
        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shortened description", result.stderr)
        header = self.header_of(self.github / "skills" / "longdesc" / "SKILL.md")
        description = re.search(r"^description: (.*)$", header, re.M)[1]
        self.assertLessEqual(len(description), 1024)

    def test_yaml_yes_stays_string(self):
        skill = write_skill(self.root, "meta",
                            frontmatter="metadata:\n  author: yes\n")
        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        header = self.header_of(self.github / "skills" / "meta" / "SKILL.md")
        fields = copilot_port.yaml.safe_load(header)
        self.assertEqual(fields["metadata"]["author"], "yes")

    def test_duplicate_frontmatter_keys_error(self):
        directory = self.root / "dup"
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            "---\nname: dup\nname: other\ndescription: d\n---\nbody\n")
        result = self.run_port(self.root, "--target", self.target)
        self.assertEqual(result.returncode, 1)
        self.assertIn("mapping keys must be unique", result.stderr)

    def test_strict_fails_on_warning(self):
        write_skill(self.root, "Weird Name!")
        result = self.run_port(self.root, "--target", self.target, "--strict")
        self.assertEqual(result.returncode, 1)
        self.assertIn("WARNING", result.stderr)
        self.assertFalse(self.github.exists())

    def test_manifest_git_loose_ref(self):
        sha = "de0f1a2b" * 5
        repo = self.root / "repo"
        self.make_checkout(
            repo, "ref: refs/heads/main\n",
            loose={"refs/heads/main": sha},
            config='[remote "origin"]\n\turl = https://user:tok@github.com/o/r.git\n')
        agent = write_agent(repo / "agents", "docs-reviewer")
        result = self.run_port(agent, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.manifest_of()[".github/agents/docs-reviewer.agent.md"]
        self.assertEqual(entry["kind"], "claude")
        self.assertEqual(entry["commit"], sha)
        self.assertEqual(entry["repository"], "https://github.com/o/r.git")
        self.assertEqual(entry["source"], "agents/docs-reviewer.md")
        written = self.github / "agents" / "docs-reviewer.agent.md"
        self.assertEqual(entry["sha256"],
                         hashlib.sha256(written.read_bytes()).hexdigest())

    def test_manifest_git_packed_refs(self):
        sha = "ab" * 20
        repo = self.root / "repo"
        self.make_checkout(
            repo, "ref: refs/heads/main\n",
            packed=f"# pack-refs with peeled\n{sha} refs/heads/main\n^{'c' * 40}\n")
        agent = write_agent(repo / "agents", "packed")
        result = self.run_port(agent, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.manifest_of()[".github/agents/packed.agent.md"]["commit"], sha)

    def test_manifest_git_detached(self):
        sha = "ef" * 20
        repo = self.root / "repo"
        self.make_checkout(repo, sha + "\n")
        agent = write_agent(repo / "agents", "detached")
        result = self.run_port(agent, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.manifest_of()[".github/agents/detached.agent.md"]
        self.assertEqual(entry["commit"], sha)
        self.assertNotIn("repository", entry)

    def test_manifest_git_worktree(self):
        sha = "12" * 20
        worktree = self.root / "wt"
        common = self.root / "repo" / ".git"
        gitdir = common / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (common / "refs" / "heads").mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n")
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
        (common / "refs" / "heads" / "main").write_text(sha + "\n")
        (common / "config").write_text(
            '[remote "origin"]\n\turl = git@github.com:o/r.git\n')
        worktree.mkdir(exist_ok=True)
        (worktree / ".git").write_text("gitdir: ../repo/.git/worktrees/wt\n")
        agent = write_agent(worktree / "agents", "worked")
        result = self.run_port(agent, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.manifest_of()[".github/agents/worked.agent.md"]
        self.assertEqual(entry["commit"], sha)
        self.assertEqual(entry["repository"], "git@github.com:o/r.git")
        self.assertEqual(entry["source"], "agents/worked.md")

    def test_manifest_git_unborn(self):
        repo = self.root / "repo"
        self.make_checkout(repo, "ref: refs/heads/main\n")
        agent = write_agent(repo / "agents", "unborn")
        result = self.run_port(agent, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not resolve the git commit", result.stderr)
        entry = self.manifest_of()[".github/agents/unborn.agent.md"]
        self.assertNotIn("commit", entry)
        self.assertNotIn("repository", entry)
        self.assertEqual(entry["source"], "agents/unborn.md")

    def test_manifest_no_git(self):
        skill = write_skill(self.root, "nogit")
        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.manifest_of()[".github/skills/nogit"]
        self.assertNotIn("commit", entry)
        self.assertNotIn("repository", entry)
        resolved = skill.resolve()
        try:
            expected = "~/" + resolved.relative_to(Path.home()).as_posix()
        except ValueError:
            expected = resolved.as_posix()
        self.assertEqual(entry["source"], expected)
        digest = hashlib.sha256()
        contents = {Path("SKILL.md"): (
            self.github / "skills" / "nogit" / "SKILL.md").read_bytes()}
        for relative in sorted(contents, key=Path.as_posix):
            digest.update(relative.as_posix().encode() + b"\0" +
                          hashlib.sha256(contents[relative]).digest())
        self.assertEqual(entry["sha256"], digest.hexdigest())

    def test_manifest_merge(self):
        self.github.mkdir(parents=True)
        existing = {
            "customizations": {
                ".github/skills/other": {
                    "kind": "skill", "sha256": "0" * 64, "source": "elsewhere"
                }
            },
            "generator": f"copilot-port {copilot_port.__version__}",
        }
        (self.github / "copilot-port.json").write_text(json.dumps(existing))
        write_skill(self.root, "alpha")
        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("UPDATE .github/copilot-port.json", result.stdout)
        merged = self.manifest_of()
        self.assertEqual(merged[".github/skills/other"]["source"], "elsewhere")
        self.assertIn(".github/skills/alpha", merged)

    def test_manifest_invalid_json(self):
        self.github.mkdir(parents=True)
        (self.github / "copilot-port.json").write_text("not json")
        write_skill(self.root, "alpha")
        result = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not valid JSON", result.stderr)
        self.assertFalse((self.github / "skills").exists())
        self.assertEqual(
            (self.github / "copilot-port.json").read_text(), "not json")

    def test_manifest_preview_and_unchanged(self):
        write_skill(self.root, "alpha")
        preview = self.run_port(self.root, "--target", self.target)
        self.assertIn("CREATE .github/copilot-port.json", preview.stdout)
        self.assertFalse((self.github / "copilot-port.json").exists())

        written = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(written.returncode, 0, written.stderr)
        manifest = self.github / "copilot-port.json"
        before = (manifest.stat().st_mtime_ns, manifest.read_bytes())
        rerun = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertIn("UNCHANGED .github/copilot-port.json", rerun.stdout)
        self.assertEqual(
            before, (manifest.stat().st_mtime_ns, manifest.read_bytes()))

    def test_manifest_sha_tracks_resources(self):
        skill = write_skill(self.root, "alpha")
        (skill.parent / "data.txt").write_text("v1")
        second = Path(self.temp.name) / "t2"
        second.mkdir()
        first = self.run_port(self.root, "--target", self.target, "--write")
        self.assertEqual(first.returncode, 0, first.stderr)
        sha1 = self.manifest_of()[".github/skills/alpha"]["sha256"]
        (skill.parent / "data.txt").write_text("v2")
        second_run = self.run_port(self.root, "--target", second, "--write")
        self.assertEqual(second_run.returncode, 0, second_run.stderr)
        sha2 = self.manifest_of(second)[".github/skills/alpha"]["sha256"]
        self.assertNotEqual(sha1, sha2)

    def call_main(self, patcher):
        argv = ["copilot_port.py", str(self.root), "--target", str(self.target),
                "--write"]
        with patcher, mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = copilot_port.main()
        return code, err.getvalue()

    def test_rollback_on_install_failure(self):
        write_agent(self.root / "0" / "agents", "wren")
        write_skill(self.root / "z", "alpha")
        write_skill(self.root / "zz", "beta")
        original = copilot_port.install
        calls = []

        def flaky(output):
            calls.append(output.path.name)
            if len(calls) == 3:
                raise OSError("boom")
            return original(output)

        code, err = self.call_main(
            mock.patch.object(copilot_port, "install", side_effect=flaky))
        self.assertEqual(code, 1)
        self.assertIn("ERROR", err)
        self.assertIn("boom", err)
        self.assertIn("Rolled back 2 customizations.", err)
        self.assertFalse((self.github / "agents" / "wren.agent.md").exists())
        self.assertFalse((self.github / "skills" / "alpha").exists())
        self.assertTrue((self.github / "skills").is_dir())
        self.assertFalse((self.github / "copilot-port.json").exists())

    def test_rollback_on_manifest_failure(self):
        write_skill(self.root, "alpha")
        code, err = self.call_main(
            mock.patch.object(copilot_port, "replace",
                              side_effect=OSError("boom")))
        self.assertEqual(code, 1)
        self.assertIn("Rolled back 1 customizations.", err)
        self.assertFalse((self.github / "skills" / "alpha").exists())
        self.assertFalse((self.github / "copilot-port.json").exists())


if __name__ == "__main__":
    unittest.main()
