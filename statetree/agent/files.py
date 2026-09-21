"""Bounded no-follow project files and process-level single-writer fencing."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat


MAX_FILE = 1024 * 1024
MAX_FILES = 2000
MAX_PROJECT = 32 * 1024 * 1024
PRIVATE_PARTS = frozenset({'.git', '.statetree', '.aws', '.ssh', '.azure', '.gnupg',
    '.codex', '.agents', '.config', '.cache', '.venv', 'venv', 'node_modules',
    '__pycache__', '.pytest_cache', 'build', 'dist', 'private', 'secrets', 'credentials'})


class UnsafePath(ValueError):
    pass


class ProjectBusy(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    if (not isinstance(name, str) or not name or len(name) > 240 or '\\' in name or ':' in name
            or '\x00' in name or PurePosixPath(name).is_absolute()):
        raise UnsafePath('Use a relative project file path with forward slashes.')
    parts = name.split('/')
    for part in parts:
        low = part.casefold()
        stem = low.split('.')[0]
        if (part in ('', '.', '..') or part.endswith((' ', '.')) or low in PRIVATE_PARTS
                or low == '.env' or low.startswith('.env.')
                or low in ('secret.json', 'secrets.json', 'credentials.json', '.npmrc', '.pypirc', '.netrc')
                or low.endswith(('.pem', '.key', '.pfx', '.p12', '.kdbx'))
                or stem in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                            *(f'lpt{i}' for i in range(1, 10))}):
            raise UnsafePath('Private or unsafe project path is excluded.')
    return name


def reject_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise UnsafePath('Symlinks, junctions and reparse points are not allowed.')


def _windows_open(path, *, directory=False, write=False, create=False, delete=False):
    """No-follow handle, denying deletion/renaming for its lifetime."""
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT / BACKUP_SEMANTICS
    access = 0 if directory else 0x80000000 | (0x40000000 if write else 0)
    if delete:
        access |= 0x10000
    handle = create_file(str(path), access, (1 | 2) if directory else 1, None, 1 if create else 3, flags, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    class Info(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('creation', wintypes.FILETIME),
                    ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                    ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD), ('size_low', wintypes.DWORD),
                    ('links', wintypes.DWORD), ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]
    info = Info()
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)) or info.attributes & 0x400:
        kernel.CloseHandle(handle)
        raise UnsafePath('Reparse points are not allowed.')
    return msvcrt.open_osfhandle(handle, os.O_RDWR if write else os.O_RDONLY)


class SafeFiles:
    def __init__(self, root):
        self.root = Path(root).absolute()
        for candidate in [self.root, *self.root.parents]:
            reject_link(candidate)
        if not self.root.is_dir():
            raise UnsafePath('Project file root is missing.')

    @contextmanager
    def parent(self, name, *, create=False):
        """Hold no-follow directory handles while resolving and using a child."""
        safe_name(name)
        parts = name.split('/')
        descriptors = []
        try:
            if os.name == 'nt':
                path = Path(self.root.anchor)
                descriptors.append(_windows_open(path, directory=True))
                for part in self.root.parts[1:]:
                    path = path / part
                    descriptors.append(_windows_open(path, directory=True))
                for part in parts[:-1]:
                    if part is not None:
                        path = path / part
                        if create:
                            path.mkdir(exist_ok=True)
                    descriptors.append(_windows_open(path, directory=True))
                yield path / parts[-1], None
            else:
                descriptor = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                descriptors.append(descriptor)
                for part in self.root.parts[1:]:
                    descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    descriptors.append(descriptor)
                for part in parts[:-1]:
                    if create:
                        try:
                            os.mkdir(part, dir_fd=descriptor)
                        except FileExistsError:
                            pass
                    descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    descriptors.append(descriptor)
                yield parts[-1], descriptor
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def read(self, name):
        with self.parent(name) as (path, parent_fd):
            descriptor = (_windows_open(path) if os.name == 'nt'
                          else os.open(path, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd))
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    raise UnsafePath('Only ordinary non-hardlinked files are supported.')
                data = stream.read(MAX_FILE + 1)
                if len(data) > MAX_FILE:
                    raise ValueError('Project file exceeds the 1 MiB limit.')
                return data

    def maybe_read(self, name):
        try:
            return self.read(name)
        except FileNotFoundError:
            return None

    def write(self, name, data, expected):
        if len(data) > MAX_FILE:
            raise ValueError('Project file exceeds the 1 MiB limit.')
        with self.parent(name, create=True) as (path, parent_fd):
            if os.name == 'nt':
                descriptor = _windows_open(path, write=True, create=expected is None)
            else:
                flags = os.O_RDWR | os.O_NOFOLLOW
                if expected is None:
                    flags |= os.O_CREAT | os.O_EXCL
                descriptor = os.open(path, flags, 0o600, dir_fd=parent_fd)
            with os.fdopen(descriptor, 'r+b') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    raise UnsafePath('Only ordinary non-hardlinked files are supported.')
                old = stream.read(MAX_FILE + 1)
                if expected is not None and digest(old) != expected:
                    raise ValueError('Expected-content conflict; read the current file before editing.')
                stream.seek(0)
                stream.write(data)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())

    def delete(self, name, expected):
        with self.parent(name) as (path, parent_fd):
            if os.name == 'nt':
                import ctypes
                import msvcrt
                from ctypes import wintypes
                with os.fdopen(_windows_open(path, delete=True), 'rb') as stream:
                    if digest(stream.read(MAX_FILE + 1)) != expected:
                        raise ValueError('Expected-content conflict during deletion.')
                    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                                   ctypes.c_void_p, wintypes.DWORD]
                    pending = ctypes.c_ubyte(1)
                    if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(stream.fileno()), 4,
                                                              ctypes.byref(pending), ctypes.sizeof(pending)):
                        raise ctypes.WinError(ctypes.get_last_error())
            else:
                if digest(self.read(name)) != expected:
                    raise ValueError('Expected-content conflict during deletion.')
                os.unlink(path, dir_fd=parent_fd)

    def names(self):
        names = []
        def walk(prefix=''):
            # Holding each ancestor handle stops Windows directory replacement;
            # POSIX scandir operates on the already opened directory descriptor.
            with self.parent(prefix + '__statetree_entry__') as (_, descriptor):
                directory = self.root / prefix if os.name == 'nt' else descriptor
                with os.scandir(directory) as entries:
                    items = sorted(list(entries), key=lambda entry: entry.name)
                for entry in items:
                    relative = prefix + entry.name
                    try:
                        safe_name(relative)
                    except UnsafePath:
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise UnsafePath('Symlinks, junctions and reparse points are not allowed.')
                    if stat.S_ISDIR(info.st_mode):
                        walk(relative + '/')
                    elif stat.S_ISREG(info.st_mode):
                        names.append(relative)
                        if len(names) > MAX_FILES:
                            raise ValueError('Project exceeds the 2000-file limit.')
                    else:
                        raise UnsafePath('Only ordinary project files are supported.')
        walk()
        return sorted(names)


@contextmanager
def project_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    try:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ProjectBusy('Another local worker holds this project lock.') from error
        yield
    finally:
        stream.close()
