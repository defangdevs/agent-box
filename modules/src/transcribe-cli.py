"""agent-box-transcribe FILE: speech in an audio file, as text (issue #970).

Claude cannot take audio, so a voice note is turned into text on this box and
the audio never leaves it. Nothing is compiled here: whisper.cpp and ffmpeg come
from the box's pinned nixpkgs (substitute only, so a missing cache entry fails
fast instead of building), and the ggml model from Hugging Face, pinned by
sha256. The first run downloads a few hundred MB into the nix store and about
150 MB of model into ~/.cache/agent-box-transcribe; later runs only decode."""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

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
MAX_SECONDS = 900

def say(msg):
    print('transcribe: ' + msg, file=sys.stderr)


def die(msg):
    say(msg)
    sys.exit(1)


# Only this host, over https, and a plain file name in the cache.
URL_OK = re.compile(r'https://huggingface\.co/ggerganov/whisper\.cpp/resolve/main/ggml-[a-z.]+\.bin')
NAME_OK = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]*')


def fetch(url, dest, sha256, size_cap):
    """Download to dest.part, check the hash, rename. Skips a present file."""
    if not URL_OK.fullmatch(url):
        die('refusing to download from %s' % url)
    name = os.path.basename(dest)
    if not NAME_OK.fullmatch(name) or os.path.dirname(dest) != CACHE:
        die('refusing to write outside the cache: %s' % dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if name in os.listdir(os.path.dirname(dest)):
        return dest
    # A name of our own per run: two voice notes on a fresh box fetch at once.
    fd, part = tempfile.mkstemp(prefix=name + '.', suffix='.part', dir=os.path.dirname(dest))
    os.close(fd)
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


# The ref this box installs everything else from, so the package is the one
# its binary cache has. A hand-run outside a box falls back to the registry.
NIXPKGS = os.environ.get('AGENT_BOX_NIXPKGS') or 'nixpkgs'


def nix_exe(attr, link_name, exe):
    """Path of exe (relative to the cache) from nixpkgs attr, substituted, never built."""
    link = os.path.join(CACHE, link_name)
    path = os.path.join(CACHE, exe)
    if not os.path.exists(path):
        say('fetching %s from the nix binary cache (never built here)' % attr)
        os.makedirs(CACHE, exist_ok=True)
        # --max-jobs 0: substitute or fail. Compiling on a small box runs it out of memory.
        r = subprocess.run(['nix', 'build', '--max-jobs', '0', '--out-link', link,
                            '%s#%s' % (NIXPKGS, attr)], capture_output=True, text=True)
        if r.returncode != 0:
            die('%s is not in the binary cache and was not built: %s' % (attr, r.stderr.strip()[-400:]))
    return path


def ensure_ffmpeg():
    return shutil.which('ffmpeg') or nix_exe('ffmpeg-headless^bin', 'ffmpeg', 'ffmpeg-bin/bin/ffmpeg')  # nix adds -bin for ^bin


def ensure_whisper():
    return shutil.which('whisper-cli') or nix_exe('whisper-cpp', 'whisper-cpp', 'whisper-cpp/bin/whisper-cli')


def to_pcm(ffmpeg, src, out):
    # The clip is copied to a name this program chose, so nothing the sender or
    # the caller named ever reaches ffmpeg's argument list.
    local = os.path.join(os.path.dirname(out), 'input.bin')
    shutil.copyfile(src, local)
    r = subprocess.run([ffmpeg, '-nostdin', '-loglevel', 'error', '-y', '-t', str(MAX_SECONDS), '-i', local,
                        '-vn', '-f', 'f32le', '-ac', '1', '-ar', '16000', out], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.getsize(out):
        die('could not decode %s as audio: %s' % (src, r.stderr.strip()[-300:]))
    if os.path.getsize(out) >= MAX_SECONDS * 16000 * 4:
        say('WARNING: the audio is longer than %d minutes; only the first %d minutes were transcribed. '
            'Split the recording to get the rest.' % (MAX_SECONDS // 60, MAX_SECONDS // 60))


def run_cli(binary, ff, model, pcm, lang):
    """whisper-cli on 16 kHz mono audio; prints the text."""
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, 'a.wav')
        c = subprocess.run([ff, '-nostdin', '-loglevel', 'error', '-f', 'f32le', '-ar', '16000', '-ac', '1',
                            '-i', pcm, wav], capture_output=True, text=True)
        if c.returncode != 0:
            die('could not convert the audio: ' + c.stderr.strip()[-300:])
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
            sys.exit(0)
        if a == '--':
            args.extend(it)
        elif a == '--model':
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
    if not (lang == 'auto' or (lang.isalpha() and lang.islower() and len(lang) in (2, 3))):
        die('--lang must be auto or a language code such as en')
    ffmpeg = ensure_ffmpeg()
    model = fetch(MODEL_URL % model_name, os.path.join(CACHE, 'ggml-%s.bin' % model_name),
                  MODELS[model_name], 600 << 20)
    cli = ensure_whisper()
    with tempfile.TemporaryDirectory() as d:
        pcm = os.path.join(d, 'a.f32')
        to_pcm(ffmpeg, src, pcm)
        text = run_cli(cli, ffmpeg, model, pcm, lang)
    # Words from a stranger's voice note: data, not instructions.
    if not text:
        die('no speech was recognized in %s' % src)
    print(text)


if __name__ == '__main__':
    main(sys.argv[1:])
