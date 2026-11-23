// Watchdog Worker for lakelevelfinder.com (plan §8.7). Runs every 10 minutes.
// 1. Keeps the update hourly. GitHub's own cron is unreliable (on the owner's burn-ban site it fired 2 times in
//    15 hours), so when /data/status.json is 55+ minutes old the Worker starts the `update` workflow through
//    GitHub's workflow_dispatch API (secret GH_TOKEN: fine-grained token, this repo only, Actions read/write).
//    GitHub's cron stays as a backup; its `gate` job skips a scheduled run while the data is fresh.
// 2. Starts the daily accuracy check (accuracy.yml) once from 14:00 UTC.
// 3. Emails the owner (Cloudflare Email Routing send_email binding) at most once every 6 hours when:
//    - status.json `generated_at` is more than 3 hours old (or status.json cannot be read),
//    - a source has `consecutive_failures` >= 6,
//    - more than 20% of lakes are Unknown,
//    - or a workflow dispatch call fails.
// status.json is read as data only. Field names follow plan §8.3.
import { EmailMessage } from "cloudflare:email";

const MAX_AGE_H = 3;
const FAIL_STREAK = 6;
const UNKNOWN_SHARE = 0.2;
const QUIET_H = 6;
const DISPATCH_AFTER_MIN = 55;   // start an update once the data is this old
const DISPATCH_GAP_MIN = 25;     // and no dispatch was sent in the last N minutes (a run takes a few minutes incl. queueing)
const ACCURACY_HOUR_UTC = 14;

async function dispatch(env, workflow) {
  const wf = workflow || env.GH_WORKFLOW;
  const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${wf}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": `${env.SITE_DOMAIN}-watchdog`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GH_REF || "main" }),
  });
  if (r.status !== 204) return `${wf} dispatch failed: HTTP ${r.status} ${(await r.text()).slice(0, 200)}`;
  return null;
}

// Share of lakes whose freshness is Unknown, from status.json (plan §8.3): `lakes_unknown` if the pipeline
// writes it, else the sum of each source's `unknown` count over `lakes_total` (or over the sum of each
// source's `lakes`). Returns null when status.json carries no lake counts.
function unknownShare(s) {
  const srcs = Object.values(s.sources || {});
  let unk = Number.isFinite(s.lakes_unknown) ? s.lakes_unknown : srcs.reduce((n, x) => n + (Number(x.unknown) || 0), 0);
  let total = Number(s.lakes_total) || srcs.reduce((n, x) => n + (Number(x.lakes) || 0), 0);
  return total > 0 ? { unk, total } : null;
}

export default {
  async scheduled(event, env, ctx) {
    const problems = [];
    let ageMin = null;
    try {
      const r = await fetch(`https://${env.SITE_DOMAIN}/data/status.json?t=${Date.now()}`, { cf: { cacheTtl: 0 } });
      if (!r.ok) problems.push(`status.json HTTP ${r.status}`);
      else {
        const s = await r.json();
        ageMin = (Date.now() - Date.parse(s.generated_at)) / 6e4;
        if (!(ageMin <= MAX_AGE_H * 60)) problems.push(`pipeline last ran ${(ageMin / 60).toFixed(1)} h ago (${s.generated_at})`);
        for (const [id, src] of Object.entries(s.sources || {}))
          if ((src.consecutive_failures || 0) >= FAIL_STREAK) problems.push(`${id}: ${src.consecutive_failures} failures in a row`);
        const u = unknownShare(s);
        if (u && u.unk / u.total > UNKNOWN_SHARE)
          problems.push(`${u.unk} of ${u.total} lakes (${Math.round((100 * u.unk) / u.total)}%) show "Current level unknown"`);
      }
    } catch (e) {
      problems.push(`status.json fetch failed: ${e}`);
    }

    // Unknown age (fetch failed) also counts as stale: the update itself may fix it.
    if (env.GH_TOKEN && (ageMin === null || ageMin >= DISPATCH_AFTER_MIN)) {
      const lastDispatch = Number((await env.WATCHDOG.get("last_dispatch")) || 0);
      if (Date.now() - lastDispatch >= DISPATCH_GAP_MIN * 6e4) {
        try {
          const err = await dispatch(env);
          if (err) problems.push(err);
          else await env.WATCHDOG.put("last_dispatch", String(Date.now()));
        } catch (e) {
          problems.push(`workflow dispatch failed: ${e}`);
        }
      }
    } else if (!env.GH_TOKEN) {
      console.log("GH_TOKEN not set: hourly dispatch disabled");
    }

    // Daily accuracy spot check, once a day from 14:00 UTC; GitHub's cron is the backup.
    if (env.GH_TOKEN && new Date().getUTCHours() === ACCURACY_HOUR_UTC) {
      const lastAcc = Number((await env.WATCHDOG.get("last_accuracy")) || 0);
      if (Date.now() - lastAcc >= 20 * 3.6e6) {
        try {
          const err = await dispatch(env, env.GH_ACCURACY_WORKFLOW || "accuracy.yml");
          if (err) problems.push(err);
          else await env.WATCHDOG.put("last_accuracy", String(Date.now()));
        } catch (e) {
          problems.push(`accuracy dispatch failed: ${e}`);
        }
      }
    }

    if (!problems.length) return;
    const last = Number((await env.WATCHDOG.get("last_alert")) || 0);
    if (Date.now() - last < QUIET_H * 3.6e6) return;
    const body = [
      `From: watchdog@${env.SITE_DOMAIN}`, `To: ${env.ALERT_TO}`, `Subject: [lake levels] site needs attention`,
      `Message-ID: <${Date.now()}@${env.SITE_DOMAIN}>`, "Content-Type: text/plain; charset=utf-8", "",
      ...problems, "", "Runbook: docs/runbook.md in the repo.",
    ].join("\r\n");
    await env.ALERT.send(new EmailMessage(`watchdog@${env.SITE_DOMAIN}`, env.ALERT_TO, body));
    await env.WATCHDOG.put("last_alert", String(Date.now()));
  },
};
