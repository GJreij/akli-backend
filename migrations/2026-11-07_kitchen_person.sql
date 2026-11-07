-- Kitchen (simple): the people who can be given tasks. kitchen_plan_item /
-- kitchen_plan_step.assignee hold kitchen_person.id. Removing someone only
-- hides them (active = false), so old days still show their name.
-- Rollback:
--   drop table if exists public.kitchen_person;

create table if not exists public.kitchen_person (
  tenant_id bigint not null default 1 references public.tenant(id),
  id text not null check (id ~ '^[a-z0-9-]{1,40}$'),
  name text not null,
  sort integer not null default 0,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  primary key (tenant_id, id)
);

alter table public.kitchen_person enable row level security;
create policy kitchen_person_admin on public.kitchen_person
  for all to authenticated using (is_admin()) with check (is_admin());

insert into public.kitchen_person (tenant_id, id, name, sort) values
  (1, 'georges', 'Georges', 0),
  (1, 'guitta', 'Guitta', 1)
on conflict do nothing;
