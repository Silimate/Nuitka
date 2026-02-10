#     Copyright 2025, Akash Levy, mailto:akash@silimate.com find license text at end of file


"""Caching of compilation results.

This caches generated C source code and metadata for compiled modules,
allowing site-packages and standard library modules to skip parsing,
optimization, and C code generation on subsequent compilations when
their source has not changed.
"""

import os
import sys

from nuitka.containers.OrderedSets import OrderedSet
from nuitka.importing.Importing import locateModule, makeModuleUsageAttempt
from nuitka.importing.StandardLibrary import isStandardLibraryPath
from nuitka.plugins.Hooks import getPluginsCacheContributionValues
from nuitka.PythonVersions import getSitePackageCandidateNames
from nuitka.Tracing import cache_logger
from nuitka.utils.AppDirs import getCacheDir
from nuitka.utils.FileOperations import (
    getFileContents,
    getNormalizedPath,
    getNormalizedPathJoin,
    makePath,
    putTextFileContents,
)
from nuitka.utils.Hashing import Hash, getStringHash
from nuitka.utils.Json import loadJsonFromFilename, writeJsonToFilename
from nuitka.utils.ModuleNames import ModuleName
from nuitka.Version import version_string


def getCompilationCacheDir():
    """Get the directory for the compilation cache."""
    return getCacheDir("compilation-cache")


# Bump this when the cache format changes.
_cache_format_version = 1


def _getCompilationConfigHash(module_name):
    """Calculate hash value for module compilation configuration.

    This incorporates plugin contributions, Nuitka version, and Python
    version so that cache entries are invalidated when any of these change.
    """
    hash_value = Hash()

    # Plugins may change their influence.
    hash_value.updateFromValues(*getPluginsCacheContributionValues(module_name))

    # Take Nuitka and Python version into account as well.
    hash_value.updateFromValues(version_string, sys.version)

    return hash_value.asHexDigest()


def _makeCompilationCacheName(module_name, source_code):
    """Build a cache directory name from module name, config hash, and source hash."""
    config_hash = _getCompilationConfigHash(module_name)

    return (
        module_name.asLegalFilename()
        + "@"
        + config_hash
        + "@"
        + getStringHash(source_code)
    )


def _getCacheEntryDir(module_name, source_code):
    """Get the cache entry directory path for a module."""
    cache_name = _makeCompilationCacheName(module_name, source_code)
    return getNormalizedPathJoin(getCompilationCacheDir(), cache_name)


def isEligibleForCompilationCache(module_filename):
    """Check if a module is eligible for compilation caching.

    A module is eligible if it resides in site-packages or the standard
    library (but not the main module or an extension module).
    """
    if module_filename is None:
        return False

    normalized = getNormalizedPath(module_filename)

    # Check site-packages
    for candidate in getSitePackageCandidateNames():
        if candidate in normalized:
            return True

    # Check standard library
    if isStandardLibraryPath(normalized):
        return True

    return False


def hasCompilationCacheEntry(module_name, source_code):
    """Check whether a valid cache entry exists for this module."""
    cache_dir = _getCacheEntryDir(module_name, source_code)
    metadata_path = os.path.join(cache_dir, "metadata.json")

    if not os.path.exists(metadata_path):
        return False

    data = loadJsonFromFilename(metadata_path)
    if data is None:
        return False

    if data.get("file_format_version") != _cache_format_version:
        return False

    if data.get("module_name") != module_name.asString():
        return False

    c_source_path = os.path.join(cache_dir, "source.c")
    if not os.path.exists(c_source_path):
        return False

    return True


def _loadAndValidateMetadata(cache_dir, module_name):
    """Load and validate cache metadata from disk.

    Returns:
        The metadata dict, or None if validation failed.
    """
    metadata_path = os.path.join(cache_dir, "metadata.json")

    if not os.path.exists(metadata_path):
        return None

    data = loadJsonFromFilename(metadata_path)
    if data is None:
        return None

    if data.get("file_format_version") != _cache_format_version:
        return None

    if data.get("module_name") != module_name.asString():
        return None

    return data


def _validateUsedModules(data, module_name, source_ref):
    """Validate that cached used module findings still match the environment.

    Returns:
        An OrderedSet of used modules, or None if validation failed.
    """
    used_modules = OrderedSet()

    for module_used in data["modules_used"]:
        used_module_name = ModuleName(module_used["module_name"])

        if module_used["finding"] == "relative":
            _used_module_name, filename, module_kind, finding = locateModule(
                module_name=used_module_name.getBasename(),
                parent_package=used_module_name.getPackageName(),
                level=1,
            )
        else:
            _used_module_name, filename, module_kind, finding = locateModule(
                module_name=used_module_name, parent_package=None, level=0
            )

        if (
            finding != module_used["finding"]
            or module_kind != module_used["module_kind"]
        ):
            cache_logger.info(
                "Compilation cache miss for '%s': module finding changed for '%s'."
                % (module_name.asString(), used_module_name.asString())
            )
            return None

        used_modules.add(
            makeModuleUsageAttempt(
                module_name=used_module_name,
                filename=filename,
                finding=module_used["finding"],
                module_kind=module_used["module_kind"],
                level=0,
                source_ref=source_ref.atLineNumber(module_used["source_ref_line"]),
                reason=module_used["reason"],
            )
        )

    return used_modules


def readCompilationCacheEntry(module_name, source_code, source_ref):
    """Read a compilation cache entry and return its data.

    Returns:
        A dict with keys 'c_source', 'const_data', 'used_modules',
        'distribution_names', 'code_name', 'is_package',
        'compile_time_filename', or None if the cache is invalid.
    """
    cache_dir = _getCacheEntryDir(module_name, source_code)

    data = _loadAndValidateMetadata(cache_dir, module_name)
    if data is None:
        return None

    used_modules = _validateUsedModules(data, module_name, source_ref)
    if used_modules is None:
        return None

    # Read cached C source code.
    c_source_path = os.path.join(cache_dir, "source.c")
    if not os.path.exists(c_source_path):
        return None

    c_source = getFileContents(c_source_path, encoding="latin1")

    # Read cached constants data if present.
    const_path = os.path.join(cache_dir, "constants.const")
    if os.path.exists(const_path):
        const_data = getFileContents(const_path, mode="rb")
    else:
        const_data = None

    cache_logger.info("Compilation cache hit for '%s'." % module_name.asString())

    return {
        "c_source": c_source,
        "const_data": const_data,
        "used_modules": used_modules,
        "distribution_names": data.get("distribution_names", []),
        "code_name": data["code_name"],
        "is_package": data["is_package"],
        "compile_time_filename": data["compile_time_filename"],
        "quick_call_data": data.get("quick_call_data"),
    }


def writeCompilationCacheEntry(
    module_name,
    source_code,
    c_source,
    const_data,
    used_modules,
    distribution_names,
    code_name,
    is_package,
    compile_time_filename,
    quick_call_data,
):
    """Write a compilation cache entry for a module.

    Args:
        module_name: The module name object.
        source_code: The module source code string.
        c_source: The generated C source code string.
        const_data: The constants pickle data as bytes, or None.
        used_modules: The used modules OrderedSet.
        distribution_names: List of distribution names.
        code_name: The C identifier for the module.
        is_package: Whether the module is a package.
        compile_time_filename: The compile-time filename of the module.
        quick_call_data: Dict of quick call helper requirements.
    """
    # We must store many separate pieces of metadata, pylint: disable=too-many-locals
    cache_dir = _getCacheEntryDir(module_name, source_code)
    makePath(cache_dir)

    # Serialize used modules for JSON storage.
    modules_used = [module.asDict() for module in used_modules]
    for module in modules_used:
        module["source_ref_line"] = module["source_ref"].getLineNumber()
        del module["source_ref"]

    metadata = {
        "file_format_version": _cache_format_version,
        "module_name": module_name.asString(),
        "modules_used": modules_used,
        "distribution_names": list(distribution_names),
        "code_name": code_name,
        "is_package": is_package,
        "compile_time_filename": compile_time_filename,
        "quick_call_data": quick_call_data,
    }

    writeJsonToFilename(
        filename=os.path.join(cache_dir, "metadata.json"), contents=metadata
    )

    # Write C source code.
    putTextFileContents(
        filename=os.path.join(cache_dir, "source.c"),
        contents=c_source,
        encoding="latin1",
    )

    # Write constants data if present.
    if const_data is not None:
        const_path = os.path.join(cache_dir, "constants.const")
        with open(const_path, "wb") as const_file:
            const_file.write(const_data)

    cache_logger.info("Compilation cache written for '%s'." % module_name.asString())


#     Part of "Nuitka", an optimizing Python compiler that is compatible and
#     integrates with CPython, but also works on its own.
#
#     Licensed under the GNU Affero General Public License, Version 3 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#        http://www.gnu.org/licenses/agpl.txt
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
