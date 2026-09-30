-- =============================================================================
-- Fix: the proximity windows bridged a line break
--
-- Three seeded rules pair a trigger term with a nearby second term across a
-- window of "anything that is not a sentence terminator":
--
--   META_OUTCOME_TIMELINE          [^.!?]{0,40}   (twice, both orders)
--   META_PA_SECOND_PERSON_HEALTH   [^.!?]{0,60}
--   META_PA_CONDITIONAL_DIAGNOSIS  [^.!?]{0,60}
--
-- A newline is not a sentence terminator, so the window walked straight over
-- it. Reproduced against the seeded ruleset, inside a SINGLE primary_text:
--
--     "- Ships in 3 days\n- Relief-focused formula"
--       -> META_OUTCOME_TIMELINE matches "in 3 days\n- Relief"   BLOCK
--
-- Two bullet points, one about shipping and one about the product, read as a
-- promise of relief in three days. Ad copy is written in short lines and
-- bulleted lists, so this is ordinary copy, not a contrived case - and a BLOCK
-- short-circuits the owner's whole run. A false-block rate that is too high
-- teaches owners to override the gate, which is as much a defect as a miss
-- (PRD 13.4).
--
-- The same window was ALSO bridging separate fields, because the orchestrator
-- joined primary_text, headline and description into one string before handing
-- them to the gate. That half is fixed in the application - the fields now
-- travel separately through CreativeBundle.copy_fields() - and this migration
-- fixes what remained inside one field.
--
-- Deliberately narrow. A blank line is not a sentence terminator either, but
-- the terminators stay as they are: widening the class further starts dropping
-- real claims, and "Sirf 7 din mein result" on one line still matches, which is
-- what the rule is for.
-- =============================================================================

do $fix$
declare
  v_updated integer;
begin
  update t_advit.policy_rules
     set pattern = replace(pattern, '[^.!?]', '[^.!?\n]')
   where code in (
           'META_OUTCOME_TIMELINE',
           'META_PA_SECOND_PERSON_HEALTH',
           'META_PA_CONDITIONAL_DIAGNOSIS'
         )
     and pattern like '%[^.!?]%';

  get diagnostics v_updated = row_count;

  -- A migration that silently changed nothing is indistinguishable from one
  -- that ran against a database where the rules were never seeded. Say which.
  raise notice 'proximity windows narrowed on % rule(s)', v_updated;
end;
$fix$;

comment on column t_advit.policy_rules.pattern is
  'Postgres ARE. Proximity windows are written [^.!?\n]{0,N}: a newline ends the '
  'window, because ad copy is written in short lines and a bulleted shipping '
  'promise beside a bulleted product benefit is not a timeline-to-result claim. '
  'app/policy/rules.py translates \m and \M to \b and the POSIX classes to their '
  'Python equivalents; \n needs no translation and means the same in both.';

grant all on all tables    in schema t_advit to service_role;
grant all on all sequences in schema t_advit to service_role;
grant all on all functions in schema t_advit to service_role;
