-- Per-dish availability inside a weekly menu: the exact dates a dish can be
-- eaten (e.g. Mon 7 and Thu 10 Dec). NULL or empty = every day of that
-- menu. Used by the planner, meal swaps and day edits so slow sellers are
-- cooked on 1-2 days only. Additive only: existing rows keep NULL.
-- Rollback:
--   alter table public.weekly_menu_recipe drop column if exists available_dates;

alter table public.weekly_menu_recipe
  add column if not exists available_dates date[];

comment on column public.weekly_menu_recipe.available_dates is
  'Dates this dish is available within its weekly menu; NULL/empty = every day.';
