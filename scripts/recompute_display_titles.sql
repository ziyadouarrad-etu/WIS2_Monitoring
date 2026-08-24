-- =====================================================================
-- RECOMPUTE display_title FOR ALL ALERTS (BACKFILL)
-- =====================================================================
-- Replicates telemetry/_compute_display_title() (wis2_ingestion.py) for
-- every row in the `events` table so existing records get the same
-- categorized titles as newly-ingested events.
--
-- How to run (on the server, against the wis2_alerts database):
--     psql -h <DB_HOST> -U <DB_USER> -d wis2_alerts -f scripts/recompute_display_titles.sql
--
-- Safe: idempotent, and only touches rows whose computed title differs
-- from the stored one (IS DISTINCT FROM guard). Run the preview first to
-- see exactly which rows will change.
--
-- NOTE ON REGEX: '\yEOF\y' is a plain string literal (no E'' prefix).
-- With standard_conforming_strings=on (default since PG 9.1) the
-- backslashes are preserved verbatim, so the regex engine sees
-- \yEOF\y (word boundary "EOF"). Do NOT write it as E'\yEOF\y'.
--
-- ORDER MATTERS: branches are evaluated top to bottom. The "Unknown
-- Error: no details" branch MUST stay after the Target branch (and after
-- every error branch) so real errors are never swallowed by it. It only
-- fires for NON-EMPTY descriptions whose only content is the
-- ",collection time ..." suffix.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1) PREVIEW (optional, recommended first) -- lists rows that WILL change
-- ---------------------------------------------------------------------
-- SELECT a.id::text,
--        a.display_title AS old_title,
--        CASE
--          WHEN COALESCE(a.title,'') LIKE '%Template:%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%un-wmo-global-test%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%CMA Global Monitor%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%CMA Global Services%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%CMA Global Broker%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%GISC Beijing%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%DWD Service%' THEN 'Maintenance'
--          WHEN LOWER(COALESCE(a.title,'')) LIKE '%maintenance%' THEN 'Maintenance'
--          WHEN COALESCE(a.description,'') LIKE '%context deadline exceeded%' THEN 'Timeout: context deadline exceeded'
--          WHEN COALESCE(a.description,'') LIKE '%unexpected EOF%' THEN 'Network Termination: unexpected EOF'
--          WHEN COALESCE(a.description,'') ~ '\yEOF\y' THEN 'Network Termination: unexpected EOF'
--          WHEN COALESCE(a.description,'') LIKE '%GOAWAY%' THEN 'Network Termination: server sent GOAWAY'
--          WHEN COALESCE(a.description,'') LIKE '%client connection lost%' THEN 'Network Termination: client connection lost'
--          WHEN COALESCE(a.description,'') LIKE '%connection refused%' THEN 'Connection Refused'
--          WHEN COALESCE(a.description,'') LIKE '%network is unreachable%' THEN 'Network Error: network is unreachable'
--          WHEN COALESCE(a.description,'') LIKE '%no route to host%' THEN 'Network Error: no route to host'
--          WHEN COALESCE(a.description,'') ~ 'HTTP status [0-9]{3}' THEN
--            'HTTP Error: ' || SUBSTRING(COALESCE(a.description,'') FROM 'HTTP status ([0-9]{3})')
--            || CASE SUBSTRING(COALESCE(a.description,'') FROM 'HTTP status ([0-9]{3})')
--                 WHEN '400' THEN ' Bad Request' WHEN '401' THEN ' Unauthorized'
--                 WHEN '403' THEN ' Forbidden' WHEN '404' THEN ' Not Found'
--                 WHEN '500' THEN ' Internal Server Error' WHEN '502' THEN ' Bad Gateway'
--                 WHEN '503' THEN ' Service Unavailable' WHEN '504' THEN ' Gateway Timeout'
--                 ELSE '' END
--          WHEN COALESCE(a.description,'') LIKE '%expected a valid start token%' THEN 'Invalid Response'
--          WHEN COALESCE(a.description,'') LIKE '%connection reset by peer%' THEN 'Network Termination: connection reset by peer'
--          WHEN COALESCE(a.description,'') LIKE '%i/o timeout%' THEN 'Timeout: i/o timeout'
--          WHEN COALESCE(a.title,'') LIKE 'Target %' AND COALESCE(a.title,'') LIKE '% is down%' THEN 'Target is down'
--          WHEN COALESCE(a.description,'') <> '' AND REGEXP_REPLACE(COALESCE(a.description,''), ',collection time .*$', '') = '' THEN 'Unknown Error: no details'
--          ELSE COALESCE(a.title,'') END AS new_title
-- FROM events a
-- WHERE a.display_title IS DISTINCT FROM (
--        CASE
--          WHEN COALESCE(a.title,'') LIKE '%Template:%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%un-wmo-global-test%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%CMA Global Monitor%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%CMA Global Services%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%CMA Global Broker%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%GISC Beijing%' THEN 'Maintenance'
--          WHEN COALESCE(a.title,'') LIKE '%DWD Service%' THEN 'Maintenance'
--          WHEN LOWER(COALESCE(a.title,'')) LIKE '%maintenance%' THEN 'Maintenance'
--          WHEN COALESCE(a.description,'') LIKE '%context deadline exceeded%' THEN 'Timeout: context deadline exceeded'
--          WHEN COALESCE(a.description,'') LIKE '%unexpected EOF%' THEN 'Network Termination: unexpected EOF'
--          WHEN COALESCE(a.description,'') ~ '\yEOF\y' THEN 'Network Termination: unexpected EOF'
--          WHEN COALESCE(a.description,'') LIKE '%GOAWAY%' THEN 'Network Termination: server sent GOAWAY'
--          WHEN COALESCE(a.description,'') LIKE '%client connection lost%' THEN 'Network Termination: client connection lost'
--          WHEN COALESCE(a.description,'') LIKE '%connection refused%' THEN 'Connection Refused'
--          WHEN COALESCE(a.description,'') LIKE '%network is unreachable%' THEN 'Network Error: network is unreachable'
--          WHEN COALESCE(a.description,'') LIKE '%no route to host%' THEN 'Network Error: no route to host'
--          WHEN COALESCE(a.description,'') ~ 'HTTP status [0-9]{3}' THEN
--            'HTTP Error: ' || SUBSTRING(COALESCE(a.description,'') FROM 'HTTP status ([0-9]{3})')
--            || CASE SUBSTRING(COALESCE(a.description,'') FROM 'HTTP status ([0-9]{3})')
--                 WHEN '400' THEN ' Bad Request' WHEN '401' THEN ' Unauthorized'
--                 WHEN '403' THEN ' Forbidden' WHEN '404' THEN ' Not Found'
--                 WHEN '500' THEN ' Internal Server Error' WHEN '502' THEN ' Bad Gateway'
--                 WHEN '503' THEN ' Service Unavailable' WHEN '504' THEN ' Gateway Timeout'
--                 ELSE '' END
--          WHEN COALESCE(a.description,'') LIKE '%expected a valid start token%' THEN 'Invalid Response'
--          WHEN COALESCE(a.description,'') LIKE '%connection reset by peer%' THEN 'Network Termination: connection reset by peer'
--          WHEN COALESCE(a.description,'') LIKE '%i/o timeout%' THEN 'Timeout: i/o timeout'
--          WHEN COALESCE(a.title,'') LIKE 'Target %' AND COALESCE(a.title,'') LIKE '% is down%' THEN 'Target is down'
--          WHEN COALESCE(a.description,'') <> '' AND REGEXP_REPLACE(COALESCE(a.description,''), ',collection time .*$', '') = '' THEN 'Unknown Error: no details'
--          ELSE COALESCE(a.title,'') END)
-- ORDER BY a.event_time DESC;

-- ---------------------------------------------------------------------
-- 2) THE BACKFILL UPDATE
-- ---------------------------------------------------------------------
UPDATE events AS a
SET display_title = c.new_title
FROM (
    SELECT id,
        CASE
            WHEN t LIKE '%Template:%'            THEN 'Maintenance'
            WHEN t LIKE '%un-wmo-global-test%'   THEN 'Maintenance'
            WHEN t LIKE '%CMA Global Monitor%'   THEN 'Maintenance'
            WHEN t LIKE '%CMA Global Services%'  THEN 'Maintenance'
            WHEN t LIKE '%CMA Global Broker%'    THEN 'Maintenance'
            WHEN t LIKE '%GISC Beijing%'         THEN 'Maintenance'
            WHEN t LIKE '%DWD Service%'          THEN 'Maintenance'
            WHEN LOWER(t) LIKE '%maintenance%'   THEN 'Maintenance'

            WHEN d LIKE '%context deadline exceeded%' THEN 'Timeout: context deadline exceeded'
            WHEN d LIKE '%unexpected EOF%'            THEN 'Network Termination: unexpected EOF'
            WHEN d ~ '\yEOF\y'                        THEN 'Network Termination: unexpected EOF'
            WHEN d LIKE '%GOAWAY%'                    THEN 'Network Termination: server sent GOAWAY'
            WHEN d LIKE '%client connection lost%'    THEN 'Network Termination: client connection lost'
            WHEN d LIKE '%connection refused%'        THEN 'Connection Refused'
            WHEN d LIKE '%network is unreachable%'    THEN 'Network Error: network is unreachable'
            WHEN d LIKE '%no route to host%'          THEN 'Network Error: no route to host'

            WHEN d ~ 'HTTP status [0-9]{3}' THEN
                'HTTP Error: ' || SUBSTRING(d FROM 'HTTP status ([0-9]{3})')
                || CASE SUBSTRING(d FROM 'HTTP status ([0-9]{3})')
                       WHEN '400' THEN ' Bad Request'
                       WHEN '401' THEN ' Unauthorized'
                       WHEN '403' THEN ' Forbidden'
                       WHEN '404' THEN ' Not Found'
                       WHEN '500' THEN ' Internal Server Error'
                       WHEN '502' THEN ' Bad Gateway'
                       WHEN '503' THEN ' Service Unavailable'
                       WHEN '504' THEN ' Gateway Timeout'
                       ELSE ''
                   END

            WHEN d LIKE '%expected a valid start token%' THEN 'Invalid Response'
            WHEN d LIKE '%connection reset by peer%'     THEN 'Network Termination: connection reset by peer'
            WHEN d LIKE '%i/o timeout%'                  THEN 'Timeout: i/o timeout'

            WHEN t LIKE 'Target %' AND t LIKE '% is down%' THEN 'Target is down'
            WHEN d <> '' AND REGEXP_REPLACE(d, ',collection time .*$', '') = '' THEN 'Unknown Error: no details'

            ELSE t
        END AS new_title
    FROM (
        SELECT id, COALESCE(title, '') AS t, COALESCE(description, '') AS d
        FROM events
    ) AS base
) AS c
WHERE a.id = c.id
  AND a.display_title IS DISTINCT FROM c.new_title;
