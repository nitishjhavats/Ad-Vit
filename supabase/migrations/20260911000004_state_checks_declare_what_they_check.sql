-- =============================================================================
-- A state_check must say, as data, what state it checks
--
-- 20260912000001 made an industry a row. That is not, on its own, enough to
-- make a SECOND PACK free of code, and it is worth being exact about where the
-- line falls:
--
--   term_list, regex, llm_judge  - already pure data. The RERA "assured
--                                  returns" list in 04_real_estate_pack.sql is
--                                  an INSERT and nothing else, exactly as
--                                  Schedule J is.
--   state_check                  - was pure code. ComplianceGate._check_state
--                                  dispatches on rule.code, with a hand-written
--                                  branch for IN_AYUSH_LICENCE_ON_FILE reading
--                                  two named fields. A RERA registration check
--                                  written the same way is a Python change and
--                                  a deploy - which is the thing "industries as
--                                  data" was supposed to remove.
--
-- So a state_check now declares its requirement as data. `required_facts` is a
-- list of fact keys that must be resolvable and non-empty for the rule to pass:
-- IN_AYUSH_LICENCE_ON_FILE becomes {ayush_licence_no, product_classification},
-- IN_RERA_REGISTRATION_ON_FILE becomes {rera_registration_no,
-- rera_authority_url}, and the gate reads the list rather than the rule code.
--
-- One rule genuinely resists this and keeps a coded handler.
-- META_AI_DISCLOSURE_REQUIRED is not asking whether a credential is on file: it
-- is a tri-state over the bundle's own media, where undeclared is itself the
-- violation and only when media is present. It now says so in a column instead
-- of being recognised by name.
--
-- WHAT THIS DOES NOT DO, stated plainly rather than left to be discovered.
-- Declaring a fact key is not the same as being able to resolve one.
-- t_advit.catalog_products supplies ayush_licence_no and
-- product_classification (20260911000004 made the resolution per-product and
-- refusable). Nothing in this schema can supply rera_registration_no: a real
-- estate project is not a SKU, and there is no project record to hang one on.
-- So the seeded RERA state_check resolves to nothing and the gate reports stage
-- 9 as NOT EVALUATED for that pack - which is the correct behaviour for a guard
-- with nothing to compare against, and puts the gap in front of the operator
-- instead of in a document. Closing it means a subject table, and that is a
-- migration; see the note at the foot of this file.
-- =============================================================================

alter table t_advit.policy_rules
  add column required_facts text[] not null default '{}',
  add column state_handler  text;

comment on column t_advit.policy_rules.required_facts is
  'Fact keys that must be on file for this state_check to pass. This is what '
  'makes a second pack''s licence rule an INSERT: {rera_registration_no} rather '
  'than a new branch in ComplianceGate._check_state. A key the runtime cannot '
  'resolve leaves the stage NOT EVALUATED - never passed.';

comment on column t_advit.policy_rules.state_handler is
  'Names a coded handler for the rare state_check that is not a credential '
  'lookup - today only META_AI_DISCLOSURE_REQUIRED. Non-null means the gate '
  'must recognise the name; an unrecognised handler is reported as NOT '
  'EVALUATED, never as a pass, on the same reasoning as UNIMPLEMENTED_STAGES.';

update t_advit.policy_rules
   set required_facts = array['ayush_licence_no', 'product_classification']
 where code = 'IN_AYUSH_LICENCE_ON_FILE';

update t_advit.policy_rules
   set required_facts = array['lead_form_consent_notice_url']
 where code = 'IN_DPDP_CONSENT_NOTICE';

update t_advit.policy_rules
   set state_handler = 'meta_ai_disclosure'
 where code = 'META_AI_DISCLOSURE_REQUIRED';


-- A state_check that names neither a fact nor a handler has nothing to compare
-- against, so it returns no findings - and no findings reads as a pass. That is
-- the failure this codebase keeps meeting from different directions: an empty
-- set treated as a clean result rather than as an absent one. The constraint
-- refuses the row instead of letting a half-written rule ship as a silent
-- approval.
--
-- t_advit.distinct_count (20260910000002) is IMMUTABLE and reads only its
-- argument, so it is usable inside a CHECK. array_length would return NULL on
-- '{}' - and a CHECK passes on NULL, which is precisely how the industry
-- independence gate was bypassed.
alter table t_advit.policy_rules
  drop constraint policy_rules_has_matcher;

alter table t_advit.policy_rules
  add constraint policy_rules_has_matcher check (
    (rule_type = 'regex'     and pattern is not null)
    or (rule_type = 'term_list' and terms is not null)
    or (rule_type = 'llm_judge')
    or (rule_type = 'state_check'
        and (t_advit.distinct_count(required_facts) > 0 or state_handler is not null))
  );

comment on constraint policy_rules_has_matcher on t_advit.policy_rules is
  'Every rule must carry something to adjudicate with. For a state_check that '
  'means a fact list or a named handler: a state_check with neither would '
  'silently pass every bundle it was ever given.';


-- ---------------------------------------------------------------------------
-- The follow-up this migration deliberately does not perform
--
-- rera_registration_no has no home. The generic shape is a subject-keyed fact
-- store, roughly:
--
--   create table t_advit.compliance_facts (
--     workspace_id uuid not null references t_advit.workspaces(id) on delete cascade,
--     subject_type text not null default 'workspace',   -- workspace | catalog_product | project
--     subject_id   uuid,
--     fact_key     text not null,
--     fact_value   text not null,
--     ...
--     constraint compliance_facts_one_per_subject
--       unique nulls not distinct (workspace_id, subject_type, subject_id, fact_key)
--   );
--
-- It is not created here on purpose. 20260911000004 has just made
-- t_advit.catalog_products the resolution point for the AYUSH facts, with a
-- written rationale and a per-product refusal path. Adding a second store for
-- the same two facts today would recreate exactly the defect industry_pack_id
-- was carrying: two overlapping notions of where the truth lives, drifting
-- apart quietly. The store lands when the first pack that actually needs it
-- does - together with the subject table it keys off - not before.
-- ---------------------------------------------------------------------------

grant all on all tables    in schema t_advit to service_role;
grant all on all sequences in schema t_advit to service_role;
grant all on all functions in schema t_advit to service_role;
