-- A Sprint in flight, exactly as the engine stored it at
-- 0284_reseed_sprint_consistency.sql: declared with --merge-grant, armed,
-- first assignment delivered and accepted, PR 41 registered with
-- its checks still running.  Written by
--   python tests/sprint_v24_support.py --write-midlane-fixture
-- Never hand-edit; never regenerate from a later engine.
INSERT OR REPLACE INTO "active_shell_chats" VALUES(1,'cv_6e231c137f394a4b8754ed6bc5af3e93',NULL,NULL,'2026-11-11 22:07:51');
INSERT OR REPLACE INTO "active_shell_chats" VALUES(3,'cv_77427ba9c4a84e0cbfc081b79c21352f',NULL,NULL,'2026-11-11 22:07:51');
INSERT OR REPLACE INTO "conversation_events" VALUES(1,'cv_77427ba9c4a84e0cbfc081b79c21352f',1,'conversation.created',1,'{"participant_id":3,"scope":"sprint","sprint_id":1,"wake_id":1}',NULL,NULL,'2026-11-11 22:07:51');
INSERT OR REPLACE INTO "conversation_events" VALUES(2,'cv_6e231c137f394a4b8754ed6bc5af3e93',1,'conversation.created',1,'{"participant_id":1,"scope":"sprint","sprint_id":1,"wake_id":2}',NULL,NULL,'2026-11-11 22:07:51');
INSERT OR REPLACE INTO "conversation_messages" VALUES(1,'cv_77427ba9c4a84e0cbfc081b79c21352f','engine','test','prompt','Sprint 1 handoff for your Originating Planner role. Load `sprint_protocol`, then `sprint_pln`. Run `sc sprint inbox --sprint 1` now and act on the Sprint message(s) using `sprint_pln`. Confirm every Sprint write succeeds before stopping. If a Sprint command failed or did not confirm its durable write, retry that command. Do not re-check the inbox otherwise — new messages arrive as their own wakes.

## wake_message #1 (declared New)

Sprint 1 armed. Bound participant routes:
- planner PLN1: codex · model=Harness default · Thinking level=uncontrolled · harness-default · route revision 1
- developer DEV1: codex · model=Harness default · Thinking level=uncontrolled · harness-default · route revision 1
- reviewer REV1: codex · model=Harness default · Thinking level=uncontrolled · harness-default · route revision 1','receiver:3:wake-for-message:1','receiver:3:wake-for-message:1',NULL,'completed','2026-11-11 22:07:51','2026-08-01 00:00:01');
INSERT OR REPLACE INTO "conversation_messages" VALUES(2,'cv_6e231c137f394a4b8754ed6bc5af3e93','engine','test','prompt','Sprint 1 handoff for your Developer role. Load `sprint_protocol`, then `sprint_dev`. Run `sc sprint inbox --sprint 1` now and act on the Sprint message(s) using `sprint_dev`. Confirm every Sprint write succeeds before stopping. If a Sprint command failed or did not confirm its durable write, retry that command. Do not re-check the inbox otherwise — new messages arrive as their own wakes.

## wake_message #2 (declared Force-New)

First lane

First lane: one merged PR.

sc context --work-unit 1','receiver:1:wake-for-message:2','receiver:1:wake-for-message:2',NULL,'completed','2026-11-11 22:07:51','2026-08-01 00:00:01');
INSERT OR REPLACE INTO "conversation_runs" VALUES(1,'cv_77427ba9c4a84e0cbfc081b79c21352f',3,1,1,NULL,NULL,NULL,'succeeded','test-broker','2026-08-01 00:00:01','2026-08-01 00:00:00',NULL,'2026-08-01 00:00:01',0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);
INSERT OR REPLACE INTO "conversation_runs" VALUES(2,'cv_6e231c137f394a4b8754ed6bc5af3e93',1,2,1,NULL,NULL,NULL,'succeeded','test-broker','2026-08-01 00:00:01','2026-08-01 00:00:00',NULL,'2026-08-01 00:00:01',0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL);
INSERT OR REPLACE INTO "conversations" VALUES('cv_77427ba9c4a84e0cbfc081b79c21352f',3,1,'codex','openai',NULL,NULL,'/srv/engine/.sc-worktrees/pln1',NULL,'waiting','Sprint 1 · Wake 1 · PLN1','generation:77bca3ddb4c315ef0ebdb974f8582ef8:participant:3:route:1:wake:1','c65b7db80cc6904779f07d04d49bd5b42e884f021c02442573733865cab314a3','2026-11-11 22:07:51','2026-11-11 22:07:51',NULL,1,0,'sprint',2,'{"adapter_metadata":{},"catalogue_generation":null,"contract_version":2,"control_state":"harness-default","effective_effort":null,"evidence_digest":null,"harness":"codex","native_variant_id":null,"provider_model":null,"requested_effort":null,"requested_model":null,"selector_binding":null,"transport":"native-default"}');
INSERT OR REPLACE INTO "conversations" VALUES('cv_6e231c137f394a4b8754ed6bc5af3e93',1,1,'codex','openai',NULL,NULL,'/srv/engine/.sc-worktrees/dev1',NULL,'waiting','Sprint 1 · Wake 2 · DEV1','generation:77bca3ddb4c315ef0ebdb974f8582ef8:participant:1:route:1:wake:2','77daadeb7d886eaac8e76deba800bb9e0733f46f6a5df4a72722026c9e60ba17','2026-11-11 22:07:51','2026-11-11 22:07:51',NULL,1,0,'sprint',2,'{"adapter_metadata":{},"catalogue_generation":null,"contract_version":2,"control_state":"harness-default","effective_effort":null,"evidence_digest":null,"harness":"codex","native_variant_id":null,"provider_model":null,"requested_effort":null,"requested_model":null,"selector_binding":null,"transport":"native-default"}');
INSERT OR REPLACE INTO "documents" VALUES(1,1,'spec',1,'Safety-net spec',0,NULL,'bound safety-net spec',NULL,'2026-11-11 22:07:51','2026-11-11 22:07:51',0,NULL,NULL);
INSERT OR REPLACE INTO "pr_subscription_transitions" VALUES(1,1,'created','6bc680eb95846887b69a90ea5d8b6a933cd688a55c319203837c17ede7ac24aa','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','{"base_ref": "main", "base_sha": "cccccccccccccccccccccccccccccccccccccccc", "checks": null, "checks_failed": false, "head_ref": "feature/pr-41", "merge_sha": null, "review_decision": null, "state": "OPEN", "title": "PR 41", "trigger": "registration", "url": "https://github.com/acme/repo/pull/41"}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "pr_subscriptions" VALUES(1,1,'acme/repo',41,1,'2026-11-11 22:07:51','2026-11-11 22:07:51.553');
INSERT OR REPLACE INTO "roadmap" VALUES(1,'Safety-net feature','in_progress',0,NULL,NULL,'2026-11-11 22:07:51','2026-11-11 22:07:51',NULL);
INSERT OR REPLACE INTO "shells" VALUES(1,'Developer','DEV1',NULL,NULL,NULL,'prompt',NULL,NULL,NULL,NULL,'dev',0,0,NULL,1,0,0,'dev-token',NULL,NULL);
INSERT OR REPLACE INTO "shells" VALUES(2,'Reviewer','REV1',NULL,NULL,NULL,'prompt',NULL,NULL,NULL,NULL,'reviewer',0,0,NULL,1,0,0,'review-token',NULL,NULL);
INSERT OR REPLACE INTO "shells" VALUES(3,'Planner','PLN1',NULL,NULL,NULL,'prompt',NULL,NULL,NULL,NULL,'planner',0,0,NULL,1,0,0,'planner-token',NULL,NULL);
INSERT OR REPLACE INTO "spec_tasks" VALUES(1,1,1,1,'Task 1',NULL,'pending',NULL,NULL,NULL,'2026-11-11');
INSERT OR REPLACE INTO "spec_tasks" VALUES(2,1,1,2,'Task 2',NULL,'pending',NULL,NULL,NULL,'2026-11-11');
INSERT OR REPLACE INTO "sprint_events" VALUES(1,1,'sprint.declared','planner',3,'{"feature_id": 1, "participant_shell_ids": [1, 2, 3], "spec_document_ids": [1]}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(2,1,'work_unit.created','planner',3,'{"assigned_shell_id": 1, "dependency_ids": [], "output_kind": "code", "planned_wave": 0, "reviewer_shell_id": 2, "task_ids": [1], "work_unit_id": 1}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(3,1,'work_unit.created','planner',3,'{"assigned_shell_id": 1, "dependency_ids": [1], "output_kind": "code", "planned_wave": 0, "reviewer_shell_id": 2, "task_ids": [2], "work_unit_id": 2}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(4,1,'participant.route_bound','planner',3,'{"binding_digest": "9a5d70af55f4dd8ced8ca6c5da69f98bd0bb548e9cb0b8daaed17d981343a357", "binding_id": 1, "contract_version": 2, "control_state": "harness-default", "effective_effort": null, "harness_support_state": "best-effort", "harness_version": "0.146.0", "participant_id": 3, "route_revision": 1}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(5,1,'participant.route_bound','planner',3,'{"binding_digest": "9a5d70af55f4dd8ced8ca6c5da69f98bd0bb548e9cb0b8daaed17d981343a357", "binding_id": 2, "contract_version": 2, "control_state": "harness-default", "effective_effort": null, "harness_support_state": "best-effort", "harness_version": "0.146.0", "participant_id": 1, "route_revision": 1}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(6,1,'participant.route_bound','planner',3,'{"binding_digest": "9a5d70af55f4dd8ced8ca6c5da69f98bd0bb548e9cb0b8daaed17d981343a357", "binding_id": 3, "contract_version": 2, "control_state": "harness-default", "effective_effort": null, "harness_support_state": "best-effort", "harness_version": "0.146.0", "participant_id": 2, "route_revision": 1}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(7,1,'conformance_owner.changed','planner',3,'{"generation": 1, "new_shell_id": 2, "previous_shell_id": null, "reason": "selected while arming"}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(8,1,'work_unit.ready','system',NULL,'{"message_id": 2, "wake_id": 2, "work_unit_id": 1}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(9,1,'lifecycle.armed','planner',3,'{"initial_wake_ids": [2, 1], "participant_bindings": [{"binding_digest": "9a5d70af55f4dd8ced8ca6c5da69f98bd0bb548e9cb0b8daaed17d981343a357", "binding_id": 1, "contract_version": 2, "control_state": "harness-default", "effective_effort": null, "harness_support_state": "best-effort", "harness_version": "0.146.0", "participant_id": 3, "route_revision": 1}, {"binding_digest": "9a5d70af55f4dd8ced8ca6c5da69f98bd0bb548e9cb0b8daaed17d981343a357", "binding_id": 2, "contract_version": 2, "control_state": "harness-default", "effective_effort": null, "harness_support_state": "best-effort", "harness_version": "0.146.0", "participant_id": 1, "route_revision": 1}, {"binding_digest": "9a5d70af55f4dd8ced8ca6c5da69f98bd0bb548e9cb0b8daaed17d981343a357", "binding_id": 3, "contract_version": 2, "control_state": "harness-default", "effective_effort": null, "harness_support_state": "best-effort", "harness_version": "0.146.0", "participant_id": 2, "route_revision": 1}], "planner_wake_id": 1, "work_wake_ids": [2]}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(10,1,'work_unit.accepted','participant',1,'{"message_id": 2, "work_unit_id": 1}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(11,1,'pr.registered','participant',1,'{"pr_number": 41, "registered_pr_id": 1, "repository": "acme/repo", "work_unit_ids": [1]}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(12,1,'pr.transition','system',NULL,'{"normalized_state": "created", "registered_pr_id": 1, "subscription_id": 1, "transition_id": 1}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_events" VALUES(13,1,'pr.no_checks_observed','system',NULL,'{"observed_head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "registered_pr_id": 1, "subscription_id": 1, "transition_id": 1}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_participant_conversations" VALUES(1,3,'cv_77427ba9c4a84e0cbfc081b79c21352f','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_participant_conversations" VALUES(2,1,'cv_6e231c137f394a4b8754ed6bc5af3e93','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_participant_route_bindings" VALUES(1,3,1,2,'harness-default','codex',NULL,NULL,NULL,NULL,NULL,NULL,'native-default',NULL,NULL,NULL,'{}','{"adapter_metadata":{},"catalogue_generation":null,"contract_version":2,"control_state":"harness-default","effective_effort":null,"evidence_digest":null,"harness":"codex","native_variant_id":null,"provider_model":null,"requested_effort":null,"requested_model":null,"selector_binding":null,"transport":"native-default"}','9a5d70af55f4dd8ced8ca6c5da69f98bd0bb548e9cb0b8daaed17d981343a357','2026-11-11 22:07:51',NULL,'0.146.0','raw-observed-v1','best-effort');
INSERT OR REPLACE INTO "sprint_participant_route_bindings" VALUES(2,1,1,2,'harness-default','codex',NULL,NULL,NULL,NULL,NULL,NULL,'native-default',NULL,NULL,NULL,'{}','{"adapter_metadata":{},"catalogue_generation":null,"contract_version":2,"control_state":"harness-default","effective_effort":null,"evidence_digest":null,"harness":"codex","native_variant_id":null,"provider_model":null,"requested_effort":null,"requested_model":null,"selector_binding":null,"transport":"native-default"}','9a5d70af55f4dd8ced8ca6c5da69f98bd0bb548e9cb0b8daaed17d981343a357','2026-11-11 22:07:51',NULL,'0.146.0','raw-observed-v1','best-effort');
INSERT OR REPLACE INTO "sprint_participant_route_bindings" VALUES(3,2,1,2,'harness-default','codex',NULL,NULL,NULL,NULL,NULL,NULL,'native-default',NULL,NULL,NULL,'{}','{"adapter_metadata":{},"catalogue_generation":null,"contract_version":2,"control_state":"harness-default","effective_effort":null,"evidence_digest":null,"harness":"codex","native_variant_id":null,"provider_model":null,"requested_effort":null,"requested_model":null,"selector_binding":null,"transport":"native-default"}','9a5d70af55f4dd8ced8ca6c5da69f98bd0bb548e9cb0b8daaed17d981343a357','2026-11-11 22:07:51',NULL,'0.146.0','raw-observed-v1','best-effort');
INSERT OR REPLACE INTO "sprint_participants" VALUES(1,1,1,'developer','codex',NULL,NULL,NULL,'assigned','2026-11-11 22:07:51','2026-11-11 22:07:51',2);
INSERT OR REPLACE INTO "sprint_participants" VALUES(2,1,2,'reviewer','codex',NULL,NULL,NULL,'assigned','2026-11-11 22:07:51','2026-11-11 22:07:51',3);
INSERT OR REPLACE INTO "sprint_participants" VALUES(3,1,3,'planner','codex',NULL,NULL,NULL,'assigned','2026-11-11 22:07:51','2026-11-11 22:07:51',1);
INSERT OR REPLACE INTO "sprint_pr_transitions" VALUES(1,1,'created','6bc680eb95846887b69a90ea5d8b6a933cd688a55c319203837c17ede7ac24aa','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','{"base_ref": "main", "base_sha": "cccccccccccccccccccccccccccccccccccccccc", "checks": null, "checks_failed": false, "head_ref": "feature/pr-41", "merge_sha": null, "review_decision": null, "state": "OPEN", "title": "PR 41", "trigger": "registration", "url": "https://github.com/acme/repo/pull/41"}','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_pr_work_units" VALUES(1,1,1,NULL);
INSERT OR REPLACE INTO "sprint_registered_prs" VALUES(1,1,1,'acme/repo',41,'2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_spec_revision_history" VALUES(1,1,1,1,'e9f6f85d88878f26fb604721d3cc7b0dd396bd103707cc833bb408fa2368685b','bound safety-net spec',0,NULL,'planner',3,'binding declared','2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_specs" VALUES(1,1,'e9f6f85d88878f26fb604721d3cc7b0dd396bd103707cc833bb408fa2368685b',NULL,'2026-11-11 22:07:51','bound safety-net spec',0);
INSERT OR REPLACE INTO "sprint_wake_attempts" VALUES(1,1,1,'cv_77427ba9c4a84e0cbfc081b79c21352f','conversation-run:1','delivered',NULL,'2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_wake_attempts" VALUES(2,2,1,'cv_6e231c137f394a4b8754ed6bc5af3e93','conversation-run:2','delivered',NULL,'2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_wake_messages" VALUES(1,1,1);
INSERT OR REPLACE INTO "sprint_wake_messages" VALUES(1,2,2);
INSERT OR REPLACE INTO "sprint_wake_outbox" VALUES(1,1,3,3,'delivered',1,'receiver:3:wake-for-message:1',NULL,'2026-11-11 22:07:51','2026-11-11 22:07:51','2026-11-11 22:07:51',NULL,NULL,NULL,NULL,NULL);
INSERT OR REPLACE INTO "sprint_wake_outbox" VALUES(2,1,1,1,'delivered',1,'receiver:1:wake-for-message:2',NULL,'2026-11-11 22:07:51','2026-11-11 22:07:51','2026-11-11 22:07:51',NULL,NULL,NULL,NULL,NULL);
INSERT OR REPLACE INTO "sprint_work_unit_dependencies" VALUES(1,2,1,'2026-11-11 22:07:51');
INSERT OR REPLACE INTO "sprint_work_unit_tasks" VALUES(1,1,1);
INSERT OR REPLACE INTO "sprint_work_unit_tasks" VALUES(1,2,2);
INSERT OR REPLACE INTO "sprint_work_units" VALUES(1,1,1,2,'First lane','First lane: one merged PR.',0,'active','2026-11-11 22:07:51','2026-11-11 22:07:51',NULL,'code',NULL,NULL);
INSERT OR REPLACE INTO "sprint_work_units" VALUES(2,1,1,2,'Second lane','Second lane: one merged PR.',0,'planned','2026-11-11 22:07:51','2026-11-11 22:07:51',NULL,'code',NULL,NULL);
INSERT OR REPLACE INTO "sprints" VALUES(1,1,3,'armed',1,NULL,'2026-11-11 22:07:51','2026-11-11 22:07:51',NULL,NULL,NULL,'2026-11-11 22:07:51',2,'77bca3ddb4c315ef0ebdb974f8582ef8',0,2,1);
INSERT OR REPLACE INTO "users" VALUES(1,'operator',NULL,NULL,NULL,NULL,1,'2026-11-11 22:07:51');
INSERT OR REPLACE INTO "wake_message" VALUES(1,1,NULL,3,NULL,3,NULL,'notification','Sprint 1 armed. Bound participant routes:
- planner PLN1: codex · model=Harness default · Thinking level=uncontrolled · harness-default · route revision 1
- developer DEV1: codex · model=Harness default · Thinking level=uncontrolled · harness-default · route revision 1
- reviewer REV1: codex · model=Harness default · Thinking level=uncontrolled · harness-default · route revision 1','new',0,NULL,'2026-11-11 22:07:51','2026-11-11 22:07:51',NULL,'sprint:1:arming-model-selections','2026-11-11 22:07:51','information',0,NULL);
INSERT OR REPLACE INTO "wake_message" VALUES(2,1,3,1,3,1,1,'work_assignment','First lane

First lane: one merged PR.

sc context --work-unit 1','force-new',1,'accepted','2026-11-11 22:07:51','2026-11-11 22:07:51',NULL,'sprint:1:work-unit:1:assignment:1','2026-11-11 22:07:51','information',0,NULL);
