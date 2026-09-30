-- =============================================================================
-- The industry packs are platform data, not local fixtures
--
-- 02_ayurveda_pack.sql and 04_real_estate_pack.sql were seeds: the Meta and
-- Indian-law rulesets, their industry scoping, the T1 context defaults a new
-- workspace starts out believing, and the T3 platform knowledge the agent
-- cites. They were applied to production ONCE, on 2026-09-11, as seeds - and
-- a seed is never applied twice, so the next edit to a rule (a Schedule J term
-- added, a Meta as_of moved) would have stayed local for ever while the
-- production gate went on judging copy against the old list.
--
-- A rule is what the compliance gate DOES. It moves the way code moves: as a
-- migration, forward-only, applied by the one path that reaches production.
-- This file is the two packs verbatim, with one change - the platform_knowledge
-- insert gains a not-exists guard, because that table has a generated key and
-- no natural one. On production every other statement is a no-op through its
-- `on conflict ... do nothing`; the rules keep whatever as_of the Platform
-- Watch re-verification has written since, because a rule's date is the
-- operator's to move and a migration must not move it back.
--
-- Future pack changes are new migrations that UPDATE the row by code. The
-- PROVENANCE and NOT-LEGAL-ADVICE commitments of the original files stand and
-- are kept verbatim below.
-- =============================================================================

-- =============================================================================
-- Ayurveda / AYUSH industry pack - compliance ruleset (PRD 13, 15)
--
-- Two layers, both blocking: Meta's advertising policy and Indian law. An
-- Ayurveda advertiser must satisfy both simultaneously.
--
-- PROVENANCE IS PART OF THE RULE. Every row carries source_url and as_of
-- because a compliance gate that says only "blocked because policy" is
-- unusable - and because half of this will be stale within a year (PRD 13.5).
--
-- THIS IS NOT LEGAL ADVICE. PRD 4.5: the OS advises, it does not opine. The
-- Schedule J term list below is encoded from public summaries of the Drugs &
-- Magic Remedies (Objectionable Advertisements) Act and MUST be verified
-- against the current Schedule as amended, with counsel, before it is relied
-- on commercially. Rules carrying needs_legal_verification in their
-- explanation are flagged in the UI as requiring confirmation.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Layer 1 - Meta advertising policy, 2026 state
-- ---------------------------------------------------------------------------

insert into t_advit.policy_rules
  (code, jurisdiction, instrument, gate_stage, rule_type, title, pattern, terms,
   severity, explanation, remedy_template, source_url, as_of,
   scope, required_facts, state_handler)
values

-- Stage 3: personal attributes. The 2026 crackdown extends to IMPLIED
-- attributes, which is what catches most Ayurveda copy.
('META_PA_SECOND_PERSON_HEALTH', 'meta', 'meta_personal_attributes', 3, 'regex',
 'Second-person health framing',
 -- The pronoun list used to carry `你`, which is Chinese for "you" and cannot
 -- occur in Hindi, Hinglish or Devanagari ad copy. It was standing in for a
 -- Devanagari alternative that was never written, so the rule had no Devanagari
 -- coverage at all - and among Latin forms it omitted bare "aap", which is the
 -- commonest second-person framing in this market ("Aap pareshan hain?").
 --
 -- No \m/\M assertions anywhere in this pattern, and that is deliberate rather
 -- than an oversight: आपको ends in a vowel sign (U+094B, category Mc), which \w
 -- does not match, so a trailing word-boundary assertion would make the
 -- Devanagari branches unmatchable. The 60-character same-sentence window
 -- ([^.!?\n]) is what keeps this from over-firing.
 '(are you|do you|kya aap|aap ?ko|aap|tumhe|tumko|क्या आप|आपको|आप|तुम्हें|तुमको)[^.!?\n]{0,60}(suffer|suffering|pain|problem|piles|bawasir|bavasir|diabetes|obesity|hair ?fall|acne|pareshan|dard|takleef|परेशान|दर्द|तकलीफ|तकलीफ़|बवासीर|मधुमेह|मोटापा|मुंहासे|बाल झड़)',
 null,
 'block',
 'Meta prohibits asserting or implying knowledge of the viewer''s medical condition. '
 'Second-person health framing ("are you suffering from...", "aapko bawasir hai?") is '
 'the single fastest route to rejection and, repeated, to account restriction.',
 'Rewrite feature-forward and in the third person: describe what the product is and '
 'who it is for, never what the viewer is assumed to suffer from. '
 'Instead of "Are you suffering from piles?", use "An Ayurvedic formulation used in '
 'traditional practice for digestive comfort."',
 'https://www.facebook.com/policies/ads/prohibited_content/personal_attributes',
 '2026-03-01', 'all_industries', '{}', null),

('META_PA_CONDITIONAL_DIAGNOSIS', 'meta', 'meta_personal_attributes', 3, 'regex',
 'Conditional diagnosis phrasing',
 '(if you (have|are|were)|agar aap ?ko|jinko|jin logon ko)[^.!?\n]{0,60}(diagnos|condition|disease|bimari|rog|problem)',
 null,
 'block',
 'Conditional phrasing ("if you have been diagnosed with...") implies knowledge of the '
 'viewer''s condition just as directly as an assertion does, and is treated the same way.',
 'Remove the conditional targeting of a condition. Describe the product category instead.',
 'https://www.facebook.com/policies/ads/prohibited_content/personal_attributes',
 '2026-03-01', 'all_industries', '{}', null),

-- Stage 4: outcome and timeline claims.
('META_OUTCOME_GUARANTEE', 'meta', 'meta_misleading_claims', 4, 'regex',
 'Guaranteed or absolute outcome',
 -- `guarantee[ds]?` used to stand alone, with no boundary and no tie to an
 -- outcome, so this BLOCK rule fired on "money-back guarantee", "authenticity
 -- guarantee" and "replacement guarantee" - ordinary commercial terms, in every
 -- industry, since the rule is scoped all_industries. `100% ?guarantee` had the
 -- same problem for "100% money back guarantee".
 --
 -- A guarantee is only what this rule is named for - an absolute OUTCOME claim -
 -- when it is attached to an outcome. So the guarantee branches now require one
 -- within the same sentence, in either order, while the genuinely absolute
 -- constructions (100% cure, permanent cure, pakka ilaj) stay unconditional.
 --
 -- `jad se khatam` is widened to `jad se` + an elimination verb. That is not
 -- scope creep: META_OUTCOME_CURE_VERB drops the bare term `jad se`, because in
 -- Hindi it means "from the root" and Ayurvedic copy uses it literally about the
 -- botanical source. Narrowing there without widening here would have traded a
 -- false positive for a false negative on a cure claim.
 --
 -- The Devanagari branches appear twice, in both orders. Hindi is head-final, so
 -- "इलाज की गारंटी" - guarantee AFTER the outcome - is the natural way to say it,
 -- and it is the one a translator writing guarantee-first would miss. The Latin
 -- branches already covered both directions.
 '(100% ?(cure|result|effective)'
 '|guarantee[ds]?[^.!?\n]{0,30}\m(results?|cure|relief|ilaj|ilaaj|aaram|khatam|thik|theek|fayda)'
 '|\m(results?|cure|relief|ilaj|ilaaj|aaram|khatam)\M[^.!?\n]{0,30}guarantee[ds]?'
 '|गारंटी[^.!?\n]{0,30}(इलाज|आराम|नतीजा|परिणाम|ठीक|खत्म)'
 '|(इलाज|आराम|नतीजा|परिणाम|ठीक|खत्म)[^.!?\n]{0,30}गारंटी'
 '|permanent(ly)? (cure|solution)'
 '|pakka ilaj'
 '|jad se[^.!?\n]{0,20}(khatam|khatm|mita|door|gayab|saaf)'
 '|जड़ से[^.!?\n]{0,20}(खत्म|मिटा|दूर|गायब|साफ))',
 null,
 'block',
 'Guarantees of cure or outcome are prohibited independently by Meta''s health policy '
 'and by Indian law. There is no phrasing that makes a guaranteed-cure claim compliant.',
 'State the traditional use without promising an outcome. Remove every guarantee, '
 '"permanent", and "100%" construction.',
 'https://www.facebook.com/policies/ads/prohibited_content/misleading_claims',
 '2026-03-01', 'all_industries', '{}', null),

('META_OUTCOME_TIMELINE', 'meta', 'meta_misleading_claims', 4, 'regex',
 'Timeline-to-result claim',
 -- Matched in both orders, because real copy uses both:
 --   duration then outcome  - "7 din mein result", "just 30 days relief"
 --   outcome then duration  - "Results in 15 days", "Relief within a week"
 -- The leading preposition is optional: Hinglish routinely omits it, and an
 -- English-shaped pattern that requires it misses those claims entirely.
 -- Hindi nouns take an OBLIQUE PLURAL before a postposition, which is exactly
 -- the construction this rule targets: "7 dino mein", "2 mahino mein",
 -- "3 hafton mein". Those end in a different syllable from din/mahine/hafte, so
 -- the \M assertion failed and the commonest Hinglish timeline claims passed.
 -- The rule's own comment claimed Hinglish coverage; it had the citation forms
 -- only.
 --
 -- The Devanagari alternatives sit OUTSIDE the \M assertion on purpose:
 -- दिनों and महीनों end in an anusvara (U+0902, category Mn), which \w does not
 -- match, so a trailing boundary would make them unmatchable - the same trap
 -- that hides matra-final terms from the term-list matcher.
 '((in|within|just|only|sirf|keval|bas)?[[:space:]]*[0-9]{1,3}[[:space:]]*'
 '((days?|weeks?|months?|din|dinon|dino|hafton|hafte|mahinon|mahino|mahine)\M'
 '|दिनों|दिन|हफ़्तों|हफ्तों|हफ़्ते|हफ्ते|महीनों|महीने)'
 '[^.!?\n]{0,40}'
 '(\m(results?|cure|relief|gone|thik|theek|khatam|aaram|asar|farak|farq|fayda)\M'
 '|नतीजा|परिणाम|आराम|ठीक|खत्म|असर|फर्क|फायदा))'
 '|((\m(results?|cure|relief|thik|theek|khatam|aaram|asar|farak|farq|fayda)\M'
 '|नतीजा|परिणाम|आराम|ठीक|खत्म|असर|फर्क|फायदा)'
 '[^.!?\n]{0,40}[0-9]{1,3}[[:space:]]*'
 '((days?|weeks?|months?|din|dinon|dino|hafton|hafte|mahinon|mahino|mahine)\M'
 '|दिनों|दिन|हफ़्तों|हफ्तों|हफ़्ते|हफ्ते|महीनों|महीने))',
 null,
 'block',
 'A promised time to result is rejected as a misleading transformation claim even '
 'without the word "guarantee" - and especially when paired with transformation imagery.',
 'Remove the timeline. Traditional-use framing carries no promised schedule.',
 'https://www.facebook.com/policies/ads/prohibited_content/misleading_claims',
 '2026-03-01', 'all_industries', '{}', null),

('META_OUTCOME_CURE_VERB', 'meta', 'meta_misleading_claims', 4, 'term_list',
 'Explicit cure language', null,
 -- `jad se` is gone. In Hindi जड़ means root and "jad se" means "from the
 -- root" - a phrase Ayurvedic copy uses LITERALLY about the botanical source
 -- ("neem ki jad se banaya gaya"). As a standalone block term it rejected
 -- honest ingredient copy. The cure claim is the full idiom, "jad se khatam /
 -- mitaye / door", and META_OUTCOME_GUARANTEE now matches that directly rather
 -- than only the one spelling it had before.
 array['cure','cures','cured','curing','ilaj','ilaaj','nivaran','permanent solution',
       'इलाज','निवारण'],
 'block',
 'Meta''s health policy prohibits cure claims for any condition, independently of '
 'whether Indian law also prohibits advertising that condition.',
 'Replace cure language with traditional-use or wellness-support framing.',
 'https://www.facebook.com/policies/ads/prohibited_content/misleading_claims',
 '2026-03-01', 'all_industries', '{}', null),

-- Stage 5: imagery. The policy expanded in 2026 - a product shown beside a
-- conspicuously fit or healthy person now receives before/after treatment.
('META_IMAGERY_BEFORE_AFTER', 'meta', 'meta_health_imagery', 5, 'llm_judge',
 'Before/after or transformation imagery', null, null,
 'block',
 'Split-frame before/after imagery is prohibited, and since 2026 so is placing the '
 'product alongside imagery of a visibly transformed, fit or healthy person used as a '
 'proxy for the same claim.',
 'Show the product, its ingredients, preparation or packaging. Remove any paired '
 'state-change visual.',
 'https://www.facebook.com/policies/ads/prohibited_content/adult_health',
 '2026-03-01', 'all_industries', '{}', null),

-- Stage 6: AI disclosure. Undisclosed AI content is roughly 14% of all
-- rejections - the third-largest category.
('META_AI_DISCLOSURE_REQUIRED', 'meta', 'meta_ai_disclosure', 6, 'state_check',
 'AI-generated content must be declared', null, null,
 'block',
 'Disclosure is required for AI-generated or substantially AI-modified imagery, '
 'background replacement, face or body modification beyond standard filters, synthetic '
 'voiceover, and substantially AI-edited video. Meta also auto-labels from C2PA metadata, '
 'so an undeclared asset can be labelled anyway - and counted against the account.',
 'Set the AI-generation state on the creative record at upload, and apply Meta''s '
 'disclosure where it applies.',
 'https://transparency.meta.com/en-gb/policies/ad-standards/',
 '2026-03-01', 'all_industries', '{}', 'meta_ai_disclosure'),

-- ---------------------------------------------------------------------------
-- Layer 2 - Indian law and self-regulation
-- ---------------------------------------------------------------------------

-- Stage 2: the hard line. If the marketing touches a Schedule J condition, the
-- DMR Act applies regardless of AYUSH or allopathic classification.
('IN_DMRA_SCHEDULE_J', 'in', 'dmr_act_schedule_j', 2, 'term_list',
 'Schedule J prohibited condition', null,
 -- Hinglish and Devanagari were present for ONE condition (piles: bawasir,
 -- bavasir) and no other, even though Indian Ayurveda copy uses the Indian name
 -- in preference to the English one for nearly all of these. IN_DMRA_SCHEDULE_J
 -- is the only rule in the India layer that can BLOCK, so every missing name was
 -- a Schedule J condition advertisable in the language the market actually
 -- writes in.
 --
 -- The Devanagari half of this list only works because _term_pattern was fixed
 -- alongside it: matras are not \w characters, so under \b(term)\b the five
 -- matra-final names below (मोटापा, नपुंसकता, मिर्गी, लकवा, पथरी) could never
 -- have matched. They would have sat here looking like coverage.
 --
 -- DELIBERATELY NOT ADDED, because each would fire on ordinary copy:
 --   'sugar'   - "no added sugar" is on half the food and supplement ads in
 --               India; blocking it would make this rule unusable
 --   'bp','tb' - two letters, matching inside nothing and beside everything
 --   'kamzori' - "weakness" on its own is generic; the Schedule J sense is
 --               carried by 'mardana kamzori', which IS listed
 --   'arsh'    - a common given name as well as a term for piles
 --
 -- needs_legal_verification still applies to every line of this, including the
 -- transliterations: these are the names the market uses, not a legal gloss.
 array[
   -- diabetes
   'diabetes','madhumeh','मधुमेह',
   -- cancer
   'cancer','कैंसर',
   -- obesity
   'obesity','motapa','मोटापा',
   -- mental illness
   'mental illness','mental retardation','insanity','पागलपन',
   -- alopecia and baldness
   'alopecia','baldness','ganjapan','गंजापन',
   -- leucoderma and vitiligo
   'leucoderma','vitiligo','safed daag','safed dag','सफेद दाग',
   -- sexual impotence
   'sexual impotence','impotence','napunsakta','नपुंसकता',
   'mardana kamzori','मर्दाना कमजोरी','shighrapatan','शीघ्रपतन',
   -- premature ageing
   'premature ageing','premature aging',
   -- deafness
   'deafness','bahrapan','बहरापन',
   -- epilepsy
   'epilepsy','fits','mirgi','मिर्गी',
   -- glaucoma and cataract
   'glaucoma','kala motia','काला मोतिया','cataract','motiyabind','मोतियाबिंद',
   -- blood pressure
   'high blood pressure','low blood pressure','hypertension',
   'uchch raktchap','रक्तचाप',
   -- hernia
   'hernia','हर्निया',
   -- leprosy
   'leprosy','kusht rog','कुष्ठ रोग',
   -- paralysis
   'paralysis','lakwa','लकवा','pakshaghat','पक्षाघात',
   -- tuberculosis
   'tuberculosis','kshay rog','क्षय रोग',
   -- venereal disease
   'venereal disease','gupt rog','गुप्त रोग',
   -- hydrocele
   'hydrocele','अंडवृद्धि',
   -- nervous debility
   'nervous debility','snayu durbalta',
   -- varicose vein
   'varicose vein','varicose veins',
   -- height
   'stature improvement','increase height','lambai badhaye','height badhaye',
   'लंबाई बढ़ाएं',
   -- piles, haemorrhoids, fistula
   'piles','haemorrhoids','hemorrhoids','bawasir','bavasir','बवासीर','मूलव्याध',
   'fistula','bhagandar','भगंदर',
   -- gangrene
   'gangrene',
   -- urinary stones
   'stones in the urinary system','kidney stone','pathri','पथरी',
   -- sexual pleasure
   'sexual pleasure','improve sex',
   -- spermatorrhoea
   'spermatorrhoea','dhat rog','धात रोग','swapndosh','स्वप्नदोष',
   -- sterility and infertility
   'female sterility','infertility','bandhyapan','बांझपन','santan prapti',
   -- HIV
   'aids','hiv','एड्स'
 ],
 'block',
 'The Drugs & Magic Remedies (Objectionable Advertisements) Act prohibits advertising a '
 'remedy for the conditions listed in Schedule J. There is no clever framing that makes '
 'this legal - this is a legal boundary, not a copywriting problem. '
 'needs_legal_verification: this list is encoded from public summaries and MUST be '
 'confirmed against the current Schedule as amended, with counsel, before commercial reliance.',
 'A Schedule J condition cannot be named as something the product remedies. Where a '
 'legitimate reframing exists, describe the product category and its traditional use '
 'without naming the condition or implying a remedy for it. Where none exists, say so plainly.',
 'https://www.indiacode.nic.in/handle/123456789/1391',
 '2026-08-01', 'listed_industries', '{}', null),

('IN_AYUSH_CLASSICAL_ANCHOR', 'in', 'ayush_guidelines', 4, 'llm_judge',
 'Claim not anchored to a classical text', null, null,
 'warn',
 'Ministry of AYUSH guidance anchors permitted claims to classical texts (Charaka '
 'Samhita, Sushruta Samhita, Ashtanga Hridayam) and frames them as wellness support '
 'rather than medical outcomes.',
 'Reframe as wellness support with a classical-text reference where one genuinely '
 'applies. Do not invent a citation.',
 'https://www.ayush.gov.in/',
 '2026-08-01', 'listed_industries', '{}', null),

('IN_AYUSH_PRACTITIONER_CREDENTIALS', 'in', 'ayush_guidelines', 4, 'llm_judge',
 'Practitioner endorsement without visible credentials', null, null,
 'warn',
 'A practitioner endorsement requires a registered practitioner with visible '
 'credentials. A customer testimonial that makes a medical claim is still a medical claim.',
 'Show registration details, or remove the endorsement framing.',
 'https://www.ayush.gov.in/',
 '2026-08-01', 'listed_industries', '{}', null),

('IN_ASCI_MISLEADING_HEALTH', 'in', 'asci_code', 4, 'llm_judge',
 'ASCI misleading health advertising', null, null,
 'warn',
 'ASCI is self-regulatory but consequential: it refers misleading health advertising to '
 'the AYUSH ministry and to the CCPA. Disclaimers must be legible and must not '
 'contradict the main claim.',
 'Align the disclaimer with the claim, or soften the claim.',
 'https://www.ascionline.in/the-asci-code/',
 '2026-08-01', 'listed_industries', '{}', null),

-- Stage 9: licence posture. Misclassification is the most common root cause of
-- an "unfixable" rejection, because the legal class decides which claim set is
-- even available.
('IN_AYUSH_LICENCE_ON_FILE', 'in', 'ayush_guidelines', 9, 'state_check',
 'AYUSH licence and product classification', null, null,
 'block',
 'AYUSH licensing must be in place before the ad runs, and the product''s legal '
 'classification - Ayurvedic drug, food/nutraceutical, or cosmetic - must be consistent '
 'with what the copy claims.',
 'Record the AYUSH licence number and the product classification on the product record, '
 'and align the claim set to that classification. Where the workspace holds more than one '
 'product, name which one the creative is for - the gate checks the licence of the product '
 'being advertised and will not infer it from the copy.',
 'https://www.ayush.gov.in/',
 '2026-08-01', 'listed_industries',
 array['ayush_licence_no', 'product_classification'], null),

-- Stage 8: consent. Not implemented in this build - the gate reports it as
-- not_evaluated rather than passing it silently.
('IN_DPDP_CONSENT_NOTICE', 'in', 'dpdp_act', 8, 'state_check',
 'DPDP consent notice on lead capture', null, null,
 'block',
 'Consent must be free, specific, informed, unambiguous and given by clear affirmative '
 'action, with logged consent and a withdrawal path as easy as the grant. This applies '
 'directly to Meta instant forms and WhatsApp opt-ins. Penalties reach INR 250 crore '
 'per contravention.',
 'Attach a compliant consent notice to the lead form and record consent state alongside '
 'every lead.',
 'https://www.meity.gov.in/data-protection-framework',
 '2025-11-14', 'all_industries', array['lead_form_consent_notice_url'], null)

-- Re-runnable, like 04_real_estate_pack.sql. Without this the statement aborts
-- on the first code it has already seeded, and everything below it - the rule
-- scoping and the pack's T1 defaults - never runs at all.
on conflict (code) do nothing;

-- The five rules above that belong to this pack rather than to every pack.
-- Scope lives in a junction table now rather than in an enum array, because
-- Postgres cannot foreign-key array elements, and because "applies to everyone"
-- had to stop being spelled the same way as "nobody filled this in".
--
-- The constraint trigger holding scope and these rows in agreement is DEFERRED,
-- so the order of the two statements does not matter inside one transaction.

insert into t_advit.policy_rule_industries (rule_code, industry_key)
values
  ('IN_DMRA_SCHEDULE_J',                'ayurveda'),
  ('IN_AYUSH_CLASSICAL_ANCHOR',         'ayurveda'),
  ('IN_AYUSH_PRACTITIONER_CREDENTIALS', 'ayurveda'),
  ('IN_ASCI_MISLEADING_HEALTH',         'ayurveda'),
  ('IN_AYUSH_LICENCE_ON_FILE',          'ayurveda')
on conflict do nothing;

-- ---------------------------------------------------------------------------
-- What this pack hands a new workspace as starting T1 memory
--
-- Lifted out of supabase/seeds/03_marketing_workspace.sql, where they were
-- written by hand against one workspace and labelled source = 'industry_pack' -
-- a provenance the schema could not back, because no industry pack existed to
-- source them from. They belong to the pack, not to Demo Brand.
-- ---------------------------------------------------------------------------

insert into t_advit.industry_context_defaults
  (industry_key, dimension, key, value_json, confidence)
values
  ('ayurveda', 'compliance', 'category_sensitivity',
   to_jsonb('high'::text), 0.900),

  ('ayurveda', 'compliance', 'sensitivity_rationale',
   to_jsonb(
     'Buyers will not say this problem aloud on a phone call. PRD 11.5: for sensitive '
     'categories WhatsApp ranks first, instant form second, click-to-call last - and '
     'this outranks a cheaper CPL.'::text
   ), 0.900),

  ('ayurveda', 'compliance', 'schedule_j_exposure',
   to_jsonb(
     'The product category may fall under DMR Act Schedule J. Every creative must clear '
     'the stage-2 gate, and the Schedule J term list requires legal verification before '
     'commercial reliance.'::text
   ), 0.700)
on conflict (industry_key, dimension, key) do nothing;

-- ---------------------------------------------------------------------------
-- T3 platform knowledge (PRD 15) - what the agent cites when it explains itself
-- ---------------------------------------------------------------------------

-- platform_knowledge has a generated uuid key and no natural one, so this
-- insert guards on the statement text: a re-run adds nothing, and a row an
-- operator has since re-verified (as_of moved forward by the console) is
-- left exactly as they left it.
insert into t_advit.platform_knowledge (topic, statement, source_url, as_of, severity)
select v.topic, v.statement, v.source_url, v.as_of::date, v.severity
  from (values
('attribution',
 'The 7-day and 28-day view-through attribution windows were removed on 12 January 2026. '
 'A year-over-year ROAS decline during 2026 is partly an accounting change, not a '
 'performance collapse. Never compare pre- and post-January figures without normalising.',
 'https://www.facebook.com/business/help', '2026-01-12', 'breaking'),

('attribution',
 'From March 2026 click-through attribution counts link clicks only; other interactions '
 'moved to a 1-day engage-through window, and the video engaged-view threshold dropped '
 'from 10 seconds to 5.',
 'https://www.facebook.com/business/help', '2026-03-01', 'breaking'),

('learning_phase',
 'An ad set exits the learning phase after roughly 50 optimisation events per week. '
 '"Learning limited" is a statement about data volume, not a verdict on the ads. '
 'Fragmentation is the most common cause of permanent learning-limited status in small accounts.',
 'https://www.facebook.com/business/help', '2026-08-01', 'informational'),

('retrieval',
 'Andromeda reads creative content directly and decides which ads are eligible for the '
 'auction. Near-duplicate creatives collapse under a shared Entity ID and cannibalise '
 'each other; keep pairwise similarity below about 0.40, with suppression around 0.60. '
 'Creative diversity is now an eligibility requirement, not an optimisation nicety.',
 'https://www.facebook.com/business/news', '2026-08-01', 'advisory'),

('advantage_plus',
 'Legacy Advantage+ Shopping and App campaign creation, duplication and updates were '
 'blocked across all API versions from 19 May 2026, and remaining legacy campaigns are '
 'being paused as v26 rolls forward. Migration is a P1 task at onboarding.',
 'https://developers.facebook.com/docs/graph-api/changelog', '2026-05-19', 'breaking'),

('measurement',
 'Pixel and CAPI events for the same conversion must share an event_id. Without '
 'deduplication, reported volume inflates roughly 1.5-2x and every downstream decision '
 'is made on inflated numbers.',
 'https://developers.facebook.com/docs/marketing-api/conversions-api', '2026-08-01', 'advisory'),

('measurement',
 'Event Match Quality scores 0-10 how confidently Meta can match a server-side event to '
 'a person. Normalising phone numbers to E.164 before hashing is the single '
 'highest-leverage fix available to most Indian accounts.',
 'https://developers.facebook.com/docs/marketing-api/conversions-api', '2026-08-01', 'advisory'),

('economics',
 'Click-to-WhatsApp ads open a 72-hour window in which all message categories are free. '
 'This is the single most important economic fact in the CTA decision model for India.',
 'https://developers.facebook.com/docs/whatsapp/pricing', '2026-01-01', 'informational')
  ) as v(topic, statement, source_url, as_of, severity)
 where not exists (select 1 from t_advit.platform_knowledge k where k.statement = v.statement);


-- =============================================================================
-- Real estate / RERA industry pack
--
-- A pack is exactly four kinds of row:
--
--   1. one   t_advit.industries row                  - the identity
--   2. N     t_advit.policy_rules rows               - the ruleset
--   3. N     t_advit.policy_rule_industries rows     - what those rules scope to
--   4. 0..N  t_advit.industry_context_defaults rows  - what a new workspace
--                                                        in this industry starts
--                                                        out believing
--
-- Deliberately NOT here: t_advit.industry_patterns. T2 industry truth cannot
-- be seeded. The independence gate requires 3 distinct workspaces under 2
-- distinct owners (20260910000002), so a brand new pack correctly knows nothing
-- yet and has to earn it.
--
-- PROVENANCE IS PART OF THE RULE, and THIS IS NOT LEGAL ADVICE - the same two
-- commitments as the Ayurveda pack. The RERA rules below are encoded from the
-- public text of the Act and MUST be verified with counsel, and against the
-- relevant STATE authority's rules, before commercial reliance. RERA is
-- administered state by state: Maharashtra's MahaRERA advertising requirements
-- are not Karnataka's, and this pack encodes only the central provisions.
-- =============================================================================

insert into t_advit.industries
  (key, display_name, status, pack_version, summary, statutory_note)
values
  ('real_estate', 'Real estate (RERA)', 'active', 1,
   'Developers, promoters and brokers marketing plots, apartments and projects in India.',
   'Real Estate (Regulation and Development) Act 2016: s.3(1) forbids advertising an '
   'unregistered project, s.11(2) requires the registration number and the authority''s '
   'website on every advertisement, s.59 sets the penalty at up to 10% of estimated '
   'project cost. Enforced state by state - this pack encodes the central provisions only.')
on conflict (key) do nothing;


-- ---------------------------------------------------------------------------
-- The ruleset
--
-- Stage 9 is licence posture, which for Ayurveda means the AYUSH licence and
-- the product classification. For real estate it means the RERA registration
-- and the authority whose register it sits in. Structurally the same check; the
-- only thing that differs is the list of fact keys, and that is data.
-- ---------------------------------------------------------------------------

insert into t_advit.policy_rules
  (code, jurisdiction, instrument, gate_stage, rule_type, title, pattern, terms,
   severity, explanation, remedy_template, source_url, as_of, scope, required_facts)
values

('IN_RERA_REGISTRATION_ON_FILE', 'in', 'rera_act_s11_2', 9, 'state_check',
 'RERA registration number and authority on file', null, null,
 'block',
 'Section 3(1) prohibits advertising or marketing a real estate project that is not '
 'registered with the state RERA authority, and section 11(2) requires the registration '
 'number and the authority''s website address to appear prominently on every '
 'advertisement and prospectus. The penalty under section 59 reaches 10% of the '
 'estimated project cost. This is a legal boundary, not a copywriting problem - the '
 'same shape as Schedule J. '
 'needs_legal_verification: encoded from the public text of the Act; state authority '
 'rules differ and must be confirmed with counsel before commercial reliance.',
 'Record the project''s RERA registration number and the state authority''s website on '
 'the project record, and place both in the ad creative where they are legible.',
 'https://www.indiacode.nic.in/handle/123456789/2158',
 '2026-08-01', 'listed_industries',
 array['rera_registration_no', 'rera_authority_url']),

-- Stage 4 is outcome and timeline claims. For Ayurveda that is a promised cure;
-- for real estate it is a promised yield. Identical machinery, different words.
('IN_RERA_ASSURED_RETURN', 'in', 'rera_act_s3_s59', 4, 'term_list',
 'Assured or guaranteed return on a property', null,
 array[
   'assured return','assured returns','guaranteed return','guaranteed returns',
   'guaranteed rental','assured rental','guaranteed appreciation',
   'assured appreciation','fixed return','guaranteed roi','pakka return',
   'buyback guarantee','guaranteed buyback','assured buyback'
 ],
 'block',
 'An assured-return scheme on an under-construction property is a deposit-taking '
 'arrangement in substance. It draws RERA, SEBI collective-investment-scheme and '
 'Companies Act deposit rules simultaneously, and it is one of the most consistently '
 'penalised claims in Indian property advertising. '
 'needs_legal_verification: confirm with counsel before relying on this list.',
 'Remove the return promise entirely. State the project''s actual, disclosed '
 'commercial terms; a rental estimate must be labelled as an estimate with its basis.',
 'https://www.indiacode.nic.in/handle/123456789/2158',
 '2026-08-01', 'listed_industries', '{}')

on conflict (code) do nothing;

-- The scope rows. The constraint trigger on policy_rules is DEFERRABLE
-- INITIALLY DEFERRED precisely so these can follow the rules they scope inside
-- one transaction; without that, the insert above would fail at statement end
-- for naming no industry.
insert into t_advit.policy_rule_industries (rule_code, industry_key)
values
  ('IN_RERA_REGISTRATION_ON_FILE', 'real_estate'),
  ('IN_RERA_ASSURED_RETURN',       'real_estate')
on conflict do nothing;


-- ---------------------------------------------------------------------------
-- What a real estate workspace starts out believing
--
-- Copied into t_advit.account_context at onboarding at the confidence stated
-- here. Starting hypotheses, not facts - the OS tests them (PRD 6.2).
-- ---------------------------------------------------------------------------

insert into t_advit.industry_context_defaults
  (industry_key, dimension, key, value_json, confidence)
values
  ('real_estate', 'compliance', 'category_sensitivity',
   to_jsonb('high'::text), 0.900),

  ('real_estate', 'compliance', 'rera_exposure',
   to_jsonb(
     'Every creative naming a project must clear stage 9 with a verified RERA '
     'registration on file. An unregistered project cannot be advertised at all, and '
     'the registration number must appear in the ad itself.'::text
   ), 0.900),

  ('real_estate', 'sales_operation', 'primary_cta',
   to_jsonb('form'::text), 0.500),

  ('real_estate', 'sales_operation', 'cta_rationale',
   to_jsonb(
     'A site visit is the conversion event and the consideration cycle runs in weeks, '
     'so an instant form that captures budget and locality outperforms click-to-call. '
     'Starting hypothesis: the OS should test it against real cost per site visit.'::text
   ), 0.400)
on conflict (industry_key, dimension, key) do nothing;
