-- Daily health of the dataset. A full day has 144 snapshots (one every 10 minutes).
-- "short_top_snapshots" are runs that stopped well before the 2,000-stream cap, like the
-- early-stop bug on Nov 2-3.
SELECT
    dt,
    count(*) AS snapshots,
    144 - count(*) AS missing_snapshots,
    count_if(NOT top_truncated AND top_stream_count < 1900) AS short_top_snapshots,
    count_if(watchlist_generated_dt IS NOT NULL) AS with_watchlist,
    sum(watchlist_truncated_chunks) AS watchlist_truncated_chunks,
    sum(rows_written) AS rows_written,
    sum(rows_rejected) AS rows_rejected,
    round(100.0 * sum(rows_rejected) / (sum(rows_written) + sum(rows_rejected)), 2) AS pct_rejected
FROM snapshots
GROUP BY dt
ORDER BY dt;
