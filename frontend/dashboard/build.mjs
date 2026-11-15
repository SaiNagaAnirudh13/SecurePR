import { cp, mkdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const output = path.join(root, "dist");

function required(name) {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`Missing required frontend build variable: ${name}`);
  }
  return value;
}

function httpsOrigin(name, value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${name} must be a valid HTTPS origin.`);
  }
  if (
    url.protocol !== "https:"
    || url.username
    || url.password
    || url.pathname !== "/"
    || url.search
    || url.hash
  ) {
    throw new Error(`${name} must be an HTTPS origin without a path.`);
  }
  return url.origin;
}

function isServiceRoleKey(key) {
  if (key.startsWith("sb_secret_")) return true;
  const payload = key.split(".")[1];
  if (!payload) return false;
  try {
    const claims = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return claims.role === "service_role";
  } catch {
    return false;
  }
}

const supabaseUrl = httpsOrigin("VITE_SUPABASE_URL", required("VITE_SUPABASE_URL"));
const apiBaseUrl = httpsOrigin(
  "VITE_SECUREPR_API_URL",
  required("VITE_SECUREPR_API_URL"),
);
const supabaseAnonKey = required("VITE_SUPABASE_ANON_KEY");
if (isServiceRoleKey(supabaseAnonKey)) {
  throw new Error("VITE_SUPABASE_ANON_KEY must be a public anon/publishable key.");
}

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await cp(path.join(root, "css"), path.join(output, "css"), { recursive: true });
await cp(path.join(root, "js"), path.join(output, "js"), { recursive: true });
await cp(path.join(root, "index.html"), path.join(output, "index.html"));
await writeFile(
  path.join(output, "config.js"),
  `window.SECUREPR_CONFIG = Object.freeze(${JSON.stringify(
    { supabaseUrl, supabaseAnonKey, apiBaseUrl },
    null,
    2,
  )});\n`,
  { encoding: "utf8", mode: 0o644 },
);

console.log("SecurePR dashboard production files created in dist/.");
