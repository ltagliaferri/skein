"""Single source for the version the CLI and the local API service report.

Both halves of a SKEIN install — the ``skein`` CLI and the API service it talks
to — ship in the same ``interskein`` distribution, so a mismatch between them
means two different installs are in play (a stale ``uv tool`` venv, a source
checkout on PYTHONPATH, a service left running from an older wheel). Reporting
the *distribution* version from both ends is what makes that detectable; see
``skein doctor``.
"""

from typing import Optional

DISTRIBUTION_NAME = "interskein"


def package_version() -> str:
    """Return the installed ``interskein`` distribution version.

    Falls back to :data:`skein.__version__` when the package metadata is absent
    — running straight from a source checkout that was never installed.
    """
    try:
        from importlib.metadata import version

        return version(DISTRIBUTION_NAME)
    except Exception:  # PackageNotFoundError, or importlib.metadata unavailable
        from skein import __version__

        return __version__


def source_version() -> str:
    """Return the version declared by the ``skein`` package that was imported.

    This can differ from :func:`package_version`: metadata describes the
    *installed distribution*, while this describes the *code actually running*.
    A source checkout on the path beside an older installed wheel makes them
    disagree, and reporting only the metadata would make both ends of a version
    comparison agree on the same wrong number. ``skein doctor`` surfaces the gap.
    """
    from skein import __version__

    return __version__


def distribution_location() -> Optional[str]:
    """Return the directory the ``skein`` package was imported from.

    Diagnostics only: when the CLI and the service disagree on version, the two
    import locations are what tells you which install is which.
    """
    try:
        import skein

        return str(getattr(skein, "__file__", "") or "") or None
    except Exception:
        return None
