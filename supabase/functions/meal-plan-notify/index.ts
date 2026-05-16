import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY_MEALPLAN")!;
const ADMIN_EMAIL = Deno.env.get("ADMIN_EMAIL")!;
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET");
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

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
  console.log("Resend status:", res.status);
  console.log("Resend body:", text);

  if (!res.ok) throw new Error(`Resend error ${res.status}: ${text}`);
}

serve(async (req) => {
  // 1) Verify webhook secret
  const gotSecret = req.headers.get("x-webhook-secret");
  if (WEBHOOK_SECRET && gotSecret !== WEBHOOK_SECRET) {
    return new Response("Unauthorized", { status: 401 });
  }

  // 2) Parse payload
  const payload = await req.json();
  const { type, schema, table, record } = payload;

  if (schema !== "public" || table !== "meal_plan" || type !== "INSERT") {
    return new Response("ignored", { status: 200 });
  }

  // 3) Fetch user details from the user table
  const { data: user, error } = await supabase
    .from("user")
    .select("name, last_name, email, phone_number, delivery_address")
    .eq("id", record?.user_id)
    .single();

  if (error) {
    console.error("Failed to fetch user:", error.message);
  }

  const fullName = user
    ? `${user.name ?? ""} ${user.last_name ?? ""}`.trim()
    : "Unknown";

  // 4) Build and send email
  const subject = `🥗 New Akli Order — ${fullName}`;
  const html = `
    <h2>New meal plan order</h2>

    <h3>👤 Client</h3>
    <ul>
      <li><b>Name:</b> ${fullName}</li>
      <li><b>Email:</b> ${user?.email ?? "—"}</li>
      <li><b>Phone:</b> ${user?.phone_number ?? "—"}</li>
    </ul>

    <h3>📦 Delivery</h3>
    <ul>
      <li><b>Address:</b> ${user?.delivery_address ?? "—"}</li>
    </ul>

    <h3>📋 Plan Details</h3>
    <ul>
      <li><b>Plan ID:</b> ${record?.id ?? "—"}</li>
      <li><b>Start date:</b> ${record?.start_date ?? "—"}</li>
      <li><b>End date:</b> ${record?.end_date ?? "—"}</li>
      <li><b>Created at:</b> ${record?.created_at ?? "—"}</li>
    </ul>
  `;

  await sendEmail(subject, html);
  return new Response("ok", { status: 200 });
});