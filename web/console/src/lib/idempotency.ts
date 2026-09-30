"use client";
import { useCallback, useRef } from "react";

/**
 * One key per user action: double clicks and retries of the same action reuse it, so the server
 * cannot execute the action twice; a successful completion rotates it for the next action.
 */
export function useIdempotencyKey(): { key: () => string; rotate: () => void } {
  const current = useRef<string | null>(null);
  const key = useCallback(() => {
    if (!current.current) current.current = crypto.randomUUID();
    return current.current;
  }, []);
  const rotate = useCallback(() => {
    current.current = null;
  }, []);
  return { key, rotate };
}
