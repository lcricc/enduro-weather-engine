import os
import json
import gzip
import zipfile
import io
import urllib.request
import urllib.error
from urllib.parse import quote, urlparse, unquote

BASE_URL = "https://meteohub.agenziaitaliameteo.it"


def api_request(path, token=None, payload=None):
    headers = {"Accept": "application/json"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = None
    method = "GET"

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"

    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers=headers,
        method=method
    )

    with urllib.request.urlopen(
        request, timeout=60
    ) as response:
        return json.load(response)


def login():
    username = os.environ.get("METEOHUB_USERNAME")
    password = os.environ.get("METEOHUB_PASSWORD")

    if not username or not password:
        raise RuntimeError(
            "Credenziali MeteoHub mancanti"
        )

    result = api_request(
        "/auth/login",
        payload={
            "username": username,
            "password": password
        }
    )

    if isinstance(result, str):
        token = result
    elif isinstance(result, dict):
        token = (
            result.get("access_token")
            or result.get("token")
        )
    else:
        token = None

    if not token:
        raise RuntimeError(
            "Token MeteoHub non trovato"
        )

    return token


def get_filename(fileoutput):
    if not isinstance(fileoutput, str):
        return None

    value = fileoutput.strip()

    if not value:
        return None

    parsed = urlparse(value)

    if parsed.scheme or parsed.netloc:
        return None

    filename = os.path.basename(
        unquote(value.replace("\\", "/"))
    )

    if filename in ("", ".", ".."):
        return None

    return filename


def identify_format(content, content_type):
    if content.startswith(b"PK\x03\x04"):
        return "ZIP"

    if content.startswith(b"\x1f\x8b"):
        return "GZIP"

    if content.startswith(b"\x28\xb5\x2f\xfd"):
        return "ZSTANDARD"

    if content.startswith(b"PAR1"):
        return "PARQUET"

    stripped = content.lstrip()

    if stripped.startswith((b"{", b"[")):
        return "POSSIBILE JSON"

    if stripped.startswith(b"<!DOCTYPE html"):
        return "HTML"

    if stripped.startswith(b"<html"):
        return "HTML"

    return "NON RICONOSCIUTO"


def diagnose_download(filename, token):
    encoded_name = quote(filename, safe="")

    url = (
        BASE_URL
        + "/api/data/"
        + encoded_name
    )

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        },
        method="GET"
    )

    with urllib.request.urlopen(
        request, timeout=120
    ) as response:
        content_type = response.headers.get(
            "Content-Type", "non specificato"
        )

        content_encoding = response.headers.get(
            "Content-Encoding", "non specificato"
        )

        content = response.read()

    print("HTTP: risposta ricevuta")
    print("Content-Type:", content_type)
    print("Content-Encoding:", content_encoding)
    print("Dimensione byte:", len(content))

    detected = identify_format(
        content,
        content_type
    )

    print("Formato rilevato:", detected)

    if detected == "GZIP":
        try:
            decompressed = gzip.decompress(content)
            print(
                "GZIP decompressione riuscita:",
                len(decompressed),
                "byte"
            )
            detected = identify_format(
                decompressed,
                ""
            )
            print(
                "Formato interno:",
                detected
            )
        except (OSError, EOFError):
            print("GZIP non decomprimibile")

    elif detected == "ZIP":
        try:
            with zipfile.ZipFile(
                io.BytesIO(content)
            ) as archive:
                print(
                    "Numero file ZIP:",
                    len(archive.namelist())
                )
                print(
                    "Estensioni presenti:",
                    sorted({
                        os.path.splitext(name)[1].lower()
                        for name in archive.namelist()
                    })
                )
        except zipfile.BadZipFile:
            print("ZIP non valido")

    if detected == "POSSIBILE JSON":
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            print("UTF-8 valido: NO")
            print("Errore decodifica, posizione:", error.start)
        else:
            print("UTF-8 valido: SI")
            try:
                data = json.loads(decoded)
            except json.JSONDecodeError as error:
                print("JSON valido: NO")
                print("Errore JSON:", error.msg)
                print("Riga errore:", error.lineno)
                print("Colonna errore:", error.colno)
                print("Posizione errore:", error.pos)

                # Verifica senza stampare il contenuto se sono
                # presenti piu record JSON, uno per riga.
                lines = [line for line in decoded.splitlines() if line.strip()]
                print("Righe non vuote:", len(lines))
                if len(lines) > 1:
                    valid_lines = 0
                    for line in lines:
                        try:
                            json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        valid_lines += 1
                    print("Righe JSON valide:", valid_lines)
                    print("Possibile JSON Lines:",
                          "SI" if valid_lines == len(lines) else "NO")
            else:
                print("JSON valido: SI")
                print("Tipo struttura:", type(data).__name__)
                if isinstance(data, list):
                    print("Numero elementi:", len(data))
                elif isinstance(data, dict):
                    print("Numero chiavi:", len(data))

    print("Diagnostica download terminata")


def main():
    print("METEOHUB - DIAGNOSTICA FORMATO")

    token = login()
    requests = api_request(
        "/api/requests",
        token
    )

    if not isinstance(requests, list):
        raise RuntimeError(
            "Formato elenco richieste inatteso"
        )

    print("Richieste trovate:", len(requests))

    for item in requests:
        if not isinstance(item, dict):
            continue

        if str(
            item.get("status", "")
        ).upper() != "SUCCESS":
            continue

        filename = get_filename(
            item.get("fileoutput")
        )

        if not filename:
            print(
                "Nome file non disponibile"
            )
            continue

        try:
            diagnose_download(
                filename,
                token
            )

        except urllib.error.HTTPError as error:
            print(
                "Errore HTTP:",
                error.code
            )

        except urllib.error.URLError:
            print(
                "Errore di connessione"
            )

        break

main()
