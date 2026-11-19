# Copilot Port

Convert local Agent Skills, Claude Code subagents, and Codex custom agents into repository customizations for the GitHub Copilot harness in VS Code. The converter runs locally using fixed rules. It makes no network or model calls and executes no code from the source bundles.

## Setup

Install Python 3.11 or newer. From the converter directory, run:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell, use `py -3 -m venv .venv` and `& .\.venv\Scripts\Activate.ps1`.

## Convert a skill or repository

Clone the upstream repository locally.

```sh
# Preview one skill.
python copilot_port.py ../upstream/skills/doc-coauthoring --target ../team-repo

# Preview a batch from a repository checkout.
python copilot_port.py ../upstream --target ../team-repo

# Write the batch after reviewing its warnings.
python copilot_port.py ../upstream --target ../team-repo --write
```

Pass a `SKILL.md` file, a skill directory, a Claude agent `.md` file, a Codex agent `.toml` file, or any combination. Directory scans look for skill bundles and agent files in directories called `agents`, including the conventional `.claude/agents` and `.codex/agents` locations. Scans skip hidden directories other than `.claude`, `.codex`, `.agents`, and `.github`, such as `.system`; pass one explicitly to include it. Pass individual files for agents stored elsewhere. The target defaults to the current directory.

| Source | Copilot output | Conversion |
| --- | --- | --- |
| Agent Skill | `.github/skills/<name>/SKILL.md` | Normalizes the name and frontmatter, preserves the Markdown body, and carries bundled files and executable bits. Omits Codex-only `agents/openai.yaml`, mapping `allow_implicit_invocation: false` to `disable-model-invocation: true` and flagging declared dependencies. |
| Claude Code subagent | `.github/agents/<name>.agent.md` | Preserves the Markdown role instructions, maps known tool names to Copilot aliases, and carries named preloaded skills. |
| Codex custom agent | `.github/agents/<name>.agent.md` | Converts the TOML description and `developer_instructions` into a Copilot agent profile. |

The converter reports dropped fields and flags common provider-specific syntax for review. It leaves the instruction text intact, including provider-specific placeholders and file references. Review hooks, permissions, model selection, tool availability, and links to files outside a copied skill before using the result. Carry any applicable upstream license and notices that live outside the skill bundle.

Each run records provenance in `<target>/.github/copilot-port.json`: for every customization it stores the source path, a `sha256` of the generated output, and — when the source lives in a git checkout — the checkout's origin URL and resolved commit. Entries merge into the existing manifest, so customizations from different runs coexist. Commit it alongside the generated files; comparing a stored `sha256` against a fresh run spots local edits. The manifest does not detect uncommitted changes in the source checkout.

Use `--strict` to fail on any migration warning, including approximate tool mappings. Lines starting `NOTE` are informational and never fail `--strict`. Validation errors and destination conflicts prevent all planned writes, and a failed write rolls back the customizations created in that run. Re-running against identical output reports `UNCHANGED`. For an upstream update, generate into a fresh staging directory and review the diff against your team's current files.

## Review agent tools and models

Plain Claude tool names such as `Read`, `Grep`, and `Bash` map to Copilot aliases such as `read`, `search`, and `execute`. Scoped and unknown tool names are reported and omitted from the generated list. An explicit source allowlist stays explicit even if no tools map. A Claude denylist or a disabled Codex shell requires a reviewed target list.

An optional JSON file lets you specify the complete list and exact Copilot model ID for an agent. Use the generated agent ID as the key. The `tools` list replaces the source tool selection. Use `[]` to request an empty list. Both fields are optional.

```json
{
  "docs-reviewer": {
    "tools": ["read", "search"]
  }
}
```

```sh
python copilot_port.py ../upstream/agents/docs-reviewer.md \
  --target ../team-repo --overrides copilot-overrides.json --write
```

In VS Code, select the Copilot harness and run **Chat: Open Customizations** to check that each imported skill and agent is discovered. The target workspace must be trusted — in Restricted Mode VS Code does not show workspace skills and agents. Typing `/` in a new chat should list each imported skill. Verify the available tools and model in a Copilot session. Tool selection applies within that harness and does not substitute for organization approvals or permissions.

## Format references

- [VS Code Agent Skills](https://code.visualstudio.com/docs/agent-customization/agent-skills) and [Agent Skills specification](https://agentskills.io/specification)
- [VS Code custom agent file reference](https://code.visualstudio.com/docs/agents/reference/custom-agent-file-reference) and [GitHub Copilot tool aliases](https://docs.github.com/en/copilot/reference/custom-agents-configuration#tool-aliases)
- [Claude Code skills](https://code.claude.com/docs/en/skills), [Claude Code subagents](https://code.claude.com/docs/en/sub-agents), and [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents#custom-agents)

## License

MIT. See [LICENSE](LICENSE).
