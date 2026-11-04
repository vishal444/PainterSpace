"""Keep speculative shot artifacts separate from the current accepted shot."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


def signatures(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).digest()
            for p in directory.iterdir() if p.is_file()}


class CompileTransaction:
    """A sibling directory preserves relative USD paths and project depth.

    Top-level files are private copies. Nested resources are read through
    directory symlinks; the compiler only authors top-level shot artifacts.
    Promotion replaces files individually with the root layer last, rolling
    back a failed promotion. This is not a filesystem-wide atomic directory swap.
    """
    def __init__(self, output):
        self.output = Path(output)
        self.directory = self.output.parent
        self.directory.mkdir(parents=True, exist_ok=True)
        self.before = signatures(self.directory)
        self.temporary = tempfile.TemporaryDirectory(
            prefix='.shot-compile-', dir=self.directory.parent)
        self.stage = Path(self.temporary.name)
        for path in self.directory.iterdir():
            target = self.stage / path.name
            if path.is_dir():
                target.symlink_to(path, target_is_directory=True)
            elif path.is_file():
                shutil.copy2(path, target)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.temporary.cleanup()

    def publish(self):
        if signatures(self.directory) != self.before:
            raise RuntimeError('Shot files changed during compilation; retry with the current edits.')
        after = signatures(self.stage)
        changed = [name for name in after if after[name] != self.before.get(name)]
        changed.sort(key=lambda name: (name == self.output.name, name))
        backup = self.stage / '.promotion-backup'
        backup.mkdir()
        installed = []
        try:
            for name in changed:
                destination = self.directory / name
                if destination.exists():
                    shutil.copy2(destination, backup / name)
                os.replace(self.stage / name, destination)
                installed.append(name)
        except Exception:
            for name in reversed(installed):
                destination = self.directory / name
                if (backup / name).exists():
                    os.replace(backup / name, destination)
                else:
                    destination.unlink()
            raise
        # In-process users may still hold a composed layer from the prior shot.
        from pxr import Sdf
        for name in changed:
            layer = Sdf.Layer.Find(str(self.directory / name))
            if layer:
                layer.Reload()


def relocate_report(value, source, destination):
    if isinstance(value, dict):
        return {k: relocate_report(v, source, destination) for k, v in value.items()}
    if isinstance(value, list):
        return [relocate_report(v, source, destination) for v in value]
    if isinstance(value, str):
        return value.replace(str(source) + '/', str(destination) + '/')
    return value
