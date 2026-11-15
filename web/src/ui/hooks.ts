// Small data hooks for the machine panel. Each refetches when a new reading arrives.
import { useEffect, useState } from "react";
import { get, type Explanation, type History } from "../api";

export type Loaded<T> = { data: T | null; error: string | null; loading: boolean };

function useFetch<T>(path: string | null, tick: number): Loaded<T> {
  const [s, setS] = useState<Loaded<T>>({ data: null, error: null, loading: path !== null });
  useEffect(() => {
    if (!path) return;
    let live = true;
    setS((p) => ({ ...p, loading: true }));
    get<T>(path)
      .then((data) => live && setS({ data, error: null, loading: false }))
      .catch((e: Error) => live && setS((p) => ({ data: p.data, error: e.message, loading: false })));
    return () => {
      live = false;
    };
  }, [path, tick]);
  return s;
}

export const useHistory = (id: string | null, tick: number) =>
  useFetch<History>(id ? `/api/history/${id}` : null, tick);
export const useExplain = (id: string | null, tick: number, on: boolean) =>
  useFetch<Explanation>(id && on ? `/api/explain/${id}` : null, tick);

/** One audio element for the whole page, so only one clip plays at a time. */
let current: HTMLAudioElement | null = null;
export function playClip(url: string, onError: (msg: string) => void, then?: () => void) {
  current?.pause();
  const a = new Audio(`${url}${url.includes("?") ? "&" : "?"}t=${Date.now()}`);
  current = a;
  a.onended = () => then?.();
  a.onerror = () =>
    onError("Couldn't play this recording. If the clips aren't on this computer, run make data.");
  a.play().catch(() => onError("The browser blocked playback. Click the button again."));
}
export function stopClip() {
  current?.pause();
}
