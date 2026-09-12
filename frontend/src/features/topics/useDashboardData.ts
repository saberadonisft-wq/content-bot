import { useCallback, useEffect, useEffectEvent, useRef, useState } from "react";
import type { Batch, ItemFilters } from "../../api";
import { DashboardRefresh, emptyDashboard } from "./dashboardRefresh";

export function useDashboardData(options: {
  keywordId: number | null; filters: ItemFilters; enabled: boolean; refreshKey: number;
  onError: (message: string) => void; onCompleted: () => void; onAuthRequired: () => Promise<boolean>;
}) {
  const [data, setData] = useState(emptyDashboard);
  const controller = useRef<DashboardRefresh | null>(null);
  const onError = useEffectEvent((error: unknown) => options.onError(error instanceof Error ? error.message : String(error)));
  const onCompleted = useEffectEvent(() => options.onCompleted());
  const onAuthRequired = useEffectEvent(() => options.onAuthRequired());
  const { keywordId, enabled, refreshKey } = options;
  const { source, session, language, sentiment, topic } = options.filters;

  useEffect(() => {
    const reset = setTimeout(() => setData(emptyDashboard()), 0);
    if (!keywordId) return () => clearTimeout(reset);
    const next = new DashboardRefresh({
      keywordId, filters: { source, session, language, sentiment, topic },
      onData: setData, onError, onCompleted, onAuthRequired,
    });
    controller.current = next;
    return () => {
      clearTimeout(reset);
      next.dispose();
      if (controller.current === next) controller.current = null;
    };
  }, [keywordId, source, session, language, sentiment, topic]);

  useEffect(() => {
    const visibility = () => controller.current?.setVisible(enabled && document.visibilityState !== "hidden");
    visibility();
    document.addEventListener("visibilitychange", visibility);
    return () => document.removeEventListener("visibilitychange", visibility);
  }, [enabled, keywordId, source, session, language, sentiment, topic]);

  useEffect(() => { controller.current?.refresh(); }, [refreshKey]);
  const setBatch = useCallback((batch: Batch | null) => controller.current?.setBatch(batch), []);
  return { ...data, setBatch };
}
