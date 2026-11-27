import asyncio

from edge_tts_plugin import parse_sing_utterance
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
