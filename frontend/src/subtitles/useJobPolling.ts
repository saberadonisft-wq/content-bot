import { useEffect, useEffectEvent } from "react";

type JobState = { state: string };

/** One request at a time; changing job or leaving the editor cancels its lifetime. */
export function useJobPolling<T extends JobState>(options: {
  jobId: string | null;
  fetchJob: (id: string, signal: AbortSignal) => Promise<T>;
  onJob: (job: T) => void;
  onError: (error: unknown) => void;
  interval?: number;
  retryDelay?: number;
}) {
  const { jobId, fetchJob, interval = 500, retryDelay = 1200 } = options;
  const onJob = useEffectEvent(options.onJob);
  const onError = useEffectEvent(options.onError);
  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      let delay = interval;
      try {
        const job = await fetchJob(jobId, controller.signal);
        if (controller.signal.aborted) return;
        onJob(job);
        if (["succeeded", "failed", "canceled"].includes(job.state)) return;
      } catch (error) {
        if (controller.signal.aborted) return;
        onError(error);
        delay = retryDelay;
      }
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), delay);
    };
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [jobId, fetchJob, interval, retryDelay]);
}
