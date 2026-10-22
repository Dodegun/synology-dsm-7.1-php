<?php
declare(strict_types=1);
ini_set('display_errors', 'stderr');
error_reporting(E_ALL);

function require_check(bool $condition, string $name): void {
    if (!$condition) {
        throw new RuntimeException($name);
    }
}

$required = ['bcmath', 'calendar', 'curl', 'dom', 'exif', 'fileinfo', 'ftp',
    'hash', 'iconv', 'json', 'libxml', 'mbstring', 'mysqli', 'mysqlnd', 'openssl',
    'PDO', 'pdo_mysql', 'pdo_sqlite', 'session', 'SimpleXML', 'sockets', 'sqlite3',
    'tokenizer', 'xml', 'xmlreader', 'xmlwriter', 'zip', 'zlib', 'apcu', 'Zend OPcache'];
foreach ($required as $extension) {
    require_check(extension_loaded($extension), 'extension: ' . $extension);
}
require_check(PHP_VERSION === getenv('PHP_EXPECTED_VERSION'), 'PHP version');
require_check(mb_strlen('가나다', 'UTF-8') === 3, 'UTF-8 mbstring');
$euc = mb_convert_encoding('한글', 'EUC-KR', 'UTF-8');
require_check(mb_convert_encoding($euc, 'UTF-8', 'EUC-KR') === '한글', 'Korean encoding roundtrip');
require_check(iconv('EUC-KR', 'UTF-8', $euc) === '한글', 'iconv Korean decoding');

$dom = new DOMDocument();
require_check($dom->loadXML('<root><item>ok</item></root>'), 'XML parse');
$xpath = new DOMXPath($dom);
require_check($xpath->evaluate('string(/root/item)') === 'ok', 'DOM XPath');
require_check((string) simplexml_load_string('<root>ok</root>') === 'ok', 'SimpleXML');
$html = new DOMDocument();
require_check($html->loadHTML('<html><body><p>ok</p></body></html>'), 'HTML parse');

$db = new SQLite3(':memory:');
$db->exec('CREATE TABLE smoke(value TEXT)');
$stmt = $db->prepare('INSERT INTO smoke VALUES(:v)');
$stmt->bindValue(':v', '한글', SQLITE3_TEXT);
$stmt->execute();
require_check($db->querySingle('SELECT value FROM smoke') === '한글', 'SQLite prepared statement');
$pdo = new PDO('sqlite::memory:');
require_check((int)$pdo->query('SELECT 42')->fetchColumn() === 42, 'PDO sqlite');
require_check(in_array('mysql', PDO::getAvailableDrivers(), true), 'PDO mysql driver');
require_check(mysqli_init() instanceof mysqli, 'mysqli initialization');

$tmp = tempnam(sys_get_temp_dir(), 'php84-smoke-');
try {
    $zip = new ZipArchive();
    require_check($zip->open($tmp, ZipArchive::OVERWRITE) === true, 'Zip open');
    $zip->addFromString('test.txt', '한글 zip');
    $zip->close();
    require_check($zip->open($tmp) === true, 'Zip reopen');
    require_check($zip->getFromName('test.txt') === '한글 zip', 'Zip roundtrip');
    $zip->close();
} finally {
    @unlink($tmp);
}

$curl = curl_init('file://' . __FILE__);
curl_setopt($curl, CURLOPT_RETURNTRANSFER, true);
require_check(is_string(curl_exec($curl)), 'curl file transfer');
require_check(curl_errno($curl) === 0, 'curl transfer status');
curl_close($curl);
$tlsUrl = getenv('PHP_SMOKE_TLS_URL');
if ($tlsUrl !== false && $tlsUrl !== '') {
    $curl = curl_init($tlsUrl);
    curl_setopt_array($curl, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CAINFO => getenv('PHP_SMOKE_TLS_CA'),
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_TIMEOUT => 10,
        CURLOPT_PROXY => '',
    ]);
    $body = curl_exec($curl);
    require_check($body === 'php84-local-tls-ok', 'curl verified HTTPS: ' . curl_error($curl));
    require_check(curl_getinfo($curl, CURLINFO_RESPONSE_CODE) === 200, 'HTTPS response status');
    curl_close($curl);
}
$key = openssl_pkey_new(['private_key_bits' => 2048, 'private_key_type' => OPENSSL_KEYTYPE_RSA]);
require_check($key !== false, 'OpenSSL RSA key generation');
require_check(openssl_sign('DSM 7.1 PHP 8.4', $signature, $key, OPENSSL_ALGO_SHA256), 'OpenSSL sign');
$details = openssl_pkey_get_details($key);
require_check(openssl_verify('DSM 7.1 PHP 8.4', $signature, $details['key'], OPENSSL_ALGO_SHA256) === 1, 'OpenSSL verify');
require_check(password_verify('smoke', password_hash('smoke', PASSWORD_BCRYPT)), 'password hash');
require_check(bcadd('0.1', '0.2', 1) === '0.3', 'bcmath');
session_id(bin2hex(random_bytes(16)));
require_check(session_start(), 'session start');
$_SESSION['php84-smoke'] = '한글 session';
session_write_close();
require_check(session_start(), 'session reopen');
require_check($_SESSION['php84-smoke'] === '한글 session', 'file session roundtrip');
session_destroy();
if (apcu_enabled()) {
    require_check(apcu_store('php84-build-smoke', 42), 'APCu store');
    require_check(apcu_fetch('php84-build-smoke') === 42, 'APCu fetch');
    apcu_delete('php84-build-smoke');
}

echo json_encode([
    'ok' => true,
    'sapi' => PHP_SAPI,
    'php' => PHP_VERSION,
    'openssl' => OPENSSL_VERSION_TEXT,
    'curl' => curl_version()['version'],
    'libxml' => LIBXML_DOTTED_VERSION,
    'sqlite' => SQLite3::version()['versionString'],
    'extensions_checked' => count($required),
    'verified_local_https' => $tlsUrl !== false && $tlsUrl !== '',
    'ini_file' => php_ini_loaded_file(),
    'session_upload_progress' => ini_get('session.upload_progress.enabled'),
    'curl_cainfo' => ini_get('curl.cainfo'),
    'openssl_cafile' => ini_get('openssl.cafile'),
    'opcache_jit' => ini_get('opcache.jit'),
    'mysql_server_tested' => false,
], JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR) . "\n";
