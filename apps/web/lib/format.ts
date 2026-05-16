import { REGION_DESCRIPTIONS, type RegionId } from "@shared/types";

export function formatActivation(value: number, fractionDigits = 2): string {
  if (!Number.isFinite(value)) return "—";
  return value.toFixed(fractionDigits);
}

export function formatDurationSeconds(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const minutes = Math.floor(seconds / 60);
  const remaining = Math.round(seconds - minutes * 60);
  if (minutes < 60) return `${minutes}m ${remaining.toString().padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  const minsPart = minutes - hours * 60;
  return `${hours}h ${minsPart.toString().padStart(2, "0")}m`;
}

export function formatWatchTimeHuman(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0m";
  const totalMinutes = Math.round(seconds / 60);
  if (totalMinutes < 60) return `${totalMinutes}m`;
  const hours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes - hours * 60;
  if (hours < 24) return `${hours}h ${mins.toString().padStart(2, "0")}m`;
  const days = Math.floor(hours / 24);
  const hrs = hours - days * 24;
  return `${days}d ${hrs.toString().padStart(2, "0")}h`;
}

export function formatDateRange(
  first: string | null,
  last: string | null,
): string {
  if (!first || !last) return "—";
  const a = new Date(first);
  const b = new Date(last);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return "—";
  const sameYear = a.getUTCFullYear() === b.getUTCFullYear();
  const dateFmt = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: sameYear ? undefined : "numeric",
  });
  return `${dateFmt.format(a)} – ${dateFmt.format(b)}`;
}

export function regionLabel(id: RegionId): string {
  return REGION_DESCRIPTIONS[id];
}

export function regionShortLabel(id: RegionId): string {
  switch (id) {
    case "v1":
      return "V1";
    case "v2":
      return "V2";
    case "v3":
      return "V3";
    case "v4":
      return "V4";
    case "auditory":
      return "Auditory";
    case "language":
      return "Language";
    case "ffa":
      return "FFA";
    case "vwfa":
      return "VWFA";
  }
}

export const DAY_OF_WEEK_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;

export function formatHour(hour: number): string {
  const h = ((hour % 12) + 12) % 12 || 12;
  const period = hour < 12 ? "am" : "pm";
  return `${h}${period}`;
}

export function videoTitle(meta: { title: string | null; author: string | null; source_url: string; id: string }): string {
  if (meta.title) return meta.title;
  if (meta.author) return `@${meta.author}`;
  try {
    const url = new URL(meta.source_url);
    return `${url.hostname}${url.pathname}`;
  } catch {
    return meta.id.slice(0, 8);
  }
}
