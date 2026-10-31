-- Kitchen Day: a fourth kitchen type, "slice" (cut and portion one
-- ingredient: cold cuts, cheese, fruit, veg), next to cook / assemble (make,
-- no heat) / ready (bought, just portion).
alter table public.subrecipe drop constraint subrecipe_prep_type_check;
alter table public.subrecipe add constraint subrecipe_prep_type_check
  check (prep_type = any (array['cook'::text, 'assemble'::text, 'slice'::text, 'ready'::text]));
-- Turkey, Kashkaval, Pickles, Seasonal Fruit, Vegetable Platter.
update public.subrecipe set prep_type = 'slice' where id in (32, 33, 54, 77, 31) and prep_type = 'assemble';
