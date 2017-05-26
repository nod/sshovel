#!/usr/bin/env python

from collections import namedtuple
import argparse
import fcntl
import hashlib
import os
import os.path
import pty
import shutil
import socket
import struct
import sys
import time


class SSHCryptException(Exception):
    pass


def log(message):
    script = os.path.basename(sys.argv[0])
    message = "{}: {}\n".format(script, message)
    sys.stderr.write(message)

def assert_exists(path):
    if not os.path.exists(path):
        raise SSHCryptException('file not found: {}'.format(path))
    if os.path.isdir(path):
        raise SSHCryptException('exists, but is a directory: {}'.format(path))


class Pack():
    @staticmethod
    def byte(b):
        return struct.pack('>B', b)

    @staticmethod
    def long(v):
        return struct.pack('>L', v)

    @staticmethod
    def string(s):
        length = len(s)
        format = '>L{}s'.format(length)
        return struct.pack(format, length, s)

    def __init__(self, read, write):
        self._read = read
        self._write = write

    def write(self, *values):
        body = ''.join(values)
        length = len(body)
        self._write(self.long(length))
        self._write(body)

    def read_byte(self):
        return self.unpack(1, '>B')

    def read_long(self):
        return self.unpack(4, '>L')

    def read_string(self):
        length = self.read_long()
        format = '{}s'.format(length)
        return self.unpack(length, format)

    def unpack(self, length, format):
        bytes = self._read(length)
        return struct.unpack(format, bytes)[0]


class SSH():
    Key = namedtuple('Key', 'blob comment')

    # https://tools.ietf.org/id/draft-miller-ssh-agent-00.html
    AGENTC_REQUEST_IDENTITIES = 11
    AGENT_IDENTITIES_ANSWER = 12
    AGENTC_SIGN_REQUEST = 13
    AGENT_SIGN_RESPONSE = 14
    AGENT_RSA_SHA2_256 = 2
    AGENT_RSA_SHA2_512 = 4

    def __init__(self, socket_path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
        sock.connect(socket_path)
        self.pack = Pack(sock.recv, sock.sendall)

    def identities(self):
        p = self.pack
        p.write(p.byte(SSH.AGENTC_REQUEST_IDENTITIES))
        length = p.read_long()
        response = p.read_byte()
        assert response == SSH.AGENT_IDENTITIES_ANSWER
        num_keys = p.read_long()
        result = []
        for _ in range(num_keys):
            blob = p.read_string()
            comment = p.read_string()
            key = SSH.Key(blob, comment)
            result.append(key)
        return result

    def sign(self, key, message, flags=0):
        p = self.pack
        p.write(
            p.byte(SSH.AGENTC_SIGN_REQUEST),
            p.string(key.blob),
            p.string(message),
            p.long(flags)
        )
        len = p.read_long()
        response = p.read_byte()
        assert response == SSH.AGENT_SIGN_RESPONSE
        signature = p.read_string()
        return signature


class Scrypt():
    def encrypt(self, in_file, out_file, passphrase):
        assert_exists(in_file)
        pid, fd = self.fork('enc', in_file, out_file)
        self.send_passphrase(fd, passphrase)
        self.send_passphrase(fd, passphrase)
        log("encrypting with scrypt...")
        os.waitpid(pid, 0)
        log("done!")

    def decrypt(self, in_file, out_file, passphrase):
        assert_exists(in_file)
        pid, fd = self.fork('dec', in_file, out_file)
        self.send_passphrase(fd, passphrase)
        log("decrypting with scrypt...")
        os.waitpid(pid, 0)
        log("done!")

    def fork(self, command, in_file, out_file):
        pid, fd = pty.fork()
        if pid == 0:
            os.execlp('scrypt', 'scrypt', command, in_file, out_file)
        fcntl.fcntl(fd, fcntl.F_SETFL, os.O_NONBLOCK)
        return pid, fd

    def send_passphrase(self, fd, passphrase):
        self.expect(fd, "passphrase: ", 1)
        os.write(fd, passphrase)
        os.write(fd, "\n")
        os.fsync(fd)
        self.expect(fd, "\r\n", 1)

    def expect(self, fd, phrase, timeout):
        start_time = time.time()
        buf = []
        while 1:
            try:
                c = os.read(fd, 1)
                if c == '':
                    raise SSHCryptException("EOF after reading: " + ''.join(buf))
                buf.append(c)
            except OSError:
                duration = time.time() - start_time
                if duration > timeout:
                    msg = "timed out waiting for '{}'".format(phrase)
                    raise SSHCryptException(msg)
                time.sleep(0.1)
            if ''.join(buf).endswith(phrase):
                return


class SSHScrypt():
    MAGIC = "https://haz.cat/ssh-crypt"

    def encrypt(self, in_file, out_file, ssh, key):
        assert_exists(in_file)
        nonce = os.urandom(128)
        signature = ssh.sign(key, nonce)
        passphrase = hashlib.sha1(signature).hexdigest()
        tmp_file = self.tmp_for(out_file)
        Scrypt().encrypt(in_file, tmp_file, passphrase)
        with open(out_file, 'w') as out_io:
            out_io.write(Pack.string(SSHScrypt.MAGIC))
            out_io.write(Pack.string(nonce))
            with open(tmp_file) as tmp_io:
                shutil.copyfileobj(tmp_io, out_io)

    def decrypt(self, in_file, out_file, ssh, key):
        assert_exists(in_file)
        passphrase = None
        tmp_file = self.tmp_for(out_file)
        with open(in_file) as in_io:
            p = Pack(in_io.read, in_io.write)
            magic = p.read_string()
            nonce = p.read_string()
            signature = ssh.sign(key, nonce)
            passphrase = hashlib.sha1(signature).hexdigest()
            with open(tmp_file, 'w') as tmp_io:
                shutil.copyfileobj(in_io, tmp_io)
        Scrypt().decrypt(tmp_file, out_file, passphrase)

    @staticmethod
    def tmp_for(file):
        dir = os.path.dirname(file)
        if dir == '':
            dir = '.'
        basename = os.path.basename(file)
        return '{}/.{}.ssh-scrypt'.format(dir, basename)


def test():
    TEST_KEY = """
-----BEGIN RSA PRIVATE KEY-----
MIICXAIBAAKBgQD0OCPZ50akyXxhyFz/JdCTZISvHJ+nFOnXMHKQzF3Q3fAbXGVM
jEU2Wer+owj6s4wxuNmd6g3XAyyomCSRoxE6txNpQ10Yay4ZUMhO3XDr3zN5WBhd
6dqDjNLsrfu5mjy9aWFZDpBYnmRnOQBeLGxNQE6shbwOAzsixirmiUyXBQIDAQAB
AoGAZwCallgGIpBcZn10Q6S2UMQPdi/TYkveyITFfS7Ezsgccd3JV7y9oEvSYi1v
JxW9Jmd5WTITPkE3f7ATlF07cT5EZaHPMHm02GJegopN1AW2caoN2N+FHpe2cnOW
0vLtV+dQ0j5QnCWOfPpM70wYqEwvO+tC9uIaIOCBVTdyF20CQQD7IxTeZjiirZ0M
SlSPZmNRlFBRUkHjc4I7A5weV5L5YR/LWN2W/RL0j+v60L18Uld0pGcDuYX1Fg42
oUYEazaLAkEA+PLEMtU7vh3d9PtyDyIG7prcAJnUljwybRgZoVQNMsMxxONbSoAb
B9tw8irock7zjkPYsqvjSAxcfV1I+J6qrwJBAJaFQEzMF8XpKOfk5SnNxFlw+3LC
Spt47+VPFJNbCcxOWjAW4zlMFcBfQqDh27BX6fMPVm71E0UCIyK7JqwfVmECQAJ8
8qcLaIhy5ff/11j9XxJda9t5rh0+Rsa+Wes52tPqDYJJP21UMHD4qX1SHnaeAWMn
nG/UtfXPYdFC8GrDszMCQHNHjkRPhgnFKZBIqg6CjWE0wVWdZRWwLrP7a2YQsX4A
aZkvLueqxAr5SzU9sTiL6tBQAEaESEHOTm11g+IRmFA=
-----END RSA PRIVATE KEY-----
"""

    def assert_equal(expected, actual, what):
        if expected != actual:
            raise SSHCryptException(
                "{}: expected '{}' but got '{}'"
                    .format(what, expected, actual))

    # Pack
    import StringIO
    message = "Hello, world!"
    io = StringIO.StringIO()
    pack = Pack(io.read, io.write)
    pack.write(pack.byte(99), pack.string(message))
    io.seek(0)
    assert_equal(pack.read_long(), 18, "total pack length")
    assert_equal(pack.read_byte(), 99, "packed byte")
    assert_equal(pack.read_string(), message, "packed string")

    # SSH
    import re
    import signal
    import subprocess
    import tempfile
    # - Start an agent
    output = subprocess.check_output('ssh-agent')
    socket_path = re.search('SSH_AUTH_SOCK=(.+?);', output).group(1)
    agent_pid = int(re.search('SSH_AGENT_PID=(\d+)', output).group(1))
    # - Make and load keys
    os.putenv('SSH_AUTH_SOCK', socket_path)
    key_name = 'a_test_ssh_key'
    key_file = tempfile.NamedTemporaryFile(prefix=key_name + '.')
    key_file.write(TEST_KEY)
    key_file.flush()
    subprocess.check_output(['ssh-add', key_file.name], stderr=subprocess.PIPE)
    # - Test SSH
    ssh = SSH(socket_path)
    keys = ssh.identities()
    assert_equal(len(keys), 1, "number of keys loaded in agent")
    key = keys[0]

    # Scrypt
    log("testing Scrypt class")
    plainfile0 = tempfile.NamedTemporaryFile()
    cipherfile = tempfile.NamedTemporaryFile()
    plainfile1 = tempfile.NamedTemporaryFile()
    plainfile0.write(message)
    plainfile0.flush()
    c = Scrypt()
    c.encrypt(plainfile0.name, cipherfile.name, 'PASSPHRASE')
    c.decrypt(cipherfile.name, plainfile1.name, 'PASSPHRASE')
    assert_equal(message, plainfile1.read(), "result of encrypt then decrypt")

    # SSHScrypt
    log("testing SSHScrypt class")
    plainfile0 = tempfile.NamedTemporaryFile()
    cipherfile = tempfile.NamedTemporaryFile()
    plainfile1 = tempfile.NamedTemporaryFile()
    plainfile0.write(message)
    plainfile0.flush()
    c = SSHScrypt()
    c.encrypt(plainfile0.name, cipherfile.name, ssh, key)
    c.decrypt(cipherfile.name, plainfile1.name, ssh, key)
    assert_equal(message, plainfile1.read(), "result of SSHScrypt encrypt and decrypt")

    # ...and tidy up.
    os.kill(agent_pid, signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser(description='Encrypt files with ssh keys')
    parser.add_argument('--encrypt',
            nargs=2,
            metavar=('IN', 'OUT'),
            help='')
    parser.add_argument('--decrypt',
            nargs=2,
            metavar=('IN', 'OUT'),
            help='')
    parser.add_argument('--key',
            metavar='MATCH',
            help='use the ssh key whose comment matches MATCH')
    parser.add_argument('--test',
            action='store_true',
            help='run the test suite')
    args = parser.parse_args()

    if args.test:
        test()
        sys.exit(0)

    socket_path = os.environ.get('SSH_AUTH_SOCK')
    if socket_path == "":
        parser.error("SSH_AUTH_SOCK is empty or unset")

    ssh = SSH(socket_path)
    keys = ssh.identities()
    if len(keys) == 0:
        parser.error("ssh agent has no keys")

    key = keys[0]
    if args.key:
        matches = []
        for candidate in keys:
            if args.key in candidate.comment:
                matches.append(candidate)
        if len(matches) == 0:
            parser.error(
                    "no ssh key matched '{}'; known keys: {}"
                    .format(args.key, [k.comment for k in keys]))
        elif len(matches) == 1:
            key = matches[0]
        else:
            parser.error(
                    "more than one key matched '{}': {}"
                    .format(args.key, [k.comment for k in matches]))

    log("using key '{}'".format(key.comment))

    if args.encrypt:
        in_file, out_file = args.encrypt
        SSHScrypt().encrypt(in_file, out_file, ssh, key)

    if args.decrypt:
        in_file, out_file = args.decrypt
        SSHScrypt().decrypt(in_file, out_file, ssh, key)

try:
    main()
except SSHCryptException as ex:
    log(ex.message)
    sys.exit(1)
