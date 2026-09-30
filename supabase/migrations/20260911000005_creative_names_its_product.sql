-- =============================================================================
-- Somewhere for a creative to say which product it advertises
--
-- IN_AYUSH_LICENCE_ON_FILE (stage 9, severity BLOCK) asks whether AYUSH
-- licensing is in place FOR THE PRODUCT BEING ADVERTISED, and whether that
-- product's legal classification is consistent with what the copy claims. Both
-- halves are per-product. A workspace holds many products; a creative had no
-- way to name one; so the runtime took products[0] and let row order decide
-- whether a licensed SKU vouched for an unlicensed one.
--
-- The runtime fix (app/orchestrator/graph.py, _licence_posture) refuses instead
-- of guessing: a creative that names a SKU is checked against that SKU, a
-- catalogue holding exactly one product is unambiguous, and anything else is
-- reported as an UNEVALUATED stage 9 rather than a pass. It deliberately does
-- not infer the product from ad copy - a wrong guess there errs permissively
-- and silently.
--
-- That leaves the remedy the gate prints - "name the product on the creative" -
-- with nowhere to be stored for any workspace holding more than one SKU. This
-- column is that place. The chat path already carries the reference on the
-- request (creative.product_sku); this is where the creative upload flow will
-- record it, so a stored creative is re-checkable later without the original
-- request.
--
-- Nothing writes it yet. Stated plainly rather than implied by its existence.
-- =============================================================================

alter table t_advit.creatives
  add column if not exists product_id uuid
    references t_advit.catalog_products(id) on delete set null;

create index if not exists creatives_product_idx
  on t_advit.creatives (product_id)
  where product_id is not null;

comment on column t_advit.creatives.product_id is
  'The catalogue product this creative advertises, which is what decides the '
  'AYUSH licence posture the stage-9 check reads. NULL is a gap, not a default: '
  'where the workspace holds more than one product the gate reports stage 9 '
  'unevaluated rather than picking a SKU. Nothing populates this yet - the '
  'creative upload flow is where it belongs.';

-- A creative and the product it advertises must belong to the same workspace.
-- Not expressible as a CHECK - CHECK constraints cannot contain subqueries -
-- and the two foreign keys above are individually satisfiable by rows from
-- different tenants, so the join has to be constrained explicitly.
alter table t_advit.catalog_products
  drop constraint if exists catalog_products_workspace_id_id_key;
alter table t_advit.catalog_products
  add constraint catalog_products_workspace_id_id_key unique (workspace_id, id);

alter table t_advit.creatives
  drop constraint if exists creatives_product_same_workspace;
alter table t_advit.creatives
  add constraint creatives_product_same_workspace
  foreign key (workspace_id, product_id)
  references t_advit.catalog_products (workspace_id, id)
  on delete set null;

comment on constraint creatives_product_same_workspace on t_advit.creatives is
  'A creative may only name a product from its own workspace. Without this, two '
  'valid foreign keys still permit a creative in workspace A to borrow a '
  'licensed product from workspace B - which is a cross-tenant licence claim.';


-- ---------------------------------------------------------------------------
-- RLS for the touched tables, restated in the migration that changes them.
-- ---------------------------------------------------------------------------

alter table t_advit.creatives        enable row level security;
alter table t_advit.catalog_products enable row level security;

drop policy if exists creatives_select on t_advit.creatives;
create policy creatives_select on t_advit.creatives
  for select to authenticated
  using (t_advit.is_workspace_member(workspace_id) or core.is_superadmin());

drop policy if exists creatives_write on t_advit.creatives;
create policy creatives_write on t_advit.creatives
  for all to authenticated
  using (t_advit.is_workspace_member(workspace_id))
  with check (t_advit.is_workspace_member(workspace_id));

drop policy if exists catalog_products_select on t_advit.catalog_products;
create policy catalog_products_select on t_advit.catalog_products
  for select to authenticated
  using (t_advit.is_workspace_member(workspace_id) or core.is_superadmin());

-- Deliberately left as-is: a workspace member may still record their own
-- licence number. The fix is not that the tenant cannot write it - they are the
-- only party who has it - but that the GATE reads the stored row rather than a
-- string travelling on the request that asks for the verdict. Writing a licence
-- number is a durable, audited claim about the business; passing one in a
-- request body was a per-call override of a BLOCK rule.
drop policy if exists catalog_products_write on t_advit.catalog_products;
create policy catalog_products_write on t_advit.catalog_products
  for all to authenticated
  using (t_advit.is_workspace_member(workspace_id))
  with check (t_advit.is_workspace_member(workspace_id));


-- ---------------------------------------------------------------------------
-- The remedy the gate prints has to name the step that makes it resolvable.
-- "Record the licence on the product record" is not enough for a workspace
-- holding four products, because the gate still cannot tell which of them the
-- creative is for.
-- ---------------------------------------------------------------------------

update t_advit.policy_rules
   set remedy_template = remedy_template ||
       ' Where the workspace holds more than one product, name which one the creative '
       'is for - the gate checks the licence of the product being advertised and will '
       'not infer it from the copy.'
 where code = 'IN_AYUSH_LICENCE_ON_FILE'
   and remedy_template not like '%name which one the creative%';


grant select, insert, update, delete on t_advit.creatives        to authenticated;
grant select, insert, update, delete on t_advit.catalog_products to authenticated;

grant all on all tables    in schema t_advit to service_role;
grant all on all sequences in schema t_advit to service_role;
grant all on all functions in schema t_advit to service_role;
