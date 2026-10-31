-- Kitchen Day v2: the plan for one cook date, saved so the cook, the helper
-- and the kitchen assistant all see the same decisions. Purely additive.
-- Rollback:
--   drop table if exists public.kitchen_day_plan;

create table if not exists public.kitchen_day_plan (
  tenant_id bigint not null default 1 references public.tenant(id),
  cook_date date not null,
  -- { "<subrecipe_id>": { "plan": "must"|"suggested"|"freeze"|"custom", "servings": n,
  --   "fridge": n, "freezer": n } } — servings frozen at confirm time.
  choices jsonb not null default '{}',
  confirmed_at timestamptz,
  confirmed_by uuid,
  -- Prep tasks that aren't a batch (separate pots): { "<task key>": { "at": iso, "servings": n } }.
  done jsonb not null default '{}',
  -- The assistant's timed order of work, once applied: [{ time, who, station, task }].
  schedule jsonb,
  -- WhatsApp note for the next day, written with the assistant.
  note text,
  note_chat jsonb not null default '[]',
  note_final boolean not null default false,
  updated_at timestamptz not null default now(),
  primary key (tenant_id, cook_date)
);

alter table public.kitchen_day_plan enable row level security;

create policy kitchen_day_plan_admin on public.kitchen_day_plan
  for all to authenticated using (is_admin()) with check (is_admin());
