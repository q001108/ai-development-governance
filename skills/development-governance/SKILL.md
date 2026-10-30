---
name: development-governance
description: Start or continue development in a project that has adopted this governance plugin, or adopt and upgrade it. Select the requirement and its bound workflow, preserve project execution policies, and prepare the selected local tests.
---

# Development governance

This skill is distributed with one fixed plugin release. Its resource root is two directories above this file; project paths refer to the target repository. Never substitute a developer's latest global copy for a project's locked release.

## Adoption and upgrades

Read [project adaptation](../../docs/工具说明/社区插件与项目适配.md) and [adoption commands](../../docs/工具说明/项目接入与升级.md). Inspect `scripts/governance_project.py --help` in the selected release before constructing commands. Generate and review an initialization or upgrade preview for the requested target project; store the new preview file outside that project, then apply the authorized scope. Preserve project AGENTS content, local customizations, business facts, and existing requirement bindings. Conflicts need resolution before applying; do not bypass the lock or edit vendored releases. Building a package, committing, publishing, and adopting it in another repository are separate actions with their own task authorization.

For an authorized legacy project adoption, `preview --preserve-existing` can preserve existing project docs, roles and scripts while adding the pinned runner. Existing script paths remain legacy in that mode. Read project-specific execution exceptions before starting a requirement; preserve an already explicit direct-development instruction in its actual scope instead of silently forcing governance back on. Project adapters may register a requirement and then record that existing instruction as MANUAL. Do not broaden a branch-specific exception to other branches.

## Work in an adopted project

Read the project's AGENTS and `.governance/entry.md`, then use `python .governance/run.py <tool-name> [arguments]`. The runner selects the locked tools and resources; specify the actual project root and requirement inputs required by each tool. Read each tool's `--help` instead of guessing flags.

When the active project runner provides `template`, locate needed templates with `template --kind baseline|context|development|qa|review --requirement-id <id>` and read its ResourceRef. Project docs paths may be compatibility navigation pages rather than template bodies. Use `--default` only for an explicit pre-registration preview; an unbound historical requirement must keep its original version/Git sources. Older project runners keep their original entry and bound resources. Read [template convergence](../../docs/工具说明/模板收敛.md) when adopting RELEASE_ONLY or changing template mirrors. Retained release files are immutable; convergence never deletes historical instances or infers migration.

In a tracking-enabled project whose active release provides `start`, first use the [unified startup](../../docs/工具说明/统一启动与续接.md). Classify the user's intent: `--intent new` for an explicit new implementation request; `--intent continue` for continued work; `--intent query` for discussion or read-only inspection. New requests supply a concise title, safe original instruction, and an AI-generated stable `--request-key` reused for retries, or the already assigned RequirementId. Developers do not fill this key or run the commands. Provide an explicit ID when known; otherwise let the router use the branch or a valid local association. Never choose a lone unassociated requirement merely because it is the only candidate. If selection is needed, present the returned choices; if a source is blocked, resolve its original facts rather than creating a second requirement.

Show a short startup receipt: requirement, actual mode, current stage, next action and blockers. READY describes located context; it grants no implementation or test authority. Use the returned requirement binding and continue its existing stage rules, preserving MANUAL across sessions and explicit resumption over a branch default. Startup already refreshes the overview on implementation/continuation; do not repeat that refresh immediately. Read-only startup writes nothing. It does not start tests: when the actual authorized test step arrives, invoke the existing automatic B1 path without waiting for another environment-check request. For an older project release without startup, keep the original entry; request/apply an explicit upgrade rather than editing its retained release.

For an existing requirement, first read its `plugin-binding.json` and load instructions and stage documents from that project's `.governance/releases/<PackageDigest>/`. The links below are discovery references; use their counterpart in the requirement's bound release when continuing it. Never mix the currently installed skill's newer rules with older bound tools. For tools without requirement inputs, add the runner-only `--for-requirement <id>` immediately after the tool name. Use that selector for rule previews and the original QA session's `test` and `stop` actions; it is consumed before forwarding legacy tool arguments.

For a new requirement, follow the default project's pinned [governance entry](../../AGENTS.md) and [main process](../../docs/Agent治理/多智能体协同开发治理流程.md). Tracking-enabled projects use the initializer below to reserve, bind and register in one workflow. Other projects reserve the ID and bind through `python .governance/run.py bind --requirement-id <id>` before creating governed artifacts. Existing requirements keep their existing binding. An unbound existing requirement is rejected by this version and needs a separate migration workflow; do not assign the active release or imply that approval alone performs migration. Skill discovery creates neither gate approval nor recorded facts.

## Adopted requirement tracking

When `.governance/tracking.json` exists, first read [requirement tracking v1.0](../../docs/工具说明/需求管理.md) from the selected release. Unified startup composes registration, project policy, status and overview; `governance_requirements init` remains the low-level/older-release registration interface. Pure discussions and read-only queries do not create requirements. Reuse an already registered ID on continuation. G1 precedes planning drafts; a verified G2 precedes `initialize-record` and VNNN assignment. Do not create approval facts merely to populate the overview.

At implementation start, continuation and stable handoffs, invoke `governance_overview refresh`; use `query` for read-only requests. Fetch uses an ignored local cache; it does not upload local changes. Shared observations have source refs, commits and timestamps and remain separate from local progress. A conflicting branch or stale source must stay explicit. Existing `docs/需求版本管理.md` remains the delivery archive ledger.

Check `governance_requirements inspect --requirement-id <id>` before governed work. An explicit developer request to exit takes priority over the ordinary governance workflow for that requirement: persist it via `exit --instruction ...`, stop further governed dispatch, cancel cancellable roles and clean owned resources, then continue helping as a normal coding assistant. Readability, security, permission and Git boundaries still apply. Do not demand a reason, gate approval or another confirmation to honor an already explicit instruction. Do not label manual delivery as quality/acceptance passed.

The MANUAL mode persists across sessions until an explicit request to resume. Before calling `resume`, inspect actual changes against the bound plan and supply the review summary; use existing change-control rules where required. A new candidate is required before reusing the governed quality path. Management history is a record of instructions, not proof that the model performed a complete semantic review. Serial `close`/`next` preserve committed history and existing plan version chains; they never silently reset old controls or migrate the plugin binding.

Select only resources needed by the current stage:

- Technical planning: [rule composition](../../docs/工具说明/规则组合.md). Use project declarations and actual affected modules; previews do not create acceptance controls.
- Before running local tests: [local test preparation](../../docs/工具说明/本地测试准备.md). When the project has adopted automatic preparation, invoke it as part of the authorized test workflow, consume the observed result, and report missing project inputs. Do not ask developers to fill a separate per-test environment checklist. Independent QA uses the same bounded test-session bridge and the existing dispatch rules.
- Writer handoff and quality checks: [candidate snapshots](../../docs/工具说明/候选快照.md). Draft snapshots require the existing runtime registration; no old quality result becomes valid merely by recapturing files.
- Continuing a requirement: [status query](../../docs/工具说明/状态查询.md). An undetermined result requires checking the original bound facts and rules.

Company-specific commands, domain rules, and business facts stay in that project's adaptation. General fixes belong in community source, followed by a new verified package and an explicit project upgrade. This release covers Codex; other AI tool adapters and complete automatic runtime continuation are outside its implemented scope.
