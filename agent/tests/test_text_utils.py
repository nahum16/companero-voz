import asyncio

from edge_tts_plugin import (
    detect_accent_change_request,
    extract_breath_segment,
    parse_sing_utterance,
    resolve_spanish_voice,
)
from text_utils import speech_text_filter


async def _collect(chunks: list[str]) -> str:
    async def gen():
        for chunk in chunks:
            yield chunk

    out = [part async for part in speech_text_filter(gen())]
    return "".join(out)


def test_preserva_espacio_inicial_y_texto_plano():
    res = asyncio.run(_collect(["Hola", " mundo", " cómo vas"]))
    assert res == "Hola mundo cómo vas"


def test_elimina_markdown_negrita_y_formato():
    res = asyncio.run(_collect(["Esto es **negrita** y `código` con # título."]))
    assert res == "Esto es negrita y código con  título."


def test_elimina_emojis():
    res = asyncio.run(_collect(["Vamos bien 🚀🔥 por la calle 😎!"]))
    assert res == "Vamos bien  por la calle !"


def test_descarta_bloque_think_partido_entre_chunks():
    res = asyncio.run(_collect(["Hola <thi", "nk>secreto</th", "ink> mundo"]))
    assert " ".join(res.split()) == "Hola mundo"
    assert res == "Hola  mundo"


def test_reemplaza_urls():
    res = asyncio.run(_collect(["Mira https://ejemplo.com/ruta/larga aquí."]))
    assert res == "Mira el enlace aquí."


def test_quita_marcadores_de_lista_despues_de_salto():
    res = asyncio.run(_collect(["Pasos:\n1. Primero\n- Segundo\n• Tercero"]))
    assert res == "Pasos: Primero Segundo Tercero"


def test_emite_carry_sin_think_al_final():
    res = asyncio.run(_collect(["¿Todo bien <th"]))
    assert res == "¿Todo bien <th"


def test_parse_sing_utterance_detecta_y_limpia_etiqueta():
    is_sing, txt = parse_sing_utterance("CANTO: Vamos caminando bajo el sol.")
    assert is_sing is True
    assert txt == "Vamos caminando bajo el sol."

    is_sing2, txt2 = parse_sing_utterance("Hola, ¿cómo estás?")
    assert is_sing2 is False
    assert txt2 == "Hola, ¿cómo estás?"


def test_resolve_spanish_voice_por_pais_y_genero():
    v1, _ = resolve_spanish_voice("acento argentino", "es-CL-LorenzoNeural")
    assert v1 == "es-AR-TomasNeural"

    v2, _ = resolve_spanish_voice("habla como mujer colombiana", "es-CL-LorenzoNeural")
    assert v2 == "es-CO-SalomeNeural"

    v3, _ = resolve_spanish_voice("pon voz de mujer", "es-MX-JorgeNeural")
    assert v3 == "es-MX-DaliaNeural"

    v4, _ = resolve_spanish_voice("español de españa", "es-MX-DaliaNeural")
    assert v4 == "es-ES-ElviraNeural"


def test_detect_accent_change_request_solo_dispara_cuando_piden_cambio():
    assert (
        detect_accent_change_request("Hola, ¿qué sabes de Argentina?", "es-CL-LorenzoNeural")
        is None
    )
    res = detect_accent_change_request(
        "Cambia a acento argentino por favor", "es-CL-LorenzoNeural"
    )
    assert res is not None
    assert res[0] == "es-AR-TomasNeural"

    res2 = detect_accent_change_request(
        "Quiero que hables como mujer venezolana", "es-CL-LorenzoNeural"
    )
    assert res2 is not None
    assert res2[0] == "es-VE-PaolaNeural"


def test_extract_breath_segment_preserva_muletillas_y_puntos_suspensivos():
    # No debe cortar en "Mmm... " cuando sigue minúscula dentro de la misma frase
    short_buf = "Mmm... mira, te cuento."
    assert extract_breath_segment(short_buf, is_first=True) is None

    full_buf = "Mmm... mira, la verdad es que sí me parece una locura total. Y además ayer pasó otra cosa."
    extracted = extract_breath_segment(full_buf, is_first=True)
    assert extracted is not None
    seg, rest = extracted
    assert seg == "Mmm... mira, la verdad es que sí me parece una locura total."
    assert rest.startswith("Y además")


def test_sanitize_chat_context_elimina_stubs_y_fusiona_turnos_usuario():
    from livekit.agents import llm
    from agent import is_stub_assistant_text, sanitize_chat_context

    assert is_stub_assistant_text("M") is True
    assert is_stub_assistant_text("J") is True
    assert is_stub_assistant_text("Uf, por") is True
    assert is_stub_assistant_text("Vale.") is False

    ctx = llm.ChatContext()
    ctx.add_message(role="assistant", content="¡Ey, hola! Qué bueno escucharte... ¿cómo andas?")
    ctx.add_message(role="user", content="Hola.")
    ctx.add_message(role="assistant", content="M")
    ctx.add_message(role="user", content="¿Cómo te llamas?")
    ctx.add_message(role="assistant", content="J")
    ctx.add_message(role="user", content="¡Suscríbete al canal!")
    ctx.add_message(role="user", content="Respóndeme por favor.")

    cleaned = sanitize_chat_context(ctx)
    roles = [m.role for m in cleaned.items if isinstance(m, llm.ChatMessage)]
    texts = [m.text_content for m in cleaned.items if isinstance(m, llm.ChatMessage)]
    assert roles == ["assistant", "user"]
    assert texts[1] == "Hola. ¿Cómo te llamas? Respóndeme por favor."


def test_fs_gateway_intent_and_crud(tmp_path):
    from fs_gateway import execute_fs_action, is_file_action_request

    assert is_file_action_request("Hola, ¿cómo estás hoy?") is False
    assert is_file_action_request("Créame un archivo notas.txt que diga hola") is True
    assert is_file_action_request("Lista los archivos del proyecto") is True

    r_create = execute_fs_action(
        "crear",
        ruta="prueba/notas.txt",
        contenido="Linea 1\nLinea 2\nLinea 3",
        base_dir=tmp_path,
    )
    assert r_create["ok"] is True

    r_read = execute_fs_action("leer", ruta="prueba/notas.txt", base_dir=tmp_path)
    assert r_read["ok"] is True
    assert "2 | Linea 2" in r_read["datos"]["contenido_lineas"]

    r_patch = execute_fs_action(
        "parchear",
        ruta="prueba/notas.txt",
        linea_inicio=2,
        linea_fin=2,
        nuevo_codigo="Linea 2 editada",
        base_dir=tmp_path,
    )
    assert r_patch["ok"] is True

    r_search = execute_fs_action("buscar", texto="editada", base_dir=tmp_path)
    assert r_search["ok"] is True
    assert r_search["datos"]["total_hallados"] == 1

    r_del = execute_fs_action("eliminar", ruta="prueba/notas.txt", base_dir=tmp_path)
    assert r_del["ok"] is True



