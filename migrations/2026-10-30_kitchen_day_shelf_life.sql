-- Applied 2026-10-30 (approved as drafted).
-- Sets prep_type / fridge_days / freezer_days on every subrecipe, and syncs the
-- existing `freezable` flag to (freezer_days is not null).
--
-- fridge_days = days a cooked batch stays good in the fridge, counting the cook
-- day as day 0 (2 = cook Sunday, last served Tuesday). Kept conservative: rice 2,
-- cooked meat/chicken/stews 3, baked goods 4, sauces and dressings 3–5, cut
-- salads 1, eggs and fish same day / 1.
-- freezer_days = how long it keeps frozen, ALREADY SPLIT INTO PORTIONS. NULL = don't freeze.

-- ---------------------------------------------------------------
-- READY: bought, just portioned. Never on the cook list.
-- ---------------------------------------------------------------
update subrecipe set prep_type='ready', fridge_days=null, freezer_days=null where id in (
  26,  -- Akli Blossom Honey
  34,  -- Pain Demi
  29,  -- Pita Bread
  58,  -- Mixed Nuts
  24,  -- Greek Yogurt (Plain)
  77,  -- Seasonal Fruit
  32,  -- Turkey
  116, -- Granola
  33,  -- Kashkaval Cheese
  83,  -- Yogurt (Taanayel)
  45,  -- Hummus (Durra + lemon)
  54,  -- Pickles
  37,  -- Kiwi
  119, -- Tortilla Wrap
  91,  -- Ariche
  94,  -- Peanut Butter
  25,  -- Banana
  57,  -- Biscotte Toast
  36,  -- Blueberry
  114, -- The Sportitian Cupcakes
  173, -- Dibs w Tahini
  107, -- Popcorn
  88   -- Cherry Tomato
);

-- ---------------------------------------------------------------
-- ASSEMBLE: no heat (salads, dressings, spreads). Helper-friendly.
-- ---------------------------------------------------------------
update subrecipe set prep_type='assemble', skill='helper', fridge_days=1, freezer_days=null where id in (
  44, 160, 158, 161, 159,  -- House Salad, Light Tabbouleh, Light Fattoush, Carrot Slaw, Malfouf
  31,  -- Vegetable Platter
  86,  -- Salade De Fruit
  92,  -- Ariche topping (avocado)
  141, -- Salmon Poke Mix (raw salmon → same/next day only)
  143  -- Parsley Tahini Tuna
);
update subrecipe set prep_type='assemble', skill='helper', fridge_days=3, freezer_days=null where id in (
  67,  -- Chicken Mango Salad Dressing
  133, -- Akli Burger Sauce
  118, -- Avocado Garlic Sauce (avocado browns; 3 max)
  166, -- Light Toum
  30,  -- Labne
  87,  -- Za'atar Labneh Filling
  123, -- Yogurt Herbs Dressing
  64,  -- Caesar Dressing
  164, -- Club Sandwich Spread & Veg
  167, -- Green Tahini
  48,  -- Tarator
  171, -- Spicy Yogurt Mayo
  165, -- Labneh Mint Spread
  170, -- Khiyar bi Laban
  90,  -- Chia Pudding
  93,  -- Overnight Oats Base
  120, -- Date & Peanut Butter
  85,  -- Halawa Date Mixed Nuts
  140  -- Cashew Chocolate Dip
);
update subrecipe set prep_type='assemble', skill='helper', fridge_days=5, freezer_days=30 where id in (
  168, -- Muhammara
  169  -- Shatta
);

-- ---------------------------------------------------------------
-- COOK + FREEZABLE: the big batch-cooking wins.
-- ---------------------------------------------------------------
-- Rice: 2 days in the fridge max (cool fast, under 1h), freezes well
update subrecipe set prep_type='cook', fridge_days=2, freezer_days=30 where id in (
  68,  -- Rice Broth
  162, -- Vermicelli Rice
  134, -- Riz aa Djej Rice
  163  -- Siyadiye Rice
);
-- Cooked meat / chicken / stews / sauces
update subrecipe set prep_type='cook', fridge_days=3, freezer_days=30 where id in (
  60,  -- Grilled Chicken Breast
  125, -- Chicken For Pasta
  135, -- Riz aa Djej Chicken
  136, -- Riz aa Djej Sauce (cornstarch: whisk after thawing)
  80,  -- Lasagne
  99,  -- Burger Meat
  43,  -- Grilled Kafta
  82,  -- Kebbe Bel Sayniye
  46,  -- Tawouk
  104, -- Chicken Fajita Filling
  72,  -- Chicken Stir Fry Base
  79,  -- Spinach & Beef Rice Bowl Base
  84,  -- Beef & Eggplant Stew
  95,  -- Kafta with Potatoes in Tomato Sauce
  49,  -- Shawarma Meat
  122, -- Shawarma Chicken
  97,  -- Lebanese Chicken & Boulgour
  47,  -- Mjadara
  142, -- Beef Stroganoff (labneh sauce can split; stir well)
  110, -- Chili Con Carne
  126, -- Meat for Pasta
  101, -- Pesto Meat
  61,  -- Quinoa & Vegetables
  152, -- Honey Garlic Chicken
  74,  -- Habbet Remmene
  151, -- Beef & Broccoli
  98,  -- Oven-Grilled Meat
  96,  -- Chicken Fasolya
  102, -- Grilled Chicken & Vegetables
  109, -- Sweet Potato & Veg
  81,  -- Homestyle Beef Pasta (tomato; pasta goes a bit softer)
  113, -- Soufflé
  112, -- Mashed Potatoes
  65   -- Caesar Chicken
);
-- Baked goods / batters
update subrecipe set prep_type='cook', fridge_days=4, freezer_days=45 where id in (
  78,  -- Peanut Butter Cookie
  138, -- Salty Turkey Muffin
  137, -- Tahini Fudge Brownies
  55,  -- Banana Oat Cake
  147, -- Apple Cinnamon Baked Oats
  155, -- Light Sfouf
  157  -- Date Snickers Slice
);
update subrecipe set prep_type='cook', fridge_days=2, freezer_days=30 where id = 115; -- Oat Pancake Batter
update subrecipe set prep_type='cook', fridge_days=7, freezer_days=null where id in (172, 174); -- chocolate syrup, PB drizzle

-- ---------------------------------------------------------------
-- COOK, FRIDGE ONLY: texture dies in the freezer
-- ---------------------------------------------------------------
update subrecipe set prep_type='cook', fridge_days=2, freezer_days=null where id in (
  132, -- Grilled Potatoes
  100, -- Grilled Vegetables & Potatoes
  129, -- Roasted Potato & Vegetables
  150, -- Batata Harra
  105, -- Sweet Potato & Broccoli
  42,  -- Oven Chicken Potatoes
  53,  -- Chicken Pesto Pasta (cream sauce splits)
  103, -- Chicken Fajita Pasta (cream)
  52,  -- Chicken Fredo (cream)
  106, -- Creamy Shrimp Pasta
  66,  -- Chicken Mango Salad Base (pasta + fresh mango)
  153, -- Veggie Stir-Fry Noodles
  111, -- Steak
  117, -- Pesto Halloumi Turkey Filling
  89,  -- Grilled Halloumi & Turkey Filling
  63   -- Tawouk Sandwich Filling (has lettuce)
);
update subrecipe set prep_type='cook', fridge_days=3, freezer_days=null where id in (
  131, -- Grilled Vegetables
  156  -- Light Mouhallabiye
);
-- Eggs / fish / seafood: same day or next day
update subrecipe set prep_type='cook', fridge_days=1, freezer_days=null where id in (
  75,  -- Siyadiye Fish
  130, -- Grilled Salmon
  127, -- Shrimp for Pasta
  108, -- Shrimp Mango Poke Base
  149, -- Breakfast Burrito Filling
  139  -- Turkey Egg Baguette
);
update subrecipe set prep_type='cook', fridge_days=0, freezer_days=null where id in (
  59,  -- Avocado & Egg Toast
  144, -- Omelette
  56,  -- Feta Omelette
  121, -- Spinach Feta Omelette
  28,  -- Scrambled Eggs
  146  -- Beid w Labneh
);

-- Keep the old flag in sync for the catalog screens
update subrecipe set freezable = (freezer_days is not null);

-- ---------------------------------------------------------------
-- Corrections (2026-10-30, from the kitchen): these are made, not just portioned
-- ---------------------------------------------------------------
update subrecipe set prep_type='assemble', skill='helper', fridge_days=2, freezer_days=null where id in (32, 33); -- Turkey, Kashkaval Cheese (sliced for sandwiches)
update subrecipe set prep_type='assemble', skill='helper', fridge_days=7, freezer_days=null where id = 26;     -- Akli Blossom Honey (mixed)
update subrecipe set prep_type='cook', fridge_days=1, freezer_days=null, equipment='stove' where id = 107;     -- Popcorn
update subrecipe set freezable = (freezer_days is not null) where id in (26, 32, 33, 107);
