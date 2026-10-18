import { useEffect, useState } from "react";

const LAST_MONTH_KEY = "cgc_codegraphcontext_fork_last_month_downloads";
type PypiStats = {
  data: {
    last_day: number;
    last_month: number;
    last_week: number;
  } | null;
  package: string;
  type: string;
};

function getCachedStats(): PypiStats | null {
  try {
    const savedLastMonth = localStorage.getItem(LAST_MONTH_KEY);
    if (!savedLastMonth) return null;

    return {
      data: {
        last_day: 0,
        last_week: 0,
        last_month: Number(savedLastMonth),
      },
      package: "codegraphcontext-fork",
      type: "fallback",
    };
  } catch {
    return null;
  }
}

function cacheLastMonth(lastMonth: number): void {
  try {
    localStorage.setItem(LAST_MONTH_KEY, lastMonth.toString());
  } catch {
    // Cached stats are optional; keep the API response visible if storage is unavailable.
  }
}

export default function ShowDownloads() {
  const [stats, setStats] = useState<PypiStats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
   async function fetchStats() {
  try {
    const res = await fetch("/api/pypi/packages/codegraphcontext-fork/recent");

    if (!res.ok) {
      throw new Error(`API error: ${res.status}`);
    }

    const data = await res.json();

    // Save last successful monthly downloads when browser storage is available.
    if (typeof data?.data?.last_month === "number") {
      cacheLastMonth(data.data.last_month);
    }

    if (!data?.data) {
      const cachedStats = getCachedStats();
      if (cachedStats) {
        setStats(cachedStats);
        return;
      }
    }

    setStats(data);
  } catch (err: unknown) {
    //  Trying to use last saved value
    const cachedStats = getCachedStats();

    if (cachedStats) {
      setStats(cachedStats);
    } else {
      setError((err as Error).message);
    }
  }
}

    fetchStats();
  }, []);

  if (error) return <p className="text-red-500">Error: {error}</p>;
  if (!stats) return <p>Loading stats...</p>;

  return (
    <div>
      {stats?.data ? (
        <>
          <p>Last month downloads: {stats.data.last_month.toLocaleString()}+</p>
        </>
      ) : (
        <p>No data available yet for this package</p>
      )}
    </div>
  );
}
