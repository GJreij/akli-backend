-- Kitchen (simple): the plan for one cook date as plain lines — per
-- subrecipe what I have, what I cook and who does it — plus hand-written
-- steps and the saved weights of pots/trays for portioning. Purely additive:
-- the old Kitchen Day tables (kitchen_day_plan, kitchen_batch) are untouched.
-- Rollback:
--   drop table if exists public.kitchen_plan_item;
--   drop table if exists public.kitchen_plan_step;
--   drop table if exists public.kitchen_equipment;

create table if not exists public.kitchen_plan_item (
  tenant_id bigint not null default 1 references public.tenant(id),
  cook_date date not null,
  subrecipe_id bigint not null references public.subrecipe(id),
  -- Servings delivered that day, frozen at save (next day's "have" suggestion).
  need numeric not null default 0,
  -- Servings already on hand before cooking.
  have numeric not null default 0,
  -- Servings planned to cook.
  cook numeric not null default 0,
  -- "georges" | "guitta"
  assignee text not null default 'georges',
  -- Servings actually cooked, when it differs from the plan.
  cooked numeric,
  done_at timestamptz,
  portioned_at timestamptz,
  updated_at timestamptz not null default now(),
  primary key (tenant_id, cook_date, subrecipe_id)
);

create index if not exists kitchen_plan_item_sub_date on public.kitchen_plan_item (tenant_id, subrecipe_id, cook_date desc);

create table if not exists public.kitchen_plan_step (
  id bigserial primary key,
  tenant_id bigint not null default 1 references public.tenant(id),
  cook_date date not null,
  text text not null,
  assignee text not null default 'georges',
  done_at timestamptz,
  sort integer not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists kitchen_plan_step_date on public.kitchen_plan_step (tenant_id, cook_date);

create table if not exists public.kitchen_equipment (
  id bigserial primary key,
  tenant_id bigint not null default 1 references public.tenant(id),
  name text not null,
  weight_g numeric not null check (weight_g >= 0),
  created_at timestamptz not null default now()
);

alter table public.kitchen_plan_item enable row level security;
alter table public.kitchen_plan_step enable row level security;
alter table public.kitchen_equipment enable row level security;

create policy kitchen_plan_item_admin on public.kitchen_plan_item
  for all to authenticated using (is_admin()) with check (is_admin());
create policy kitchen_plan_step_admin on public.kitchen_plan_step
  for all to authenticated using (is_admin()) with check (is_admin());
create policy kitchen_equipment_admin on public.kitchen_equipment
  for all to authenticated using (is_admin()) with check (is_admin());
