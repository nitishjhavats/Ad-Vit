-- =============================================================================
-- Fix: kg_edges_select authorised on the source node and never on the target
--
-- The whole USING clause was one EXISTS over t_advit.kg_nodes correlated by a
-- single expression: `where n.id = kg_edges.src_id`. `dst_id` was named by no
-- predicate anywhere in the policy, and the edge's own `tier` column was not
-- read either.
--
-- t_advit.kg_edges has no workspace_id of its own, and kg_nodes_tier_scoping
-- constrains NODES rather than an edge's two endpoints - so visibility of ONE
-- endpoint was being taken as authority over a row that names TWO. The guard was
-- not weak on the destination; there was no comparison on the destination at
-- all, so in that direction it permitted unconditionally.
--
-- What leaked is the EDGE ROW, not the far node's content - those are different
-- and only the first happened. Measured: Broadmate's member read two edges
-- terminating on a Rival Wellness account node, complete with effect_size,
-- confidence, evidence_n and the free-form props_json. The destination node's
-- own row stayed hidden behind kg_nodes_select (a LEFT JOIN to it returned
-- NULLs), so no node content and no workspace uuid crossed.
--
-- Two concrete exposures, both reproduced:
--
--   (a) Shared-tier source. ANY authenticated user - including an organisation
--       member with no workspace grant at all - could read every edge whose
--       source is an industry or global node, including its foreign
--       account-tier destinations. Those are precisely the provenance edges the
--       promotion pipeline writes ("industry pattern --observed_in--> the
--       account node that evidenced it"). A tenant could therefore enumerate,
--       count and track over time the foreign evidence nodes behind an industry
--       pattern, each with its effect size and confidence. That is the anonymity
--       industry_patterns.evidence_workspaces and the >=3-workspace independence
--       gate exist to hold. It hands over no workspace uuid; it hands over the
--       shape and size of the evidence set.
--
--   (b) Own-node source. An edge from the reader's OWN account node to another
--       tenant's account node was fully readable: the reader learns their node
--       is linked to a node they cannot see, with the edge type, the effect size
--       and the confidence.
--
-- kg_nodes and kg_edges are empty and nothing in the repository writes them, so
-- this is a live hole in a boundary that currently has nothing behind it. That
-- is the right time to fix it, not a reason to wait: the day the knowledge-graph
-- writer ships, this becomes a cross-tenant read.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- The policy. ALTER, not drop-and-create, so the OID survives.
--
-- Three conjuncts, and each closes a different hole:
--
--   1 and 2. Both endpoints must be visible. An edge is a row ABOUT TWO NODES,
--            so it is readable only when both are.
--
--   3.       The edge's own tier. Conjuncts 1 and 2 alone still leak an
--            ACCOUNT-tier edge drawn between two SHARED-tier nodes: both
--            endpoints are industry or global, both EXISTS arms are satisfied by
--            the shared tier, and the row goes to the entire customer base. That
--            is the house defect again - with no account endpoint and no
--            workspace_id on the row, the guard has nothing to compare against
--            and was permitting.
--
-- Every arm is a POSITIVE exists, never a NOT EXISTS, and that is the load-
-- bearing detail. The natural phrasing for a destination guard is "exclude the
-- ones I cannot see":
--
--     and not exists (select 1 from kg_nodes d
--                      where d.id = dst_id and d.tier = 'account'
--                        and not is_workspace_member(d.workspace_id))
--
-- It reads like a guard and denies nothing. kg_nodes RLS has ALREADY removed the
-- foreign node from that subquery, so it matches zero rows, NOT EXISTS is
-- vacuously true, and the edge is permitted. Applied to the live policy inside a
-- rolled-back transaction, that phrasing produced a visible set byte-identical
-- to the broken policy's. It is longer, it looks stricter, and it is a no-op.
--
-- core.is_superadmin() is hoisted OUT to the top level. It used to sit inside
-- the src EXISTS, where it was equivalent; with two subqueries it has to cover
-- both arms or the support console silently loses edges.
-- ---------------------------------------------------------------------------
alter policy kg_edges_select on t_advit.kg_edges
  using (
    core.is_superadmin()
    or (
      -- 1. the source is visible
      exists (
        select 1 from t_advit.kg_nodes s
         where s.id = kg_edges.src_id
           and (
             (s.tier = 'account' and t_advit.is_workspace_member(s.workspace_id))
             or s.tier in ('industry', 'global')
           )
      )
      -- 2. and so is the destination
      and exists (
        select 1 from t_advit.kg_nodes d
         where d.id = kg_edges.dst_id
           and (
             (d.tier = 'account' and t_advit.is_workspace_member(d.workspace_id))
             or d.tier in ('industry', 'global')
           )
      )
      -- 3. and an account-tier edge is somebody's. An edge claiming to be
      --    account-scoped with no account endpoint you belong to is not yours to
      --    read, however visible its two shared-tier endpoints happen to be.
      and (
        kg_edges.tier <> 'account'
        or exists (
          select 1 from t_advit.kg_nodes o
           where o.id in (kg_edges.src_id, kg_edges.dst_id)
             and o.tier = 'account'
             and t_advit.is_workspace_member(o.workspace_id)
        )
      )
    )
  );


-- ---------------------------------------------------------------------------
-- And the constraint kg_edges never had.
--
-- kg_nodes has carried kg_nodes_tier_scoping all along: an account node must
-- name a workspace, a shared node must not. kg_edges has a `tier` column with
-- nothing constraining it and, until the policy above, nothing reading it.
-- Verified: inserting an ownerless account NODE is refused by that check, while
-- inserting an ownerless account EDGE succeeded.
--
-- A CHECK cannot express this - it spans three rows - so it is a trigger, in the
-- same shape as core.guard_entitlement_value.
--
-- This is not belt-and-braces for its own sake. Conjunct 3 makes such an edge
-- invisible to every tenant, which means it is a row nobody can read and nobody
-- should be able to write; leaving it writable would accumulate rows whose only
-- effect is to make a future reader wonder why the graph has holes in it.
-- ---------------------------------------------------------------------------
create or replace function t_advit.guard_kg_edge_tier()
returns trigger
language plpgsql
security definer
set search_path = t_advit, core, pg_catalog
as $fn$
begin
  if new.tier = 'account' and not exists (
    select 1 from t_advit.kg_nodes n
     where n.id in (new.src_id, new.dst_id)
       and n.tier = 'account'
  ) then
    raise exception
      'an account-tier edge must touch an account-tier node; % -> % touches none',
      new.src_id, new.dst_id
      using errcode = '23514', hint = 'edge_tier_unscoped';
  end if;

  return new;
end;
$fn$;

comment on function t_advit.guard_kg_edge_tier() is
  'An account-tier edge has to belong to an account. Without this, an edge drawn '
  'between two shared-tier nodes could claim account scope while naming no '
  'workspace - which the SELECT policy now refuses to show anyone, so it would '
  'be a row that exists and can never be read.';

drop trigger if exists kg_edges_tier_guard on t_advit.kg_edges;
create trigger kg_edges_tier_guard
  before insert or update on t_advit.kg_edges
  for each row execute function t_advit.guard_kg_edge_tier();

revoke execute on function t_advit.guard_kg_edge_tier() from public;
