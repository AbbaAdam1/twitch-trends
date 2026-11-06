-- One game's audience day by day. Replace the name with any game from top_new_games.
SELECT
    dt,
    round(avg_viewers) AS avg_viewers,
    peak_viewers,
    round(avg_streams, 1) AS avg_streams,
    live_snapshots,
    watchlist_snapshots
FROM game_daily
WHERE game_name = 'Phantom Blade Zero'
ORDER BY dt;
