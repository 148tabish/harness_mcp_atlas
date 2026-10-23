# Migration: harbor 0.24.0 + Rewardkit grading

Working log for the 2026-10-22 migration. Tick items as they land; delete the file when the work is merged.
Decisions: judge default `gpt-5.6-sol` through codex since 2026-10-22 (a Pro ChatGPT login accepts it; a Plus login only terra/luna); an author-shipped verifier
(test.sh, Dockerfile, evaluate.sh, grade.py) is ignored with a warning, not refused.

## A. harbor 0.24.0
- [x] A1 vendor/harbor-0.24.0 replaces vendor/harbor-0.23.0
- [x] A2 openhands chown "uid:gid" fix lives in OpenHandsSDKBridge, harbor unpatched
- [x] A3 setup.sh installs 0.24.0, upgrades when the installed version differs
- [x] A4 SETUP.md / run-openhands.sh stale 0.23.0 wording
- [x] A5 smoke: nop calibration (reward 0.0) and one live opus-5 trial on 0.24 (job coldchain-4418-opus-5-20261022-084637: pytest 0.9899, rubric 0.9756, reward 0.9828, packaged as run_3 beside the two regraded runs)

## B. Rewardkit grading, 0.5 pytest / 0.5 rubric
- [x] B1 contract: harness owns test.sh, verifier Dockerfile, ledger, reward.toml; author verifier files ignored with a warning; no template dead files in bundles
- [x] B2 verifier image: pip harbor-rewardkit==0.2.3, codex kept, network public
- [x] B3 build.py generates tests/reward/{reward.toml, pytest/ledger.py, rubric/judge.toml}; rewardkit pointed only at tests/reward
- [x] B4 judge.toml compiler + validator (name=id, binary, signed weights / total positive under weighted-sum = the authors' max(0, (earned - penalised) / total positive); rest in annotations). A first version used weighted-mean + negate, which paid credit for avoided penalties (run_1 0.9431 vs 0.9103): fixed on review
- [x] B5 one judge-model source: --judge-model > JUDGE_MODEL (.env) > models.yaml; no literal fallbacks; check-env probes it
- [x] B6 judge.json + reward-details.json (+ judge_logs/) packaged; judge in report.json, judge_models in pass_summary.json; rubric null + judge{ran,reason} (proven on the 202230 regrade)
- [x] B7 reward.json carries pytest, rubric, reward (run_1: 0.9899 / 0.9431 / 0.9665, judge gpt-5.6-terra, 43 criteria, ~$0.23)
- [x] B8 retire rubric_judge_cli.py, evaluate.sh, template grade.py

## C. packaging and reporting
- [x] C1 cumulative run_N across jobs (same uuid + task checksum), no --force wipe, job/trial in report.json
- [x] C2 job names with seconds
- [x] C3 --threshold reaches the packager; threshold recorded in pass@N.json
- [x] C4 one scale 0-1 (6 dp) + scale field; mean_pass@k -> reward_per_run, accuracy -> pass_rate
- [x] C5 guards `tripped`; test crashes `error`; unreadable verifier JSON -> errored
- [x] C6 state_capture_failed.txt when the server log is missing; packager flags it
- [x] C7 report.json model name = folder normaliser; harness_summary.json in bundle (trajectories/<model>/jobs.json); judge cost: Rewardkit records token usage in reward-details.json, no $ table kept

## D. Mac + Linux parity
- [x] D1 compose `extra_hosts: host.docker.internal:host-gateway` on main; models.yaml base_url host.docker.internal, health 127.0.0.1; runjob.py no longer rewrites per OS (proven: a container reaches the bridge through the alias on this Mac)
- [x] D2 both bridges bind 0.0.0.0 (zbridge default was 172.17.0.1, a Mac has no such interface; probes on 127.0.0.1); .env.example labels the loopback options Mac-only
- [x] D3 setup.sh `== host`: OS/arch, engine version + host-gateway (with a real container probe), docker CPUs/memory vs defaults.concurrent, codex CLI, Claude login source (token / credentials file / Keychain)
- [x] D4 setup.sh inspects every pinned FROM digest of templates/tests/Dockerfile and vendor/light-servers/Dockerfile for the host platform (variants like linux/arm64/v8 count); Dockerfile.ecr left as is, linux/amd64 only, documented
- [x] D5 shell audit: only `base64 -w0` (run-openhands.sh) was GNU-only, replaced; df / ss-lsof already had BSD branches; no sed -i, readlink -f, timeout
- [x] D6 validate refuses a COPY/ADD in environment/Dockerfile whose source exists only in a different letter case (glob-aware, skips --from stages)
- [~] D7 proof: Mac done (job coldchain-4418-opus-5-20261022-094413, agent reached the bridge as host.docker.internal, 35 MCP calls, pytest 0.9899; the judge was skipped because the ChatGPT codex account hit its usage limit, fallback recorded in judge.json). EC2 run still to do with the same commit

## Follow-ups outside the harness
- [ ] task-pipeline trajqc: read the 0-1 scale and the renamed keys (reward_per_run, pass_rate, pytest_score, rubric_score, judge)
- [ ] task-pipeline: pass --judge-transport; the bundle QC spec should read judge.json

## Notes from the verification
- harbor-rewardkit pinned 0.2.3 (PyPI 0.2.1 predates the vendored copy); verifier Dockerfile installs with uv (pip via Docker Desktop's proxy exceeded the 1800 s start timeout)
- a Plus ChatGPT codex login refuses gpt-5.5 and gpt-5.6-sol (terra/luna only); the Pro login in use since 2026-10-22 runs gpt-5.6-sol, the default
- regrade needs PYTHONPATH to the bridge agent (harbor imports the agent class); fixed in harness/regrade.py
- with the rubric at 0.5 the coldchain runs score 0.9665 / 0.9696, under the task's 0.97 threshold that was set for pytest alone

## Grading review (2026-10-22, own pass + an independent reviewer)
- [x] rubric arithmetic: weighted-mean + negate paid credit for avoided penalties (run_1 0.9431 vs authors' 0.9103); now weighted-sum with signed weights / total positive = max(0, (earned - penalised) / total positive), clamped at 0 in test.sh
- [x] ledger: assert-style tests (no return value) were scored as failed while pytest passed them; the ledger now applies the same verdict-style rule as the ctrf plugin (return-style: value; assert-style: not raising; neither: no verdict)
- [x] ctrf.json: read only the old components shape (overall_score 0.0, no weights) and used the opposite guard polarity; now reads the flat tests and guard_polarity
- [x] judge isolation: the codex judge runs unsandboxed as root beside the ledger; it now works on a copy of /workspace, never the directory the ledger reads
- [x] judge trajectory: was hard-wired to harbor's ATIF; non-ATIF agents (claude-code, oracle) would have left every [trajectory] criterion without evidence; test.sh now places the ATIF or the rebuilt trajectory in judge_ws and the compiled judge.toml points there
- [x] timed-out judge: report.json took rubric 0.0 from reward-details.json while reward.json had dropped the channel; rubric_score now comes from reward.json only, rows only when the judge ran
- [x] build hash: .build.json carried built_at, so every rebuild changed harbor's task_checksum and orphaned earlier trials in the packager; no timestamp in the task dir any more (verified: two builds hash the same)
- [x] report.json evaluation_target was null (Rewardkit drops annotations); taken from the compiled judge.toml instead
- noted, not changed: judge timeout 1200 s + fast error retries vs the verifier's 1800 s; pytest dimension rounded to 4 dp before the root mean (sub-1e-4); a rubric penalty trip classifies as wrong_answer/partial, only pytest guards as guard_violation

## Open
- the ChatGPT codex login hit its usage limit on 2026-10-22 ("try again at Nov 20th, 2026"); until then the rubric channel is unscored on every run and reward = pytest. Alternatives: another codex login, or JUDGE_TRANSPORT=claude-code with CLAUDE_CODE_OAUTH_TOKEN (plumbed, not yet exercised)
- a harness template change (compose, verifier) changes the task hash, so earlier runs are packaged apart from new ones; that is by design (their tests/ differ) but worth knowing before changing templates mid-campaign

## Added 2026-10-22 (formatting tasks/ -> tasks/tasks-formatted)

- [x] judge_compile reads the Rewardkit-shaped authoring rubric too (polarity / evaluation_target / importance /
      category / number inside `annotations`, `type = "binary"`, signed weights, `negate = false`); `negate = true` refused
- [x] evaluation targets `state_change` ([end state]) and `user_facing_message` ([final answer]) accepted; test.sh gives the
      judge `judge_ws/end_env.json` and prompt.md explains the tag
- [x] import_task cuts the author's blocks at every harness block (a built task.toml's `[[environment.mcp_servers]]` was being
      copied into the input) and leaves the built verifier behind instead of carrying it
- [x] defaults: `--model` opus-5 (`defaults.model`), judge gpt-5.6-sol
- [x] a built task carries no duplicate: the author's tests/judge.toml and .build.json are no longer shipped (the compiled
      rubric holds every source field; import_task decompiles it back, proven byte-identical on recompile); claims_lib.py is
      harness-owned (an author copy is skipped like the other verifier files)
- [x] pass@1 of the three formatted tasks run 2026-10-22 (opus-5, judge gpt-5.6-sol), packaged under out/packaged:
      04ce26ea 0.35 (pytest 0 / rubric 0.7, regraded), a8c4b7f1 0.4209 (0.2759 / 0.566), 21b885d5 0.3812 (0.2321 / 0.5303)
- [x] verifier fixes found by the first run: end_env.json carries both snapshot shapes (house {App: {status, output}} and
      authored {"apps", "capture_status"}); guards that assert AND return a value are "mixed" (None after the asserts = pass);
      the three tasks declare guard_polarity = pass_charges; validate warns when guards have no declared polarity
- [ ] AUTHOR DEFECT 04ce26ea: oracle.json bamboohr_baseline digests were taken from the fixture (old_env says session_fixture),
      not the served world (corpus + overlay), and include headcount_plans, a key the server never dumps -> the
      bamboohr_written guard trips on every run and gates the pytest channel to 0. Needs the author (or a nop-calibration
      baseline) before this task can score on pytest.
- [ ] run the three formatted tasks (opus-5, judge gpt-5.6-sol) and package; 04ce26ea's test_weights.json declared no
      null_floor or threshold (null_floor 0.0 added at import, threshold = harness default 0.7)

## Added 2026-10-23 (author server patches)
- [x] oracle/nop calibration of the three final tasks (judge gpt-5.6-sol): nop 0.0 everywhere; oracle a8c4b7f1 1.0,
      21b885d5 0.9394 (pytest 1.0; rubric 0.8788 is the author's dossier text vs their own rubric), 5b10675f 0.0227.
- [x] ROOT CAUSE 5b10675f: the author's task changes environment/light-servers/software/LightAirtable/airtable.py
      (preview rows answered, counts re-delivered, sources handed over). import_task kept only task_data + world_data
      and build copied vendor/light-servers wholesale, so the served world lacked the task's behaviour: solve.py
      raised "preview for web-access not answered", and the opus-5 pass@1 (0.2529) ran in that same world (its preview
      row was never answered, no re-delivered count ever appeared) -> that run and its QC verdict are void.
      Fix: harness/server_patches.py; import_task carries differing *.py / desc.json under software/ into
      input/<name>/environment/light-servers/..., validate lists them (refuses anything else there), build copies
      them over the vendor tree after laying it down. Corpus differences (every task ships an older
      LightKubernetes/corpus/state.yaml) are reported and not carried: world changes go through world_data.
- [ ] 04ce26ea also ships server patches (LightBambooHR, LightGreenhouse, LightNews, utils/world_events.py,
      utils/receipts.py) plus corpus/desc.json changes; re-import and re-check its "author baseline defect".

## Added 2026-10-23 (5 more tasks -> "5more taks/task-formatted")
- [x] Three atlas tasks (2e23ad31, 310e65cb, 8522713f) imported, fixed, built, copied. What the import/validate path
      missed and what was changed in the harness because of it:
      - judge.toml targets `final_report` (2e23ad31) and `end_env` (310e65cb) are not harness names; renamed in the
        inputs to final_answer / state_change. All three sets of guards detect-the-misbehaviour -> guard_polarity
        pass_charges (the import had left the default fail_charges in place).
      - environment/Dockerfile pins `curl=8.14.1-2+deb13u5` (2e23ad31, 310e65cb): gone from Debian trixie, every
        trial died at image build. Pin moved to deb13u7 like the sibling tasks. Author-side, Dockerfile only.
      - 8522713f shipped BOTH tests/judge.toml (33 criteria of another task: SLA outage credits) and tests/rubric.json
        (its own 27). The harness compiles judge.toml first, the task's own verifier used rubric.json first, so the
        oracle was judged against a foreign rubric (pytest 1.0, rubric 0.0). validate now refuses a task shipping
        both; import_task warns. The foreign judge.toml was removed from the input.
      - 310e65cb shipped its own tests/claims_lib.py (load_fixture(env, name), end_env, captured, by_key, number,
        text) under the same name as the harness library; import_task drops that name, so test_outputs.py collected
        against the harness's module and the verifier wrote no reward. Carried as tests/task_lib.py with the import
        changed; import_task now warns when a dropped verifier file differs from templates/tests/.
      - 310e65cb's oracle prints a plain-text summary to oracle.txt and writes its calls to
        /logs/agent/trajectory.json. test.sh step 1 and state_capture.py took the first non-empty candidate
        (oracle.txt) -> 0 steps, no sessions, no final message: every trajectory / final-answer criterion
        failed for the wrong reason (rubric 0.5714). Both now take the first candidate that yields steps or a
        final message. test.sh step 2 also fills capture_status.<App>.sessions from the trajectory (tool names
        App.tool / App_tool / mcp__App__tool) instead of always []; S009-style criteria read it.
      - 310e65cb's solve.py emitted no closing report at all; one is now appended as the final ATIF agent
        message (total 1500000 cents, third edition, refusal needs both quantities, cap <= audited excess,
        per-row quantity that crossed the limit). 8522713f rubric criterion 25 reworded (the judge read "while
        the live register still carries the keyed figures" as the corrected figures and charged the oracle).
- [x] (re-authored 2026-10-24 in the standard layout, see below) Two ethara tasks (22152a1d permit-fee-refund-batch, c5ff46fc greenhouse-irrigation-permit-audit) were a
      different authoring layout (checkers.py + expected.json + fixtures/, solve.sh + recompute.py, no
      test_weights.json / oracle.json / gt_env.json / solve.py, judge criteria without evaluation_target, judge
      off-reward in their own verifier) and do not fit the import. Left in input/ unformatted; user to decide.

## Added 2026-10-23 (three more jatin tasks: cf630988, eeafd50a, e18698a1)

- [x] validate.py counted only `def test_...` as defined tests, so eeafd50a's 24 tests bound by assignment
      (`test_sizing_sheet_gpu_a100 = _sheet_test("gpu-a100")`), which pytest collects by attribute name, were
      refused as "weights a test no test defines". The check now also matches `^test_\w+\s*=`. Oracle 80/80 confirms
      the verifier collects them.
- [x] Debian pin drift again: curl and libcurl4t64 8.14.1-2+deb13u5 -> deb13u7 in the three agent Dockerfiles.

## Added 2026-10-24 (aman2tasks: 22152a1d, c5ff46fc re-authored)

- [x] c5ff46fc mints 21 tests at import time (globals()["test_T_%s" % k] = _mk_trap(k)); no static regex can
      enumerate them, so validate refused every one as "weights a test no test defines". When the module uses
      globals()["test_..."], the unmatched names are now a WARNING; the oracle calibration is the check that every
      weighted name is collected (c5ff46fc: 35 weighted, 35 collected, 99/99).
- [x] Debian pin drift again: curl and libcurl4t64 deb13u5 -> deb13u7 in both agent Dockerfiles.
