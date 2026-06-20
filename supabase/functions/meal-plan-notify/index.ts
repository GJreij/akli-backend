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
    .select("name, last_name, email, phone_number")
    .eq("id", record?.user_id)
    .single();

  if (error) {
    console.error("Failed to fetch user:", error.message);
  }

  const fullName = user
    ? `${user.name ?? ""} ${user.last_name ?? ""}`.trim()
    : "Unknown";

  // 3b) Resolve the actual delivery address used for this order — the user
  // table no longer carries a single delivery_address column; addresses now
  // live in user_delivery_address and get stamped per-order onto deliveries.
  let deliveryAddress: string | null = null;
  const { data: days } = await supabase
    .from("meal_plan_day")
    .select("id, delivery_id")
    .eq("meal_plan_id", record?.id);

  const deliveryIds = (days ?? [])
    .map((d: { delivery_id: number | null }) => d.delivery_id)
    .filter((id: number | null): id is number => id !== null);

  if (deliveryIds.length > 0) {
    const { data: deliveryRows } = await supabase
      .from("deliveries")
      .select("delivery_address")
      .in("id", deliveryIds)
      .not("delivery_address", "is", null)
      .limit(1);
    deliveryAddress = deliveryRows?.[0]?.delivery_address ?? null;
  }

  // Fallback in case this webhook fires before meal_plan_day/deliveries are
  // written yet — use the user's default saved address instead.
  if (!deliveryAddress) {
    const { data: defaultAddr } = await supabase
      .from("user_delivery_address")
      .select("address_text")
      .eq("user_id", record?.user_id)
      .eq("is_default", true)
      .limit(1)
      .single();
    deliveryAddress = defaultAddr?.address_text ?? null;
  }

  // 3c) Resolve the payment method used for this order, so the admin can
  // reach out to the client about it if needed.
  let paymentProvider: string | null = null;
  const mealPlanDayIds = (days ?? []).map((d: { delivery_id: number | null; id?: number }) => d.id);
  if (mealPlanDayIds.length > 0) {
    const { data: paymentRows } = await supabase
      .from("payment")
      .select("provider")
      .in("meal_plan_day_id", mealPlanDayIds)
      .not("provider", "is", null)
      .limit(1);
    paymentProvider = paymentRows?.[0]?.provider ?? null;
  }

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
      <li><b>Address:</b> ${deliveryAddress ?? "—"}</li>
    </ul>

    <h3>💳 Payment</h3>
    <ul>
      <li><b>Method:</b> ${paymentProvider ?? "—"}</li>
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