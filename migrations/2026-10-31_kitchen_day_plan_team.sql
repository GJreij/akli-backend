-- Kitchen Day: who's in the kitchen (names + hours) and who does each task.
-- team:   [{id, name, from "HH:MM", to "HH:MM"}]
-- assign: {task_key: person_id}
alter table public.kitchen_day_plan
  add column if not exists team jsonb not null default '[]'::jsonb,
  add column if not exists assign jsonb not null default '{}'::jsonb;
