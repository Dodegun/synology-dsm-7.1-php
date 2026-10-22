<?php
/**
 * Emit the stock Web Station core/extensions JSON shape from this built PHP.
 * Run with the intended php.ini and all shipped shared modules already loaded.
 * This only reads the current runtime's configuration and writes JSON to stdout.
 */
$series = getenv('PHP_SERIES') ?: '8.4';
if (!in_array($series, ['8.2', '8.3', '8.4'], true)) {
    fwrite(STDERR, "PHP_SERIES must be 8.2, 8.3 or 8.4.\n");
    exit(1);
}
$versionsDocument = json_decode(
    file_get_contents(dirname(__DIR__, 2) . '/versions.json'),
    true,
    512,
    JSON_THROW_ON_ERROR
);
$versions = $versionsDocument['versions'] ?? $versionsDocument;
$expectedVersion = $versions[$series]['version'] ?? null;
if (!is_string($expectedVersion) || PHP_SAPI !== 'cli' || PHP_VERSION !== $expectedVersion) {
    fwrite(STDERR, "Expected the versions.json PHP $series CLI runtime ($expectedVersion).\n");
    exit(1);
}
$prefix = 'php' . str_replace('.', '', $series);

$all = ini_get_all(null, false);
if (!is_array($all)) {
    fwrite(STDERR, "Cannot enumerate the runtime INI directives.\n");
    exit(1);
}

// Prefer configured php.ini values over CLI-only SAPI overrides (for example
// max_execution_time=0), so the metadata describes the same FPM base config.
foreach ($all as $name => &$value) {
    $configured = get_cfg_var($name);
    if ($configured !== false && !is_array($configured)) {
        $value = $configured;
    }
    if ($value !== null && !is_string($value)) {
        $value = is_bool($value) ? ($value ? '1' : '0') : (string)$value;
    }
}
unset($value);

$target = '/var/packages/PHP' . $series . '/target';
$overrides = [
    'extension_dir' => $target . '/usr/local/lib/' . $prefix . '/modules',
    'curl.cainfo' => $target . '/etc/cacert.pem',
    'openssl.cafile' => $target . '/etc/cacert.pem',
    'opcache.jit' => '0',
    'opcache.jit_buffer_size' => '0',
    'session.upload_progress.enabled' => '0',
    'session.save_path' => '/var/services/tmp',
];
foreach ($overrides as $name => $value) {
    // Keep package policy visible even when a compiled-out feature (JIT)
    // does not register its INI directives in this runtime.
    $all[$name] = $value;
}

$result = ['core' => [], 'extensions' => []];
foreach ($all as $name => $value) {
    // Stock Web Station metadata groups unprefixed names in Core and dotted
    // directive names in Extensions. Core is not an ini_get_all() module name.
    $group = str_contains($name, '.') ? 'extensions' : 'core';
    $result[$group][$name] = $value;
}
ksort($result['core']);
ksort($result['extensions']);
echo json_encode($result, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR), "\n";
