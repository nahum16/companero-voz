import base64
import hashlib
import json
import os
import re
import shutil
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

IGNORED_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    ".idea",
    ".vscode",
    "__pycache__",
    ".venv",
    ".next",
}

PROTECTED_FILENAMES = {"api.php", ".env", ".env.local"}

_FS_INTENT_RE = re.compile(
    r"(?:"
    r"\b(?:"
    r"archivo|archivos|carpeta|carpetas|directorio|directorios|"
    r"fichero|ficheros|repositorio|servidor|sandbox|log|logs|firewall|"
    r"manifiesto|arbol|parchea|parchear|renombra|renombrar|"
    r"adentro\s+pon(?:le)?|ponle\s+adentro|agregale|anadele|escribele|"
    r"revisa(?:r|me)?\s+(?:el\s+|los\s+)?(?:log|logs|archivo|archivos|codigo|firewall)|"
    r"lista(?:r|me)?\s+(?:los\s+|las\s+)?(?:archivos|carpetas|directorios|proyecto)|"
    r"le(?:e|er|eme)\s+(?:el\s+)?(?:archivo|codigo|fichero|log)|"
    r"crea(?:r|me)?\s+(?:un\s+|una\s+|el\s+|la\s+)?(?:archivo|carpeta|pagina|script|documento|nota)|"
    r"guarda(?:r|me)?\s+(?:en\s+|un\s+)?(?:archivo|carpeta|documento|nota)|"
    r"escrib(?:e|ir|eme)\s+(?:en\s+|el\s+|un\s+)?(?:archivo|documento|nota)|"
    r"edita(?:r|me)?\s+(?:el\s+)?(?:archivo|codigo)|"
    r"modifica(?:r|me)?\s+(?:el\s+)?(?:archivo|codigo)|"
    r"busca(?:r|me)?\s+(?:en\s+)?(?:los\s+archivos|el\s+proyecto|el\s+codigo|el\s+log)|"
    r"elimina(?:r|me)?\s+(?:el\s+|la\s+)?(?:archivo|carpeta)|"
    r"borra(?:r|me)?\s+(?:el\s+|la\s+)?(?:archivo|carpeta)|"
    r"copia(?:r|me)?\s+(?:el\s+|la\s+)?(?:archivo|carpeta)|"
    r"muev(?:e|er|eme)\s+(?:el\s+|la\s+)?(?:archivo|carpeta)"
    r")\b"
    r"|"
    r"\b[\w\-]+\.(?:txt|md|php|html|css|js|ts|tsx|py|json|yaml|yml|sql|sh|log)\b"
    r")",
    re.IGNORECASE,
)


def _strip_accents(text: str) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(ch) != "Mn"
    )


def is_file_action_request(user_text: str) -> bool:
    """Detecta si el mensaje del usuario pide consultar, crear o modificar archivos/carpetas."""
    norm = _strip_accents(user_text)
    return bool(_FS_INTENT_RE.search(norm))


def get_sandbox_base_dir() -> Path:
    """Devuelve el directorio raíz del sandbox (por defecto la raíz del proyecto donde vive api.php)."""
    configured = os.getenv("FS_SANDBOX_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parent.parent


def _resolve_safe_path(relative_path: str, base_dir: Path, *, must_exist: bool = True) -> Path:
    rel = relative_path.strip().replace("\\", "/").replace("\0", "").lstrip("/")
    if not rel or rel == ".":
        return base_dir

    target = (base_dir / rel).resolve()
    try:
        target.relative_to(base_dir)
    except ValueError as exc:
        raise ValueError(f"Ruta fuera del sandbox permitido: {relative_path}") from exc

    if must_exist and not target.exists():
        raise FileNotFoundError(f"La ruta no existe: {relative_path}")

    return target


def _resp(ok: bool, mensaje: str, datos: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": ok,
        "mensaje": mensaje,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "datos": datos or {},
    }


def _call_remote_php_api(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    user = os.getenv("FS_API_USER", "admin_ia")
    password = os.getenv("FS_API_PASS", "CambiaEstaClaveSegura_2026!")
    basic_token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Basic {basic_token}",
            "X-API-User": user,
            "X-API-Pass": password,
            "User-Agent": "CompaneroVoz-Agent/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def execute_fs_action(
    accion: str,
    *,
    ruta: str = "",
    contenido: str = "",
    tipo: str = "archivo",
    texto: str = "",
    extension: str = "",
    origen: str = "",
    destino: str = "",
    linea_inicio: int = 0,
    linea_fin: int = 0,
    nuevo_codigo: str = "",
    profundidad: int = 3,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Ejecuta una acción de api.php:
    - Si FS_API_URL está definida en el entorno, llama por HTTP a ese api.php remoto/local.
    - Si no está definida, ejecuta exactamente la misma lógica de api.php sobre el sandbox local.
    """
    remote_url = os.getenv("FS_API_URL", "").strip()
    action = accion.strip().lower() or "manifiesto"

    if remote_url:
        payload = {
            "accion": action,
            "ruta": ruta,
            "contenido": contenido,
            "tipo": tipo,
            "texto": texto,
            "extension": extension,
            "origen": origen,
            "destino": destino,
            "linea_inicio": linea_inicio,
            "linea_fin": linea_fin,
            "nuevo_codigo": nuevo_codigo,
            "profundidad": profundidad,
        }
        try:
            return _call_remote_php_api(remote_url, payload)
        except Exception as exc:
            return _resp(False, f"Error al conectar con api.php remoto ({remote_url}): {exc}")

    root = (base_dir or get_sandbox_base_dir()).resolve()

    try:
        if action in ("manifiesto", "help"):
            return _resp(
                True,
                "API de Sistema de Archivos operativa.",
                {
                    "directorio_raiz": root.name,
                    "acciones_disponibles": [
                        "listar",
                        "arbol",
                        "leer",
                        "crear",
                        "escribir",
                        "parchear",
                        "buscar",
                        "copiar",
                        "mover",
                        "eliminar",
                        "info",
                    ],
                },
            )

        if action == "listar":
            target = _resolve_safe_path(ruta, root, must_exist=True)
            if not target.is_dir():
                return _resp(False, "La ruta especificada no es un directorio.")
            items = []
            for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if entry.name in IGNORED_DIRS or entry.name in PROTECTED_FILENAMES:
                    continue
                is_dir = entry.is_dir()
                rel = entry.relative_to(root).as_posix()
                stat = entry.stat()
                items.append(
                    {
                        "nombre": entry.name,
                        "ruta": rel,
                        "tipo": "carpeta" if is_dir else "archivo",
                        "tamano": None if is_dir else stat.st_size,
                    }
                )
            return _resp(
                True,
                "Listado obtenido correctamente.",
                {"ruta_actual": ruta or "/", "total": len(items), "items": items[:60]},
            )

        if action == "arbol":
            target = _resolve_safe_path(ruta, root, must_exist=True)
            max_depth = max(1, min(int(profundidad or 3), 4))

            def _scan(dir_path: Path, level: int) -> Any:
                if level > max_depth:
                    return ["..."]
                nodes: dict[str, Any] = {}
                for entry in sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    if entry.name in IGNORED_DIRS or entry.name in PROTECTED_FILENAMES:
                        continue
                    if entry.is_dir():
                        nodes[entry.name + "/"] = _scan(entry, level + 1)
                    else:
                        nodes[entry.name] = "archivo"
                return nodes

            return _resp(
                True,
                "Árbol de proyecto generado.",
                {"raiz": ruta or "/", "arbol": _scan(target, 1)},
            )

        if action == "leer":
            if not ruta:
                return _resp(False, 'Falta el parámetro "ruta".')
            target = _resolve_safe_path(ruta, root, must_exist=True)
            if target.name in PROTECTED_FILENAMES:
                return _resp(False, "Archivo protegido por seguridad.")
            if not target.is_file():
                return _resp(False, "El archivo no existe.")
            raw = target.read_text(encoding="utf-8", errors="replace")
            lines = raw.replace("\r\n", "\n").split("\n")
            numbered = "\n".join(f"{i + 1} | {line}" for i, line in enumerate(lines[:120]))
            if len(numbered) > 2500:
                numbered = numbered[:2500] + "\n... (truncado para voz)"
            return _resp(
                True,
                "Archivo leído correctamente.",
                {
                    "ruta": ruta,
                    "total_lineas": len(lines),
                    "tamano_bytes": len(raw.encode("utf-8")),
                    "contenido_lineas": numbered,
                },
            )

        if action in ("crear", "escribir"):
            if not ruta:
                return _resp(False, 'Falta el parámetro "ruta".')
            target = _resolve_safe_path(ruta, root, must_exist=False)
            if target.name in PROTECTED_FILENAMES:
                return _resp(False, "No está permitido sobrescribir archivos protegidos del sistema.")
            if tipo.strip().lower() == "carpeta":
                target.mkdir(parents=True, exist_ok=True)
                return _resp(True, "Carpeta creada exitosamente.", {"ruta": ruta})
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contenido, encoding="utf-8")
            digest = hashlib.sha256(contenido.encode("utf-8")).hexdigest()[:12]
            return _resp(
                True,
                "Archivo guardado correctamente.",
                {
                    "ruta": target.relative_to(root).as_posix(),
                    "bytes_escritos": len(contenido.encode("utf-8")),
                    "sha256": digest,
                },
            )

        if action == "parchear":
            if not ruta or linea_inicio < 1 or linea_fin < linea_inicio:
                return _resp(
                    False,
                    "Parámetros inválidos. Requiere ruta, linea_inicio (>=1) y linea_fin (>=linea_inicio).",
                )
            target = _resolve_safe_path(ruta, root, must_exist=True)
            if target.name in PROTECTED_FILENAMES:
                return _resp(False, "No está permitido parchear archivos protegidos.")
            if not target.is_file():
                return _resp(False, "El archivo a parchear no existe.")
            original = target.read_text(encoding="utf-8", errors="replace")
            use_crlf = "\r\n" in original
            lines = original.replace("\r\n", "\n").split("\n")
            if linea_fin > len(lines):
                return _resp(
                    False,
                    f"linea_fin ({linea_fin}) excede el total de líneas ({len(lines)}).",
                )
            replacement = (
                []
                if nuevo_codigo == ""
                else nuevo_codigo.replace("\r\n", "\n").split("\n")
            )
            lines[linea_inicio - 1 : linea_fin] = replacement
            final_text = ("\r\n" if use_crlf else "\n").join(lines)
            target.write_text(final_text, encoding="utf-8")
            return _resp(
                True,
                "Parche aplicado correctamente.",
                {
                    "ruta": ruta,
                    "lineas_reemplazadas": f"{linea_inicio}-{linea_fin}",
                    "total_lineas_nuevo": len(lines),
                },
            )

        if action == "buscar":
            query = (texto or contenido).strip()
            if not query:
                return _resp(False, 'Falta el parámetro "texto" a buscar.')
            target = _resolve_safe_path(ruta, root, must_exist=True)
            ext_filter = extension.strip().lower().lstrip(".")
            matches: list[dict[str, Any]] = []

            for file_path in target.rglob("*"):
                if any(part in IGNORED_DIRS for part in file_path.parts):
                    continue
                if not file_path.is_file() or file_path.name in PROTECTED_FILENAMES:
                    continue
                if ext_filter and file_path.suffix.lower().lstrip(".") != ext_filter:
                    continue
                try:
                    if file_path.stat().st_size > 1024 * 1024:
                        continue
                    file_text = file_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if query.lower() in file_text.lower():
                    rel = file_path.relative_to(root).as_posix()
                    for idx, line in enumerate(file_text.splitlines()):
                        if query.lower() in line.lower():
                            matches.append(
                                {
                                    "archivo": rel,
                                    "linea": idx + 1,
                                    "contenido": line.strip()[:140],
                                }
                            )
                            if len(matches) >= 25:
                                break
                if len(matches) >= 25:
                    break

            return _resp(
                True,
                "Búsqueda completada.",
                {"termino": query, "total_hallados": len(matches), "resultados": matches},
            )

        if action == "copiar":
            if not origen or not destino:
                return _resp(False, 'Faltan parámetros "origen" y "destino".')
            src = _resolve_safe_path(origen, root, must_exist=True)
            dst = _resolve_safe_path(destino, root, must_exist=False)
            if dst.name in PROTECTED_FILENAMES:
                return _resp(False, "Destino protegido.")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            return _resp(True, "Copiado exitosamente.", {"origen": origen, "destino": destino})

        if action in ("mover", "renombrar"):
            if not origen or not destino:
                return _resp(False, 'Faltan parámetros "origen" y "destino".')
            src = _resolve_safe_path(origen, root, must_exist=True)
            dst = _resolve_safe_path(destino, root, must_exist=False)
            if src.name in PROTECTED_FILENAMES or dst.name in PROTECTED_FILENAMES:
                return _resp(False, "No se puede mover un archivo protegido.")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return _resp(True, "Movido/renombrado exitosamente.", {"origen": origen, "destino": destino})

        if action == "eliminar":
            if not ruta or ruta.strip() in ("/", "."):
                return _resp(False, "No se puede eliminar la raíz del proyecto.")
            target = _resolve_safe_path(ruta, root, must_exist=True)
            if target == root or target.name in PROTECTED_FILENAMES:
                return _resp(False, "Operación bloqueada sobre archivo o directorio protegido.")
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return _resp(True, "Recurso eliminado correctamente.", {"ruta_eliminada": ruta})

        if action == "info":
            if not ruta:
                return _resp(False, 'Falta el parámetro "ruta".')
            target = _resolve_safe_path(ruta, root, must_exist=True)
            stat = target.stat()
            is_dir = target.is_dir()
            return _resp(
                True,
                "Información obtenida.",
                {
                    "ruta": ruta,
                    "tipo": "carpeta" if is_dir else "archivo",
                    "tamano": None if is_dir else stat.st_size,
                    "modificado": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                },
            )

        return _resp(False, f"Acción desconocida: '{action}'.")
    except Exception as exc:
        return _resp(False, f"Error en operación de archivos: {exc}")
