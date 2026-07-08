# README And Distribution Reference Review

Sources checked on 2026-07-08:

- OpenAI Codex Skills: https://developers.openai.com/codex/skills
- OpenAI Codex Plugins: https://developers.openai.com/codex/plugins
- OpenAI Build Plugins: https://developers.openai.com/codex/plugins/build
- Agent Skills Specification: https://agentskills.io/specification
- Claude Skill Authoring Best Practices: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices
- GitHub README Docs: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes
- anthropics/skills: https://github.com/anthropics/skills
- jehna/readme-best-practices: https://github.com/jehna/readme-best-practices
- matiassingers/awesome-readme: https://github.com/matiassingers/awesome-readme

## Features To Adopt

| Source | Useful pattern | Applied here |
|---|---|---|
| OpenAI Codex Skills | Direct skill folders are good for local authoring; reusable multi-skill distribution should use plugins. | Repo includes `.codex-plugin/plugin.json` and `skills/`. |
| Agent Skills Specification | Each skill is a folder with `SKILL.md`; optional `references/`, `scripts/`, `assets/` support progressive disclosure. | Each skill has `SKILL.md` and focused `references/`. |
| Claude Best Practices | Keep skill body concise, use specific descriptions, split details into references, test with real usage. | SKILL.md files are short; policy details live in references. |
| GitHub README Docs | README should explain what the project does, why it is useful, how to start, where to get help, and maintainer/contributor expectations. | README includes purpose, layout, install, usage, validation, license. |
| anthropics/skills | Self-contained skills with examples and disclaimers; test before relying on critical workflows. | README states forward-testing is still required. |
| README best-practice repos | Clear title, short description, install, features, contributing/license, examples, links. | README keeps a direct operational structure. |
| awesome-readme | Good READMEs often include badges, screenshots/GIFs, TOC, examples, docs links, and licensing. | Future polish can add badges and examples after behavior is validated. |

## Do Not Adopt Yet

- Heavy branding, screenshots, or badges before the workflow is forward-tested.
- A wrapper `workflow` skill before actual usage patterns are known.
- Marketplace files that install the plugin by default before the manifest is reviewed.
- Claims that the skills are safe for production-critical tasks before pressure tests pass.
