<?php
// =====================================================================
//  TW1MP data proxy for the community website (twmp.alchemy-fox.de).
//  The site is HTTPS, so the browser cannot fetch the game server's plain
//  HTTP data port directly (mixed content). This script fetches it here on
//  the server side, caches it briefly, and returns it same-origin.
//  Upload next to gilden.html; nothing to configure.
// =====================================================================

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: public, max-age=60');

$BASE      = 'http://87.106.168.34:17070';   // VPS public data port
$CACHE     = sys_get_temp_dir() . '/tw1mp_data_cache.json';
$CACHE_TTL = 60;                             // seconds

// Serve a fresh cache without touching the VPS.
if (is_readable($CACHE) && (time() - filemtime($CACHE)) < $CACHE_TTL) {
    echo file_get_contents($CACHE);
    exit;
}

function fetch_json($url) {
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => 3,
            CURLOPT_TIMEOUT        => 5,
        ]);
        $body = curl_exec($ch);
        curl_close($ch);
    } else {
        $ctx  = stream_context_create(['http' => ['timeout' => 5]]);
        $body = @file_get_contents($url, false, $ctx);
    }
    if ($body === false || $body === null || $body === '') {
        return null;
    }
    return json_decode($body, true);
}

$guilds  = fetch_json($BASE . '/public/guilds');
$ranking = fetch_json($BASE . '/public/ranking');
$summary = fetch_json($BASE . '/public/summary');

// VPS unreachable: fall back to the last good cache, else report offline.
if ($guilds === null && $summary === null) {
    if (is_readable($CACHE)) { echo file_get_contents($CACHE); exit; }
    http_response_code(502);
    echo json_encode(['ok' => false, 'error' => 'Server nicht erreichbar']);
    exit;
}

$out = json_encode([
    'ok'      => true,
    'summary' => $summary,
    'guilds'  => isset($guilds['guilds']) ? $guilds['guilds'] : [],
    'ranking' => $ranking !== null ? $ranking : ['available' => false],
    'updated' => date('c'),
]);

@file_put_contents($CACHE, $out);
echo $out;
