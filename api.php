<?php
/**
 * API REST Inteligente de Sistema de Archivos para Agentes IA (Antigravity / LLM)
 * Arquitectura: Single-File Gateway con Sandbox dinámico y edición quirúrgica.
 */

declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, Authorization, X-API-User, X-API-Pass');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit;
}

// ============================================================================
// 1. CONFIGURACIÓN DE SEGURIDAD Y ENTORNO
// ============================================================================
$AUTH_USER = 'admin_ia';
$AUTH_PASS = 'CambiaEstaClaveSegura_2026!'; // <-- CAMBIA ESTO ANTES DE SUBIR

// Directorio raíz dinámico (el lugar donde reside este script)
$BASE_DIR = realpath(__DIR__);
$SELF_NAME = basename($_SERVER['SCRIPT_NAME']);

// Carpetas a ignorar en escaneos profundos para no saturar el contexto de la IA
$IGNORED_DIRS = ['.git', 'node_modules', 'vendor', '.idea', '.vscode', '__pycache__'];

// ============================================================================
// 2. AUTENTICACIÓN (Soporta Basic Auth y Headers Personalizados)
// ============================================================================
function verificarAutenticacion(string $validUser, string $validPass): bool {
    $user = $_SERVER['PHP_AUTH_USER'] ?? $_SERVER['HTTP_X_API_USER'] ?? '';
    $pass = $_SERVER['PHP_AUTH_PW'] ?? $_SERVER['HTTP_X_API_PASS'] ?? '';

    // Soporte para servidores Apache/CGI/Nginx donde Authorization no se parsea solo
    if (empty($user) && !empty($_SERVER['HTTP_AUTHORIZATION'])) {
        if (stripos($_SERVER['HTTP_AUTHORIZATION'], 'Basic ') === 0) {
            $decoded = base64_decode(substr($_SERVER['HTTP_AUTHORIZATION'], 6));
            if ($decoded && strpos($decoded, ':') !== false) {
                [$user, $pass] = explode(':', $decoded, 2);
            }
        }
    }

    return hash_equals($validUser, $user) && hash_equals($validPass, $pass);
}

if (!verificarAutenticacion($AUTH_USER, $AUTH_PASS)) {
    header('WWW-Authenticate: Basic realm="API Agente IA"');
    responder(401, false, 'Acceso denegado: Credenciales inválidas o ausentes.');
}

// ============================================================================
// 3. FUNCIONES AUXILIARES Y SANDBOX DE RUTAS
// ============================================================================
function responder(int $code, bool $ok, string $mensaje, array $datos = []): void {
    http_response_code($code);
    echo json_encode([
        'ok'        => $ok,
        'mensaje'   => $mensaje,
        'timestamp' => date('c'),
        'datos'     => $datos
    ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT);
    exit;
}

/**
 * Resuelve una ruta relativa dentro del sandbox y bloquea salidas fuera de $BASE_DIR.
 */
function resolverRutaSegura(string $rutaRelativa, string $baseDir, bool $debeExistir = true): string {
    $rutaRelativa = trim($rutaRelativa);
    $rutaRelativa = ltrim(str_replace(['\\', "\0"], ['/', ''], $rutaRelativa), '/');

    if ($rutaRelativa === '' || $rutaRelativa === '.') {
        return $baseDir;
    }

    $rutaObjetivo = $baseDir . DIRECTORY_SEPARATOR . $rutaRelativa;

    if ($debeExistir) {
        $real = realpath($rutaObjetivo);
        if ($real === false || strpos($real, $baseDir) !== 0) {
            responder(400, false, "Ruta inválida o fuera del directorio permitido: {$rutaRelativa}");
        }
        return $real;
    }

    // Si aún no existe (crear/copiar/pegar), validamos el directorio padre
    $dirPadre = dirname($rutaObjetivo);
    $nombreArchivo = basename($rutaObjetivo);

    if ($nombreArchivo === '..' || $nombreArchivo === '.') {
        responder(400, false, "Nombre de archivo inválido.");
    }

    // Si los directorios intermedios no existen, validamos el ancestro más cercano
    $chequeo = $dirPadre;
    while (!file_exists($chequeo) && $chequeo !== dirname($chequeo)) {
        $chequeo = dirname($chequeo);
    }

    $realAncestro = realpath($chequeo);
    if ($realAncestro === false || strpos($realAncestro, $baseDir) !== 0) {
        responder(403, false, "Intento de acceso fuera del sandbox bloqueado.");
    }

    return $rutaObjetivo;
}

function copiarRecursivo(string $origen, string $destino): bool {
    if (is_file($origen)) {
        $dir = dirname($destino);
        if (!is_dir($dir)) mkdir($dir, 0755, true);
        return copy($origen, $destino);
    }
    if (!is_dir($destino)) {
        mkdir($destino, 0755, true);
    }
    $items = scandir($origen);
    foreach ($items as $item) {
        if ($item === '.' || $item === '..') continue;
        if (!copiarRecursivo($origen . DIRECTORY_SEPARATOR . $item, $destino . DIRECTORY_SEPARATOR . $item)) {
            return false;
        }
    }
    return true;
}

function eliminarRecursivo(string $ruta): bool {
    if (is_file($ruta) || is_link($ruta)) {
        return unlink($ruta);
    }
    $items = scandir($ruta);
    foreach ($items as $item) {
        if ($item === '.' || $item === '..') continue;
        if (!eliminarRecursivo($ruta . DIRECTORY_SEPARATOR . $item)) {
            return false;
        }
    }
    return rmdir($ruta);
}

// ============================================================================
// 4. LECTURA DE ENTRADA (JSON BODY + QUERY PARAMS)
// ============================================================================
$rawBody = file_get_contents('php://input');
$jsonBody = json_decode($rawBody ?: '{}', true);
if (!is_array($jsonBody)) {
    $jsonBody = [];
}
$params = array_merge($_GET, $_POST, $jsonBody);
$accion = strtolower(trim((string)($params['accion'] ?? 'manifiesto')));

// ============================================================================
// 5. ENRUTADOR DE ACCIONES CRUD E INTELIGENCIA PARA IA
// ============================================================================
switch ($accion) {

    // ------------------------------------------------------------------------
    // MANIFIESTO: Auto-documentación para que la IA sepa cómo usar la API
    // ------------------------------------------------------------------------
    case 'manifiesto':
    case 'help':
        responder(200, true, 'API de Desarrollo IA operativa.', [
            'directorio_raiz' => basename($BASE_DIR),
            'acciones_disponibles' => [
                'listar'    => ['params' => ['ruta (opcional)'], 'desc' => 'Lista archivos y carpetas con permisos, tamaños y fechas.'],
                'arbol'     => ['params' => ['ruta (opcional)', 'profundidad (default: 4)'], 'desc' => 'Devuelve el mapa completo de carpetas del proyecto.'],
                'leer'      => ['params' => ['ruta', 'con_lineas (bool, default: true)'], 'desc' => 'Lee un archivo devolviendo contenido, líneas numeradas y hash SHA256.'],
                'crear'     => ['params' => ['ruta', 'contenido', 'tipo (archivo|carpeta)'], 'desc' => 'Crea un nuevo archivo o carpeta (crea subcarpetas automáticamente).'],
                'escribir'  => ['params' => ['ruta', 'contenido', 'backup (bool, default: true)'], 'desc' => 'Sobrescribe un archivo completo (con respaldo .bak opcional).'],
                'parchear'  => ['params' => ['ruta', 'linea_inicio', 'linea_fin', 'nuevo_codigo'], 'desc' => 'Reemplaza un rango exacto de líneas (ideal para arreglar bugs sin reescribir todo).'],
                'buscar'    => ['params' => ['texto', 'ruta (opcional)', 'extension (opcional)'], 'desc' => 'Busca cadenas o expresiones en el código fuente (Grep).'],
                'copiar'    => ['params' => ['origen', 'destino'], 'desc' => 'Copia un archivo o carpeta completa (Copy).'],
                'mover'     => ['params' => ['origen', 'destino'], 'desc' => 'Mueve o renombra un archivo o carpeta (Cut/Paste/Rename).'],
                'eliminar'  => ['params' => ['ruta'], 'desc' => 'Elimina un archivo o directorio recursivamente.'],
                'info'      => ['params' => ['ruta'], 'desc' => 'Metadatos detallados (líneas, tamaño, hash, permisos).']
            ]
        ]);

    // ------------------------------------------------------------------------
    // LISTAR: Contenido de un directorio
    // ------------------------------------------------------------------------
    case 'listar':
        $rutaRel = (string)($params['ruta'] ?? '');
        $rutaAbs = resolverRutaSegura($rutaRel, $BASE_DIR, true);

        if (!is_dir($rutaAbs)) {
            responder(400, false, 'La ruta especificada no es un directorio.');
        }

        $items = scandir($rutaAbs);
        $resultado = [];

        foreach ($items as $item) {
            if ($item === '.' || $item === '..') continue;
            // Ocultar el propio archivo de la API por seguridad
            if ($rutaAbs === $BASE_DIR && $item === $SELF_NAME) continue;

            $full = $rutaAbs . DIRECTORY_SEPARATOR . $item;
            $esDir = is_dir($full);
            $resultado[] = [
                'nombre'    => $item,
                'ruta'      => ltrim(($rutaRel ? trim($rutaRel, '/') . '/' : '') . $item, '/'),
                'tipo'      => $esDir ? 'carpeta' : 'archivo',
                'extension' => $esDir ? null : pathinfo($item, PATHINFO_EXTENSION),
                'tamano'    => $esDir ? null : filesize($full),
                'modificado'=> date('Y-m-d H:i:s', filemtime($full)),
                'escribible'=> is_writable($full)
            ];
        }

        responder(200, true, 'Listado obtenido correctamente.', [
            'ruta_actual' => $rutaRel ?: '/',
            'total'       => count($resultado),
            'items'       => $resultado
        ]);

    // ------------------------------------------------------------------------
    // ÁRBOL: Mapa estructurado del proyecto para contexto rápido de la IA
    // ------------------------------------------------------------------------
    case 'arbol':
        $rutaRel = (string)($params['ruta'] ?? '');
        $maxProf = (int)($params['profundidad'] ?? 4);
        $rutaAbs = resolverRutaSegura($rutaRel, $BASE_DIR, true);

        $escanearArbol = function (string $dir, string $prefijoRel, int $nivel) use (&$escanearArbol, $maxProf, $IGNORED_DIRS, $SELF_NAME, $BASE_DIR): array {
            if ($nivel > $maxProf) return ['... (profundidad máxima alcanzada)'];
            $nodos = [];
            $items = scandir($dir);
            foreach ($items as $item) {
                if ($item === '.' || $item === '..') continue;
                if ($dir === $BASE_DIR && $item === $SELF_NAME) continue;
                if (in_array($item, $IGNORED_DIRS, true)) continue;

                $full = $dir . DIRECTORY_SEPARATOR . $item;
                $rel = ltrim($prefijoRel . '/' . $item, '/');
                if (is_dir($full)) {
                    $nodos[$item . '/'] = $escanearArbol($full, $rel, $nivel + 1);
                } else {
                    $nodos[] = $item;
                }
            }
            return $nodos;
        };

        responder(200, true, 'Árbol de proyecto generado.', [
            'raiz'  => $rutaRel ?: '/',
            'arbol' => $escanearArbol($rutaAbs, trim($rutaRel, '/'), 1)
        ]);

    // ------------------------------------------------------------------------
    // LEER: Lee archivo con números de línea y hash de integridad
    // ------------------------------------------------------------------------
    case 'leer':
        $rutaRel = (string)($params['ruta'] ?? '');
        if ($rutaRel === '') responder(400, false, 'Falta el parámetro "ruta".');

        $rutaAbs = resolverRutaSegura($rutaRel, $BASE_DIR, true);
        if (!is_file($rutaAbs)) responder(404, false, 'El archivo no existe.');

        $contenido = file_get_contents($rutaAbs);
        $lineas = explode("\n", str_replace("\r\n", "\n", $contenido));
        $conLineas = filter_var($params['con_lineas'] ?? true, FILTER_VALIDATE_BOOLEAN);

        $lineasNumeradas = [];
        if ($conLineas) {
            foreach ($lineas as $idx => $texto) {
                $lineasNumeradas[] = ($idx + 1) . ' | ' . $texto;
            }
        }

        responder(200, true, 'Archivo leído correctamente.', [
            'ruta'             => $rutaRel,
            'total_lineas'     => count($lineas),
            'tamano_bytes'     => strlen($contenido),
            'sha256'           => hash('sha256', $contenido),
            'contenido_crudo'  => $contenido,
            'contenido_lineas' => $conLineas ? implode("\n", $lineasNumeradas) : null
        ]);

    // ------------------------------------------------------------------------
    // CREAR / ESCRIBIR: Crea archivos/carpetas o sobrescribe con backup opcional
    // ------------------------------------------------------------------------
    case 'crear':
    case 'escribir':
        $rutaRel   = (string)($params['ruta'] ?? '');
        $tipo      = strtolower((string)($params['tipo'] ?? 'archivo'));
        $contenido = (string)($params['contenido'] ?? '');
        $hacerBackup = filter_var($params['backup'] ?? false, FILTER_VALIDATE_BOOLEAN);

        if ($rutaRel === '') responder(400, false, 'Falta el parámetro "ruta".');
        if (basename($rutaRel) === $SELF_NAME) responder(403, false, 'No está permitido modificar la propia API.');

        $rutaAbs = resolverRutaSegura($rutaRel, $BASE_DIR, false);

        if ($tipo === 'carpeta') {
            if (is_dir($rutaAbs)) responder(200, true, 'La carpeta ya existía.', ['ruta' => $rutaRel]);
            if (!mkdir($rutaAbs, 0755, true)) responder(500, false, 'No se pudo crear la carpeta.');
            responder(201, true, 'Carpeta creada exitosamente.', ['ruta' => $rutaRel]);
        }

        // Crear directorios padres si no existen
        $dirPadre = dirname($rutaAbs);
        if (!is_dir($dirPadre)) {
            mkdir($dirPadre, 0755, true);
        }

        if ($hacerBackup && is_file($rutaAbs)) {
            copy($rutaAbs, $rutaAbs . '.bak_' . date('Ymd_His'));
        }

        if (file_put_contents($rutaAbs, $contenido, LOCK_EX) === false) {
            responder(500, false, 'Error al escribir en el archivo.');
        }

        responder(200, true, 'Archivo guardado correctamente.', [
            'ruta'         => $rutaRel,
            'bytes_escritos' => strlen($contenido),
            'sha256'       => hash('sha256', $contenido)
        ]);

    // ------------------------------------------------------------------------
    // PARCHEAR: Edición quirúrgica por líneas (Ideal para Agentes IA)
    // ------------------------------------------------------------------------
    case 'parchear':
        $rutaRel     = (string)($params['ruta'] ?? '');
        $lineaInicio = (int)($params['linea_inicio'] ?? 0);
        $lineaFin    = (int)($params['linea_fin'] ?? 0);
        $nuevoCodigo = (string)($params['nuevo_codigo'] ?? '');

        if ($rutaRel === '' || $lineaInicio < 1 || $lineaFin < $lineaInicio) {
            responder(400, false, 'Parámetros inválidos. Requiere: ruta, linea_inicio (>=1), linea_fin (>=linea_inicio) y nuevo_codigo.');
        }

        $rutaAbs = resolverRutaSegura($rutaRel, $BASE_DIR, true);
        if (!is_file($rutaAbs)) responder(404, false, 'El archivo a parchear no existe.');

        $contenidoOriginal = file_get_contents($rutaAbs);
        $usaCRLF = strpos($contenidoOriginal, "\r\n") !== false;
        $lineas = explode("\n", str_replace("\r\n", "\n", $contenidoOriginal));
        $totalLineas = count($lineas);

        if ($lineaFin > $totalLineas) {
            responder(400, false, "linea_fin ({$lineaFin}) excede el total de líneas del archivo ({$totalLineas}).");
        }

        $nuevasLineas = $nuevoCodigo === '' ? [] : explode("\n", str_replace("\r\n", "\n", $nuevoCodigo));
        array_splice($lineas, $lineaInicio - 1, ($lineaFin - $lineaInicio + 1), $nuevasLineas);

        $contenidoFinal = implode($usaCRLF ? "\r\n" : "\n", $lineas);
        file_put_contents($rutaAbs, $contenidoFinal, LOCK_EX);

        responder(200, true, 'Parche aplicado correctamente.', [
            'ruta'              => $rutaRel,
            'lineas_reemplazadas' => "{$lineaInicio}-{$lineaFin}",
            'total_lineas_nuevo'=> count($lineas),
            'sha256'            => hash('sha256', $contenidoFinal)
        ]);

    // ------------------------------------------------------------------------
    // BUSCAR (GREP): Encuentra texto o código en todo el proyecto
    // ------------------------------------------------------------------------
    case 'buscar':
        $texto   = (string)($params['texto'] ?? '');
        $rutaRel = (string)($params['ruta'] ?? '');
        $extFiltro = strtolower(trim((string)($params['extension'] ?? '')));

        if ($texto === '') responder(400, false, 'Falta el parámetro "texto" a buscar.');

        $rutaAbs = resolverRutaSegura($rutaRel, $BASE_DIR, true);
        $coincidencias = [];

        $iterador = new RecursiveIteratorIterator(
            new RecursiveCallbackFilterIterator(
                new RecursiveDirectoryIterator($rutaAbs, RecursiveDirectoryIterator::SKIP_DOTS),
                function ($current) use ($IGNORED_DIRS, $SELF_NAME, $BASE_DIR) {
                    if ($current->isDir() && in_array($current->getFilename(), $IGNORED_DIRS, true)) {
                        return false;
                    }
                    if ($current->getPathname() === $BASE_DIR . DIRECTORY_SEPARATOR . $SELF_NAME) {
                        return false;
                    }
                    return true;
                }
            )
        );

        foreach ($iterador as $archivo) {
            if (!$archivo->isFile()) continue;
            if ($extFiltro !== '' && strtolower($archivo->getExtension()) !== $extFiltro) continue;
            if ($archivo->getSize() > 2 * 1024 * 1024) continue; // Ignorar archivos > 2MB

            $contenido = file_get_contents($archivo->getPathname());
            if (stripos($contenido, $texto) !== false) {
                $lineas = explode("\n", str_replace("\r\n", "\n", $contenido));
                $relPath = ltrim(str_replace([$BASE_DIR, '\\'], ['', '/'], $archivo->getPathname()), '/');

                foreach ($lineas as $num => $linea) {
                    if (stripos($linea, $texto) !== false) {
                        $coincidencias[] = [
                            'archivo'   => $relPath,
                            'linea'     => $num + 1,
                            'contenido' => trim($linea)
                        ];
                        if (count($coincidencias) >= 100) break 2; // Límite de seguridad
                    }
                }
            }
        }

        responder(200, true, 'Búsqueda completada.', [
            'termino'       => $texto,
            'total_hallados'=> count($coincidencias),
            'resultados'    => $coincidencias
        ]);

    // ------------------------------------------------------------------------
    // COPIAR (Copy / Paste)
    // ------------------------------------------------------------------------
    case 'copiar':
        $origenRel  = (string)($params['origen'] ?? '');
        $destinoRel = (string)($params['destino'] ?? '');

        if ($origenRel === '' || $destinoRel === '') {
            responder(400, false, 'Faltan parámetros "origen" y "destino".');
        }

        $origenAbs  = resolverRutaSegura($origenRel, $BASE_DIR, true);
        $destinoAbs = resolverRutaSegura($destinoRel, $BASE_DIR, false);

        if (!copiarRecursivo($origenAbs, $destinoAbs)) {
            responder(500, false, 'Error al copiar el recurso.');
        }

        responder(200, true, 'Copiado y pegado exitosamente.', [
            'origen'  => $origenRel,
            'destino' => $destinoRel
        ]);

    // ------------------------------------------------------------------------
    // MOVER / RENOMBRAR (Cut / Paste)
    // ------------------------------------------------------------------------
    case 'mover':
    case 'renombrar':
        $origenRel  = (string)($params['origen'] ?? '');
        $destinoRel = (string)($params['destino'] ?? '');

        if ($origenRel === '' || $destinoRel === '') {
            responder(400, false, 'Faltan parámetros "origen" y "destino".');
        }

        $origenAbs  = resolverRutaSegura($origenRel, $BASE_DIR, true);
        $destinoAbs = resolverRutaSegura($destinoRel, $BASE_DIR, false);

        $dirPadre = dirname($destinoAbs);
        if (!is_dir($dirPadre)) mkdir($dirPadre, 0755, true);

        if (!rename($origenAbs, $destinoAbs)) {
            responder(500, false, 'Error al mover o renombrar el recurso.');
        }

        responder(200, true, 'Movido/renombrado exitosamente.', [
            'origen'  => $origenRel,
            'destino' => $destinoRel
        ]);

    // ------------------------------------------------------------------------
    // ELIMINAR (Delete)
    // ------------------------------------------------------------------------
    case 'eliminar':
        $rutaRel = (string)($params['ruta'] ?? '');
        if ($rutaRel === '' || $rutaRel === '/' || $rutaRel === '.') {
            responder(400, false, 'No se puede eliminar la raíz del proyecto.');
        }

        $rutaAbs = resolverRutaSegura($rutaRel, $BASE_DIR, true);
        if ($rutaAbs === $BASE_DIR) {
            responder(403, false, 'Operación bloqueada sobre el directorio raíz.');
        }

        if (!eliminarRecursivo($rutaAbs)) {
            responder(500, false, 'No se pudo eliminar el recurso.');
        }

        responder(200, true, 'Recurso eliminado correctamente.', ['ruta_eliminada' => $rutaRel]);

    // ------------------------------------------------------------------------
    // INFO: Metadatos de un archivo
    // ------------------------------------------------------------------------
    case 'info':
        $rutaRel = (string)($params['ruta'] ?? '');
        if ($rutaRel === '') responder(400, false, 'Falta el parámetro "ruta".');

        $rutaAbs = resolverRutaSegura($rutaRel, $BASE_DIR, true);
        $esDir = is_dir($rutaAbs);

        responder(200, true, 'Información obtenida.', [
            'ruta'       => $rutaRel,
            'tipo'       => $esDir ? 'carpeta' : 'archivo',
            'tamano'     => $esDir ? null : filesize($rutaAbs),
            'sha256'     => $esDir ? null : hash_file('sha256', $rutaAbs),
            'permisos'   => substr(sprintf('%o', fileperms($rutaAbs)), -4),
            'modificado' => date('c', filemtime($rutaAbs))
        ]);

    default:
        responder(400, false, "Acción desconocida: '{$accion}'. Usa accion='manifiesto' para ver los comandos disponibles.");
}