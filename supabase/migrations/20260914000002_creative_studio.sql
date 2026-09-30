-- =============================================================================
-- Creative studio: somewhere to put a video, and a record of what was made of it
--
-- t_advit.creatives has carried the analytical columns since the first schema -
-- tags, pillars, an embedding slot, a compliance verdict, a fatigue score - and
-- nothing has ever written a row. There was no bucket to upload into, no
-- lifecycle column to say where an upload was in its life, and no place for the
-- rating the customer is paying for.
--
-- This gives the table its lifecycle and its rating, and gives the platform a
-- private bucket whose objects a tenant can reach only under their own
-- workspace's prefix.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Lifecycle and rating
-- ---------------------------------------------------------------------------
create type t_advit.creative_status as enum ('uploaded', 'analysing', 'analysed', 'failed');

alter table t_advit.creatives
  add column status         t_advit.creative_status not null default 'uploaded',
  add column uploaded_by    uuid references core.platform_users(id),
  add column original_name  text,
  add column size_bytes     bigint,
  add column analysed_at    timestamptz,
  add column analysis_error text,
  -- The rating, as the model returned it plus what was computed around it.
  -- One JSON column rather than a column per criterion, because the rubric is
  -- data (app/creative/rubric.py) and will change; a column per criterion would
  -- put the rubric in the schema too.
  add column rating_json    jsonb;

comment on column t_advit.creatives.rating_json is
  'The structured rating. Present only when status = analysed. Carries the '
  'rubric version it was scored against, so an old rating is comparable to '
  'itself and not silently to a newer rubric.';

alter table t_advit.creatives
  add constraint creatives_analysed_has_rating check (
    -- An analysed creative carries a rating; an unanalysed one does not claim to.
    (status = 'analysed') = (rating_json is not null)
  ),
  add constraint creatives_failed_says_why check (
    status <> 'failed' or analysis_error is not null
  ),
  add constraint creatives_size_positive check (size_bytes is null or size_bytes > 0);

-- The write policy is `creatives_write` for all, on is_workspace_member. The
-- rating and the verdict are the SYSTEM's judgement, though, and a tenant that
-- could set its own compliance_verdict could clear a BLOCK by editing a row.
-- Column-level: a tenant may update what they uploaded, not what was concluded.
revoke update on t_advit.creatives from authenticated;
grant update (original_name, tags_json, product_id, ai_generated)
   on t_advit.creatives to authenticated;


-- ---------------------------------------------------------------------------
-- 2. The bucket, and who can reach what in it
--
-- Objects live at <workspace_id>/<creative_id>.<ext>. The first path segment is
-- the tenancy key, and every policy below reads it with split_part.
--
-- Private, and the size limit is the Meta limit for a video ad rather than a
-- number picked for the database: an upload the platform would never be able to
-- send to Meta should be refused at the door.
-- ---------------------------------------------------------------------------
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'creatives', 'creatives', false,
  4294967296,   -- 4 GB, Meta's ceiling for a video ad
  array['video/mp4', 'video/quicktime', 'video/webm', 'image/jpeg', 'image/png', 'image/webp']
)
on conflict (id) do nothing;

-- Each policy is scoped to THIS bucket. storage.objects is shared with every
-- other product on the cluster, and a policy without the bucket predicate would
-- decide for all of them.

create policy creatives_read_own_workspace on storage.objects
  for select to authenticated
  using (
    bucket_id = 'creatives'
    and t_advit.is_workspace_member(split_part(name, '/', 1)::uuid)
  );

create policy creatives_upload_own_workspace on storage.objects
  for insert to authenticated
  with check (
    bucket_id = 'creatives'
    and t_advit.is_workspace_member(split_part(name, '/', 1)::uuid)
    -- The uploader is recorded by Storage as owner; a tenant may not claim to
    -- be somebody else at upload.
    and owner = auth.uid()
  );

-- No UPDATE and no DELETE for tenants. A creative that has been rated and, in
-- time, run as an ad is part of the account's history; replacing the bytes
-- under an existing rating would make the rating a lie. Replacing a creative
-- is uploading a new one.

comment on policy creatives_read_own_workspace on storage.objects is
  'The first path segment is the workspace id. is_workspace_member is the '
  'product''s boundary everywhere else, and it is the boundary here.';
