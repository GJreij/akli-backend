import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY")!;
const ADMIN_EMAIL = Deno.env.get("ADMIN_EMAIL")!;
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET"); // optional but recommended
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

const GOAL_LABELS: Record<string, string> = {
  lose: "Lose weight",
  maintain: "Maintain weight",
  build: "Build muscle",
  health: "General health",
};

const DIET_LABELS: Record<string, string> = {
  "high-protein": "High protein",
  balanced: "Balanced",
  "low-carb": "Low carb",
  "low-fat": "Low fat",
};

const ACTIVITY_LABELS: Record<string, string> = {
  "1.2": "Sedentary",
  "1.375": "Lightly active",
  "1.55": "Moderately active",
  "1.725": "Very active",
};

function pickTrueFlag(record: any) {
  if (record?.self_built_diet) return "self_built_diet";
  if (record?.non_akli_partner) return "non_akli_partner";
  if (record?.akli_partner) return "akli_partner";
  return "none";
}

async function sendEmail(subject: string, html: string) {
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${RESEND_API_KEY}`,
    },
    body: JSON.stringify({
      from: "Akli <onboarding@resend.dev>",
      to: [ADMIN_EMAIL],
      subject,
      html,
    }),
  });

  const text = await res.text();
  console.log("Resend response status:", res.status);
  console.log("Resend response body:", text);

  if (!res.ok) {
    throw new Error(`Resend error ${res.status}: ${text}`);
  }
}

// Pull the diet/goal info the client submitted during onboarding — lives on
// daily_macro_target now (goal, sex, height_cm, weight_kg, activity_level,
// method, diet_type, kcal_target), not on the user row itself.
async function fetchLatestDiet(userId: string) {
  const { data } = await supabase
    .from("daily_macro_target")
    .select("kcal_target, protein_g, carbs_g, fat_g, diet_type, goal, sex, height_cm, weight_kg, activity_level, method")
    .eq("user_id", userId)
    .order("created_at", { ascending: false })
    .limit(1)
    .single();
  return data;
}

function dietSectionHtml(diet: Record<string, any> | null) {
  if (!diet) {
    return `<h3>🎯 Diet & goals</h3><p>Not set yet.</p>`;
  }
  const activityLabel = diet.activity_level != null
    ? (ACTIVITY_LABELS[String(diet.activity_level)] ?? diet.activity_level)
    : null;

  return `
    <h3>🎯 Diet & goals</h3>
    <ul>
      <li><b>Goal:</b> ${diet.goal ? (GOAL_LABELS[diet.goal] ?? diet.goal) : "—"}</li>
      <li><b>Method:</b> ${diet.method === "guided" ? "Guided (calculated from stats)" : diet.method === "manual" ? "Manual (entered directly)" : "—"}</li>
      ${diet.method === "guided" ? `
      <li><b>Sex:</b> ${diet.sex ?? "—"}</li>
      <li><b>Height:</b> ${diet.height_cm != null ? `${diet.height_cm} cm` : "—"}</li>
      <li><b>Weight:</b> ${diet.weight_kg != null ? `${diet.weight_kg} kg` : "—"}</li>
      <li><b>Activity level:</b> ${activityLabel ?? "—"}</li>
      ` : ""}
      <li><b>Diet style:</b> ${diet.diet_type ? (DIET_LABELS[diet.diet_type] ?? diet.diet_type) : "—"}</li>
      <li><b>Daily target:</b> ${diet.kcal_target != null ? `${Math.round(diet.kcal_target)} kcal` : "—"}
        (P ${diet.protein_g != null ? Math.round(diet.protein_g) : "—"}g ·
         C ${diet.carbs_g != null ? Math.round(diet.carbs_g) : "—"}g ·
         F ${diet.fat_g != null ? Math.round(diet.fat_g) : "—"}g)</li>
    </ul>
  `;
}

serve(async (req) => {
  console.log("WEBHOOK HIT");

  const gotSecret = req.headers.get("x-webhook-secret");
  console.log("headers x-webhook-secret:", gotSecret);

  if (WEBHOOK_SECRET && gotSecret !== WEBHOOK_SECRET) {
    console.log("Webhook secret mismatch");
    return new Response("Unauthorized", { status: 401 });
  }

  const payload = await req.json();
  console.log("headers x-webhook-secret:", req.headers.get("x-webhook-secret"));

  console.log("payload.type:", payload?.type);
  console.log("payload.schema:", payload?.schema);
  console.log("payload.table:", payload?.table);

  console.log("record.onboarding:", payload?.record?.onboarding);
  console.log("old_record.onboarding:", payload?.old_record?.onboarding);

  // Supabase Database Webhooks payload usually contains:
  // { type: 'INSERT'|'UPDATE'|'DELETE', schema, table, record, old_record }
  const type = payload?.type;
  const schema = payload?.schema;
  const table = payload?.table;

  if (schema !== "public" || table !== "user") {
    return new Response("ignored", { status: 200 });
  }

  const record = payload?.record;
  const oldRecord = payload?.old_record;

  // 1) New user inserted — this fires the moment someone finishes onboarding
  // and creates their account, so include what they just told us.
  if (type === "INSERT") {
    const diet = await fetchLatestDiet(record?.id);

    const subject = `New user signup: ${record?.email ?? record?.id ?? ""}`;
    const html = `
      <h3>New user signed up</h3>
      <ul>
        <li><b>Name:</b> ${record?.name ?? ""} ${record?.last_name ?? ""}</li>
        <li><b>Email:</b> ${record?.email ?? ""}</li>
        <li><b>Phone:</b> ${record?.phone_number ?? ""}</li>
        <li><b>Created at:</b> ${record?.created_at ?? ""}</li>
      </ul>
      ${dietSectionHtml(diet)}
    `;
    console.log("About to send email, subject:", subject);

    await sendEmail(subject, html);
    return new Response("ok", { status: 200 });
  }

  // 2) onboarding changed to true (false/null -> true)
  if (type === "UPDATE") {
    const newOnboarding = record?.onboarding === true;
    const oldOnboarding = oldRecord?.onboarding === true;

    if (newOnboarding && !oldOnboarding) {
      const mode = pickTrueFlag(record);
      const diet = await fetchLatestDiet(record?.id);

      const subject =
        `Onboarding completed: ${(record?.name ?? "")} ${(record?.last_name ?? "")}`.trim();

      const html = `
        <h3>Client completed onboarding</h3>
        <ul>
          <li><b>Name:</b> ${record?.name ?? ""} ${record?.last_name ?? ""}</li>
          <li><b>Phone:</b> ${record?.phone_number ?? ""}</li>
          <li><b>Email:</b> ${record?.email ?? ""}</li>
          <li><b>Mode:</b> ${mode}</li>
        </ul>
        ${dietSectionHtml(diet)}
      `;
      console.log("About to send email, subject:", subject);

      await sendEmail(subject, html);
    }

    return new Response("ok", { status: 200 });
  }

  return new Response("ignored", { status: 200 });
});
