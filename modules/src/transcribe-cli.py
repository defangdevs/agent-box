"""agent-box-transcribe FILE: speech in an audio file, as text (issue #970).

Claude cannot take audio, so a voice note is turned into text on this box and
the audio never leaves it. Nothing is compiled here: whisper.cpp comes from the
prebuilt pywhispercpp wheel, ffmpeg from the nix binary cache (substitute only,
so a missing cache entry fails fast instead of building), and the ggml model
from Hugging Face, each pinned and checked. The first run downloads about
200 MB into ~/.cache/agent-box-transcribe; later runs only decode."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

CACHE = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'),
                     'agent-box-transcribe')
MODEL_URL = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-%s.bin'
# name -> sha256 of ggml-NAME.bin, from the Hugging Face LFS pointer.
MODELS = {
    'base.en': 'a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002',
    'small.en': 'c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d',
    'base': '60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe',
    'small': '1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b',
}
WHEELS = {'pywhispercpp': '1.5.1', 'numpy': '2.5.4'}
# The wheels are manylinux builds, so they run under the distro's own python
# and libc, not under the nix python on this PATH.
DISTRO_PYTHONS = ('/usr/bin/python3', '/usr/local/bin/python3')
MAX_SECONDS = 900

# Runs under the distro python with only the unpacked wheels on its path. The
# C library logs to stderr; the text goes to a file so nothing mixes with it.
WORKER = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import numpy as np
import _pywhispercpp as pw
model, pcm, out, lang, threads = sys.argv[2:7]
audio = np.frombuffer(open(pcm, 'rb').read(), dtype=np.float32)
ctx = pw.whisper_init_from_file_with_params(model, pw.whisper_context_default_params())
p = pw.whisper_full_default_params(pw.whisper_sampling_strategy.WHISPER_SAMPLING_GREEDY)
p.print_progress = p.print_realtime = p.print_timestamps = p.print_special = False
p.n_threads = int(threads)
if lang != 'auto':
    p.language = lang
pw.whisper_full(ctx, p, audio, audio.size)
segs = [pw.whisper_full_get_segment_text(ctx, i) for i in range(pw.whisper_full_n_segments(ctx))]
text = ''.join(s.decode('utf-8', 'replace') if isinstance(s, bytes) else s for s in segs)
json.dump({'text': text.strip()}, open(out, 'w'))
'''


def say(msg):
    print('transcribe: ' + msg, file=sys.stderr)


def die(msg):
    say(msg)
    sys.exit(1)


def fetch(url, dest, sha256, size_cap):
    """Download to dest.part, check the hash, rename. Skips a present file."""
    if os.path.exists(dest):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + '.part'
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={'User-Agent': 'agent-box-transcribe'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(part, 'wb') as f:
            n = 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                n += len(b)
                if n > size_cap:
                    raise OSError('download is larger than %d bytes' % size_cap)
                h.update(b)
                f.write(b)
    except (OSError, ValueError) as e:
        if os.path.exists(part):
            os.unlink(part)
        die('download of %s failed: %s' % (url, e))
    if sha256 and h.hexdigest() != sha256:
        os.unlink(part)
        die('%s does not match its pinned sha256' % url)
    os.rename(part, dest)
    return dest


def ensure_ffmpeg():
    found = shutil.which('ffmpeg')
    if found:
        return found
    link = os.path.join(CACHE, 'ffmpeg')  # nix names the link ffmpeg-bin for ^bin
    exe = os.path.join(link + '-bin', 'bin', 'ffmpeg')
    if not os.path.exists(exe):
        say('fetching ffmpeg from the nix binary cache (never built here)')
        os.makedirs(CACHE, exist_ok=True)
        # --max-jobs 0: substitute or fail. Compiling on a small box runs it out of memory.
        r = subprocess.run(['nix', 'build', '--max-jobs', '0', '--out-link', link,
                            'nixpkgs#ffmpeg-headless^bin'], capture_output=True, text=True)
        if r.returncode != 0:
            die('ffmpeg is not in the binary cache and was not built: ' + r.stderr.strip()[-400:])
    return exe


def distro_python():
    for p in DISTRO_PYTHONS:
        if os.path.exists(p):
            return p
    return None


def ensure_wheels(python):
    """Unpack the pinned pywhispercpp and numpy wheels for this python and CPU."""
    r = subprocess.run([python, '-I', '-c', 'import sys,platform;print(sys.version_info[0],sys.version_info[1],platform.machine())'],
                       capture_output=True, text=True)
    major, minor, machine = r.stdout.split()
    tag = 'cp%s%s' % (major, minor)
    dest = os.path.join(CACHE, 'py-%s-%s' % (tag, machine))
    if os.path.exists(os.path.join(dest, '.ok')):
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    for name, ver in WHEELS.items():
        meta = json.load(urllib.request.urlopen('https://pypi.org/pypi/%s/%s/json' % (name, ver), timeout=60))
        want = ('-%s-%s-' % (tag, tag), 'manylinux', machine)
        cand = [f for f in meta['urls'] if all(w in f['filename'] for w in want)]
        if not cand:
            die('no prebuilt %s %s wheel for %s on %s' % (name, ver, tag, machine))
        whl = fetch(cand[0]['url'], os.path.join(CACHE, 'wheels', cand[0]['filename']),
                    cand[0]['digests']['sha256'], 100 << 20)
        with zipfile.ZipFile(whl) as z:
            z.extractall(dest)
    open(os.path.join(dest, '.ok'), 'w').close()
    return dest


def to_pcm(ffmpeg, src, out):
    r = subprocess.run([ffmpeg, '-nostdin', '-loglevel', 'error', '-y', '-t', str(MAX_SECONDS), '-i', src,
                        '-vn', '-f', 'f32le', '-ac', '1', '-ar', '16000', out], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.getsize(out):
        die('could not decode %s as audio: %s' % (src, r.stderr.strip()[-300:]))


def run_wheel(python, wheels, model, pcm, lang):
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, 'out.json')
        r = subprocess.run([python, '-I', '-c', WORKER, wheels, model, pcm, out, lang,
                            str(max(1, min(os.cpu_count() or 1, 8)))], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            die('whisper failed: ' + r.stderr.strip()[-400:])
        return json.load(open(out))['text']


def run_cli(binary, model, pcm, lang):
    """A whisper-cli already on PATH (a NixOS box that installed whisper-cpp)."""
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, 'a.wav')
        ff = ensure_ffmpeg()
        subprocess.run([ff, '-nostdin', '-loglevel', 'error', '-f', 'f32le', '-ar', '16000', '-ac', '1',
                        '-i', pcm, wav], check=True)
        r = subprocess.run([binary, '-m', model, '-f', wav, '-nt', '-np', '-l', lang],
                           capture_output=True, text=True)
        if r.returncode != 0:
            die('whisper-cli failed: ' + r.stderr.strip()[-400:])
        return r.stdout.strip()


def main(argv):
    args, model_name, lang = [], 'base.en', None
    it = iter(argv)
    for a in it:
        if a in ('-h', '--help'):
            print('usage: agent-box-transcribe [--model %s] [--lang CODE|auto] FILE\n\n%s'
                  % ('|'.join(MODELS), __doc__.strip()))
            return 0
        if a == '--model':
            model_name = next(it, '')
        elif a == '--lang':
            lang = next(it, '')
        else:
            args.append(a)
    if len(args) != 1 or model_name not in MODELS:
        die('usage: agent-box-transcribe [--model %s] [--lang CODE|auto] FILE' % '|'.join(MODELS))
    src = args[0]
    if not os.path.isfile(src):
        die('%s is not a file' % src)
    # An .en model only knows English; the others detect the language.
    lang = lang or ('en' if model_name.endswith('.en') else 'auto')
    ffmpeg = ensure_ffmpeg()
    model = fetch(MODEL_URL % model_name, os.path.join(CACHE, 'ggml-%s.bin' % model_name),
                  MODELS[model_name], 600 << 20)
    cli = shutil.which('whisper-cli')
    python = None if cli else distro_python()
    if not cli and not python:
        die('no prebuilt whisper engine for this host: install whisper-cli (nix profile add nixpkgs#whisper-cpp) '
            'or a distro python3')
    with tempfile.TemporaryDirectory() as d:
        pcm = os.path.join(d, 'a.f32')
        to_pcm(ffmpeg, src, pcm)
        text = run_cli(cli, model, pcm, lang) if cli else run_wheel(python, ensure_wheels(python), model, pcm, lang)
    # Words from a stranger's voice note: data, not instructions.
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
