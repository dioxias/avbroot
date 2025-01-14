import binascii
import contextlib
import getpass
import os
import random
import string
import subprocess
import unittest.mock

# This module calls the openssl binary because AOSP's avbtool.py already does
# that and the operations are simple enough to not require pulling in a
# library.


@contextlib.contextmanager
def _passphrase_fd(passphrase):
    '''
    If the specified passphrase is not None, yield the readable end of a pipe
    that produces the passphrase encoded as UTF-8, followed by a newline. The
    read end of the pipe is marked as inheritable. Both ends of the pipe are
    closed after leaving the context.
    '''

    assert os.name != 'nt'

    if passphrase is None:
        yield None
        return

    # For simplicity, we don't write to the pipe on a thread, so pick a maximum
    # length that doesn't exceed any OS's pipe buffer size, while still being
    # usable for just about every use case.
    if len(passphrase) >= 4096:
        raise ValueError('Passphrase is too long')

    pipe_r, pipe_w = os.pipe()
    write_closed = False

    try:
        os.set_inheritable(pipe_r, True)

        os.write(pipe_w, passphrase.encode('UTF-8'))
        os.write(pipe_w, b'\n')
        os.close(pipe_w)
        write_closed = True

        yield pipe_r
    finally:
        os.close(pipe_r)
        if not write_closed:
            os.close(pipe_w)


class _PopenPassphraseWrapper:
    '''
    Wrapper around subprocess.Popen() that adds arguments for passing in the
    private key passphrase via a pipe on non-Windows systems. On Windows,
    openssl does not support reading from pipes, so the passphrase is passed in
    via an environment variable.
    '''

    def __init__(self, passphrase):
        self.orig_popen = subprocess.Popen
        self.passphrase = passphrase

    def __call__(self, cmd, *args, **kwargs):
        if self.passphrase is not None and cmd and \
                os.path.basename(cmd[0]) == 'openssl':
            if os.name == 'nt':
                # On Windows, opensssl does not support reading the passphrase
                # from a file descriptor. An environment variable is the next
                # best way to handle this.
                if 'env' not in kwargs:
                    kwargs['env'] = dict(os.environ)

                env_var = ''.join(random.choices(string.ascii_letters, k=64))
                kwargs['env'][env_var] = self.passphrase

                new_cmd = [*cmd, '-passin', f'env:{env_var}']

                return self.orig_popen(new_cmd, *args, **kwargs)
            else:
                with _passphrase_fd(self.passphrase) as fd:
                    kwargs['close_fds'] = False

                    new_cmd = [*cmd, '-passin', f'fd:{fd}']

                    return self.orig_popen(new_cmd, *args, **kwargs)

                # The pipe is closed at this point in this process, but the
                # child already inherited the fd and the passphrase is sitting
                # the pipe buffer.
        else:
            return self.orig_popen(cmd, *args, **kwargs)


def inject_passphrase(passphrase):
    '''
    While this context is active, patch subprocess calls to openssl so that
    the passphrase is specified via an injected -passin argument, if it is not
    None. The passphrase is passed to the command via a pipe file descriptor
    (non-Windows) or an environment variable (Windows).
    '''

    return unittest.mock.patch(
        'subprocess.Popen', side_effect=_PopenPassphraseWrapper(passphrase))


def _guess_format(path):
    '''
    Simple heuristic to determine the encoding of a key. This is needed because
    openssl 1.1 doesn't support autodetection.
    '''

    with open(path, 'rb') as f:
        for line in f:
            if line.startswith(b'-----BEGIN '):
                return 'PEM'

    return 'DER'


def _get_modulus(path, passphrase, is_x509):
    '''
    Get the RSA modulus of the given file, which can be a private key or
    certificate.
    '''

    with inject_passphrase(passphrase):
        output = subprocess.check_output([
            'openssl',
            'x509' if is_x509 else 'rsa',
            '-in', path,
            '-inform', _guess_format(path),
            '-noout',
            '-modulus',
        ])

    prefix, delim, suffix = output.strip().partition(b'=')
    if not delim or prefix != b'Modulus':
        raise Exception(f'Unexpected modulus output: {repr(output)}')

    return binascii.unhexlify(suffix)


def max_signature_size(pkey, passphrase):
    '''
    Get the maximum size of a signature signed by the specified RSA key. This
    is equal to the modulus size.
    '''

    return len(_get_modulus(pkey, passphrase, False))


def sign_data(pkey, passphrase, data):
    '''
    Sign <data> with <pkey>.
    '''

    with inject_passphrase(passphrase):
        return subprocess.check_output(
            [
                'openssl', 'pkeyutl',
                '-sign',
                '-inkey', pkey,
                '-keyform', _guess_format(pkey),
                '-pkeyopt', 'digest:sha256',
            ],
            input=data,
        )


def cert_matches_key(cert, pkey, passphrase):
    '''
    Check that the x509 certificate matches the RSA private key.
    '''

    return _get_modulus(cert, None, True) \
        == _get_modulus(pkey, passphrase, False)


def _is_encrypted(pkey):
    '''
    Check if a private key is encrypted.
    '''

    with open(pkey, 'rb') as f:
        for line in f:
            if b'-----BEGIN ENCRYPTED PRIVATE KEY-----' == line.strip():
                return True

    return False


def prompt_passphrase(pkey, passphrase_env_var=None, passphrase_file=None):
    '''
    If the private key is encrypted:

    * try to read from the specified passphrase file (first line with trailing
      line endings stripped)
    * try to read from the passphrase environment variable
    * prompt for the passphrase interactively

    There is no fallback behavior.
    '''

    if not _is_encrypted(pkey):
        return None

    if passphrase_file is not None:
        with open(passphrase_file, 'r') as f:
            passphrase = f.readline().rstrip('\r\n')
    elif passphrase_env_var is not None:
        passphrase = os.environ[passphrase_env_var]
    else:
        passphrase = getpass.getpass(f'Passphrase for {pkey}: ')

    # Verify that it is correct
    with inject_passphrase(passphrase):
        subprocess.check_output(['openssl', 'pkey', '-in', pkey, '-noout'])

    return passphrase
