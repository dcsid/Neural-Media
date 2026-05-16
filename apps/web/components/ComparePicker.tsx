"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";
import type { VideoMetadata } from "@shared/types";
import { videoTitle } from "@/lib/format";

interface ComparePickerProps {
  videos: VideoMetadata[];
  selectedA: string | null;
  selectedB: string | null;
}

export function ComparePicker({ videos, selectedA, selectedB }: ComparePickerProps) {
  const router = useRouter();
  const params = useSearchParams();

  const updateParam = useCallback(
    (key: "a" | "b", value: string) => {
      const next = new URLSearchParams(params?.toString() ?? "");
      if (value) {
        next.set(key, value);
      } else {
        next.delete(key);
      }
      const query = next.toString();
      router.replace(query ? `/compare?${query}` : "/compare");
    },
    [params, router],
  );

  return (
    <div className="grid gap-6 border-t border-line py-8 md:grid-cols-2">
      <PickerSlot
        side="A"
        videos={videos}
        selected={selectedA}
        otherSelected={selectedB}
        onChange={(v) => updateParam("a", v)}
      />
      <PickerSlot
        side="B"
        videos={videos}
        selected={selectedB}
        otherSelected={selectedA}
        onChange={(v) => updateParam("b", v)}
      />
    </div>
  );
}

interface PickerSlotProps {
  side: "A" | "B";
  videos: VideoMetadata[];
  selected: string | null;
  otherSelected: string | null;
  onChange: (id: string) => void;
}

function PickerSlot({
  side,
  videos,
  selected,
  otherSelected,
  onChange,
}: PickerSlotProps) {
  return (
    <label className="flex flex-col gap-2">
      <span className="eyebrow">Video {side}</span>
      <select
        value={selected ?? ""}
        onChange={(e) => onChange(e.target.value)}
        className="border border-line bg-canvas px-3 py-2 text-[13px] text-ink-100 focus:border-accent focus:outline-none"
      >
        <option value="">Select a video…</option>
        {videos.map((v) => (
          <option
            key={v.id}
            value={v.id}
            disabled={v.id === otherSelected}
          >
            {videoTitle(v)}
          </option>
        ))}
      </select>
    </label>
  );
}
