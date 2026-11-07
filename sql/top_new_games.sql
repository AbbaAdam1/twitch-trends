-- Watchlist games by average viewers on the latest compacted day, with release dates from
-- that day's IGDB pull. To pick a day, replace the first SELECT with: SELECT '2026-11-06' AS dt
WITH day AS (
    SELECT max(dt) AS dt FROM game_daily
),
releases AS (
    SELECT twitch_game_id, name, first_release_date, genres
    FROM release_games
    WHERE dt = (SELECT dt FROM day)
)
SELECT
    g.game_name,
    r.first_release_date,
    date_diff('day', r.first_release_date, CAST((SELECT dt FROM day) AS date)) AS days_since_release,
    round(g.avg_viewers) AS avg_viewers,
    g.peak_viewers,
    round(g.avg_streams, 1) AS avg_streams,
    r.genres
FROM game_daily g
LEFT JOIN releases r ON r.twitch_game_id = g.game_id
WHERE g.dt = (SELECT dt FROM day)
ORDER BY g.avg_viewers DESC
LIMIT 50;
