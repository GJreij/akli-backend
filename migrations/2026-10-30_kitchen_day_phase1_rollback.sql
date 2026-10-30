-- Reverts 2026-10-30_kitchen_day_phase1.sql. Drops logged batches.
alter table public.meal_plan_day_recipe_serving drop column if exists batch_id;
drop table if exists public.kitchen_batch;

alter table public.ingredient
  drop column if exists is_staple,
  drop column if exists stored_frozen,
  drop column if exists thaw_hours;

alter table public.subrecipe
  drop column if exists prep_type,
  drop column if exists fridge_days,
  drop column if exists freezer_days,
  drop column if exists equipment,
  drop column if exists active_minutes,
  drop column if exists total_minutes,
  drop column if exists skill,
  drop column if exists prep_ahead;
