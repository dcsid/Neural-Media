// Re-export of shared/types.ts so the rest of the web app can import via
// `@/lib/types` without reaching across the workspace boundary. The
// shared file is the single source of truth — do NOT redefine anything
// here.
export * from "@shared/types";
