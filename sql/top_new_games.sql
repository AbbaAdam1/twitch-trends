-- Watchlist games by average viewers yesterday, with release dates from that day's IGDB pull.
-- Change the date on the first line of the WITH block to look at another day.
WITH day AS (
    SELECT date_format(current_date - INTERVAL '1' DAY, '%Y-%m-%d') AS dt
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
