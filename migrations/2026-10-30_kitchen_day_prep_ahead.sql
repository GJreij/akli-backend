-- Kitchen Day phase 2: a day-before job per subrecipe ("Marinate overnight").
-- Rollback: alter table public.subrecipe drop column if exists prep_ahead;
alter table public.subrecipe add column if not exists prep_ahead text;
comment on column public.subrecipe.prep_ahead is 'Job to do the day before cooking (e.g. "Marinate overnight", "Soak chickpeas"). Shown on Kitchen Day > Tomorrow.';
