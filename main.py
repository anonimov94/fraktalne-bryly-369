"""Lokalny serwer dla wizualizacji "Fraktalne Bryły 369".

Przeglądarki blokują fetch() pliku eksperymenty.json przy otwieraniu strony jako file://,
dlatego aplikację trzeba serwować przez HTTP. Uruchom:  python main.py  [--port 8000] [--no-browser]

Na Linuksie serwer tworzy też na żądanie strony wirtualne wejście "Dzwiek_systemu_FB369" (PulseAudio/PipeWire),
bo przeglądarki oparte na Chromium ukrywają źródła typu "Monitor of …" – po zamknięciu serwera wejście znika.
"""
import argparse
import functools
import http.server
import json
import os
import signal
import socketserver
import subprocess
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / 'ustawienia.json'
MAX_SETTINGS_BYTES = 16 * 1024
SYSTEM_SOURCE = 'fb369_system'
SYSTEM_LABEL = 'Dzwiek_systemu_FB369'   # bez spacji – pactl nie przyjmuje ich w source_properties
_system_lock = threading.Lock()


def pactl(*args):
    return subprocess.run(['pactl', *args], capture_output=True, text=True, timeout=5)


def system_modules():
    """[(indeks, argumenty)] załadowanych modułów remap-source tworzących nasze wirtualne wejście."""
    mods = []
    for line in pactl('list', 'short', 'modules').stdout.splitlines():
        idx, _, rest = line.partition('\t')
        if f'source_name={SYSTEM_SOURCE}' in rest.split():
            mods.append((idx, rest.split()))
    return mods


def ensure_system_source():
    """Wirtualne wejście = monitor bieżącego domyślnego wyjścia (słuchawki, głośniki, Bluetooth).
    Gdy domyślne wyjście się zmieniło, wejście jest tworzone od nowa."""
    with _system_lock:
        sink = pactl('get-default-sink').stdout.strip()
        if not sink:
            raise RuntimeError('brak domyślnego wyjścia audio')
        master = f'master={sink}.monitor'
        mods = system_modules()
        if len(mods) == 1 and master in mods[0][1]:
            return {'label': SYSTEM_LABEL, 'sink': sink}
        for idx, _ in mods:
            pactl('unload-module', idx)
        r = pactl('load-module', 'module-remap-source', master, f'source_name={SYSTEM_SOURCE}',
                  f'source_properties=device.description={SYSTEM_LABEL}')
        if r.returncode:
            raise RuntimeError(r.stderr.strip() or 'pactl load-module nie powiódł się')
        return {'label': SYSTEM_LABEL, 'sink': sink}


def remove_system_source():
    try:
        for idx, _ in system_modules():
            pactl('unload-module', idx)
    except (OSError, subprocess.SubprocessError):
        pass


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    """Wyłącza cache, żeby "Przeładuj eksperymenty.json" zawsze widziało świeżo zapisany plik."""

    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      '.json': 'application/json; charset=utf-8',
                      '.html': 'text/html; charset=utf-8'}

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def send_json(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        """GET /ustawienia – zapamiętane ustawienia światła i dźwięku ({} gdy brak)."""
        if self.path.split('?')[0] != '/ustawienia':
            return super().do_GET()
        try:
            self.send_json(200, json.loads(SETTINGS.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            self.send_json(200, {})

    def do_POST(self):
        """POST /ustawienia – zapisuje obiekt JSON do ustawienia.json (atomowo, przez plik tymczasowy).
        POST /audio/system – tworzy wirtualne wejście z dźwiękiem systemu i zwraca jego nazwę."""
        if self.path.split('?')[0] == '/audio/system':
            try:
                return self.send_json(200, ensure_system_source())
            except FileNotFoundError:
                return self.send_json(501, {'blad': 'brak pactl (PulseAudio/PipeWire)'})
            except (RuntimeError, subprocess.SubprocessError) as e:
                return self.send_json(500, {'blad': str(e)})
        if self.path.split('?')[0] != '/ustawienia':
            return self.send_json(404, {'blad': 'nieznany adres'})
        length = int(self.headers.get('Content-Length') or 0)
        if not 0 < length <= MAX_SETTINGS_BYTES:
            return self.send_json(413, {'blad': f'oczekiwano 1–{MAX_SETTINGS_BYTES} bajtów'})
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return self.send_json(400, {'blad': 'niepoprawny JSON'})
        if not isinstance(data, dict):
            return self.send_json(400, {'blad': 'oczekiwano obiektu JSON'})
        tmp = SETTINGS.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, SETTINGS)
        self.send_json(200, {'ok': True})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def start_server(port, attempts=10):
    """Próbuje kolejnych portów, jeśli wybrany jest zajęty."""
    handler = functools.partial(NoCacheHandler, directory=str(ROOT))
    for p in range(port, port + attempts):
        try:
            return Server(('127.0.0.1', p), handler)
        except OSError:
            print(f'Port {p} zajęty, próbuję {p + 1}…')
    raise SystemExit(f'Brak wolnego portu w zakresie {port}–{port + attempts - 1}')


def _raise_interrupt(*_):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description='Serwer wizualizacji Fraktalne Bryły 369')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-browser', action='store_true', help='nie otwieraj przeglądarki automatycznie')
    args = parser.parse_args()
    # SIGTERM (kill, zamknięcie terminala) kończy serwer tak jak Ctrl+C – z usunięciem wirtualnego wejścia
    signal.signal(signal.SIGTERM, _raise_interrupt)

    with start_server(args.port) as httpd:
        url = f'http://localhost:{httpd.server_address[1]}/index.html'
        print(f'Serwuję {ROOT}\nOtwórz: {url}\nCtrl+C kończy pracę serwera.', flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nZatrzymano serwer.')
        finally:
            remove_system_source()


if __name__ == '__main__':
    main()
