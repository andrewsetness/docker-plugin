# Completion contract: docker-plugin

> Repository-specific completion definition under Portfolio Completion
> Standard v1.1 (`docs/COMPLETION_STANDARD.md` in project-meta). This document
> defines what "done" means for this version; it does not claim completion.
> Current progress lives in this repository's STATUS.md.

## Outcome

Jenkins Docker Cloud plugin that dynamically provisions containers as Jenkins agents. This fork (`andrewsetness/docker-plugin`) is the canonical portfolio copy as of 2026-11-09; the sibling `setnessconsulting/docker-plugin` fork was retired after its unique completion documents were ported here. The repository is complete for v1 when the documented Maven build/test path passes on the reviewed SHA and Jenkins compatibility constraints (2.555.3+) hold; upstream release tracking is out of scope.

## Jobs to be done

- Build the plugin with Maven.
- Run the plugin's test suite.
- Maintain Jenkins baseline compatibility (2.555.3+).
- Provide lightweight PR checks via GitHub Actions.

## Required functionality

- Maven project with plugin source and tests.
- Jenkins core baseline constraint.
- GitHub Actions PR checks.
- Plugin documentation (README, CONTRIBUTING).

## Automated verification

- Maven build and tests (`mvn -B verify` or repo's documented command) must pass on the reviewed SHA.
- GitHub Actions lightweight PR checks must be green.

## Manual verification

- Offline review of README/CONTRIBUTING against build behavior.
- Confirm Jenkins baseline version constraint.

## Runtime / external checks

- Jenkins runtime integration is an external check (Jenkins controller required).
- No live deployment claimed for this fork.

## Stability evidence

- Local Maven test suite green on the reviewed SHA.
- Gate 8 Tier B evidence recorded in STATUS.md.

## Acceptable limitations

- Upstream Jenkins plugin release tracking is out of scope for this fork's milestone.
- Canonical checkout was clean; contract recorded against `origin/master` `823df59fa0991f2fdc9b460f7c41e3101b6b04b5`.

## Post-completion operating mode

Maintenance-only with periodic review; active evolution only under a new milestone with owner approval.

## Open owner decisions

- Tier B confirmed for gate 8 (owner, 2026-11-09).
- Canonical fork selected: `andrewsetness/docker-plugin`; `setnessconsulting/docker-plugin` retired (owner, 2026-11-09).
- Jira task: AIPORT-944.
