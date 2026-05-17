// Re-export of shared/types.ts so the rest of the web app can import via
// `@/lib/types` without reaching across the workspace boundary. The
// shared file is the single source of truth — do NOT redefine anything
// here.
export * from "@shared/types";

// Local additions for the /import flow, pending the canonical drop into
// shared/types.ts. See lib/import-types.ts for the proposed shape.
export * from "./import-types";
