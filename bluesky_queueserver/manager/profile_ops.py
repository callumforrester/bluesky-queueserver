import os
import glob
import runpy
import inspect
from collections.abc import Iterable
import pkg_resources
import yaml
import tempfile
import re
import sys

import logging

logger = logging.getLogger(__name__)


def get_default_profile_collection_dir():
    """
    Returns the path to the default profile collection that is distributed with the package.
    The function does not guarantee that the directory exists.
    """
    pc_path = pkg_resources.resource_filename("bluesky_queueserver", "profile_collection_sim/")
    return pc_path


_patch1 = """

import logging
logger_patch = logging.Logger(__name__)

__local_namespace = locals()

try:
    pass  # Prevent errors when patching an empty file

"""

_patch2 = """

    class IPDummy:
        def __init__(self, user_ns):
            self.user_ns = user_ns

            # May be this should be some meaningful logger (used by 'configure_bluesky_logging')
            self.log = logging.Logger('ipython_patch')


    def get_ipython_patch():
        ip_dummy = IPDummy(__local_namespace)
        return ip_dummy

    get_ipython = get_ipython_patch

"""

_patch3 = """

except BaseException as ex:
    logger_patch.exception("Exception while loading profile: '%s'", str(ex))
    raise

"""


def _patch_profile(file_name):
    """
    Patch the profile (.py file from a beamline profile collection).
    Patching includes placing the code in the file in ``try..except..` block
    and inserting patch for ``get_ipython()`` function after the line
    ``from IPython import get_python``.

    The patched file is saved to the temporary file ``qserver/profile_temp.py``
    in standard directory for the temporary files. For Linux it is ``/tmp``.
    It is assumed that files in profile collection are processed one by one, so
    overwriting the same temporary file is a good way to eliminate resource leaks.

    Parameters
    ----------
    file_name: str
        full path to the patched file.

    Returns
    -------
    str
        full path to the patched temporary file.
    """

    # On Linux the temporary .py file will be always '/tmp/qserver/profile_temp.py'
    #   On other systems the file will be placed in appropriate location, but
    #   it will always be the same file.
    tmp_dir = os.path.join(tempfile.gettempdir(), "qserver")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_fln = os.path.join(tmp_dir, "profile_temp.py")

    with open(file_name, "r") as fln_in:
        code = fln_in.readlines()

    def get_prefix(s):
        # Returns the sequence of spaces and tabs at the beginning of the code line
        prefix = ""
        while s and (s == " " or s == "\t"):
            prefix += s[0]
            s = s[1:]
        return prefix

    with open(tmp_fln, "w") as fln_out:
        # insert 'try ..'
        fln_out.writelines(_patch1)
        is_patched = False
        for line in code:
            fln_out.write("    " + line)
            # The following RE patterns cover only the cases of commenting with '#'.
            if not is_patched:
                if re.search(r"^[^#]*IPython[^#]+get_ipython", line):
                    # Keep the same indentation as in the preceding line
                    prefix = get_prefix(line)
                    for lp in _patch2:
                        fln_out.write(prefix + lp)
                    is_patched = True  # Patch only once
                elif re.search(r"^[^#]*get_ipython *\(", line):
                    # 'get_ipython()' is called before the patch was applied
                    raise RuntimeError(
                        "Profile calls 'get_ipython' before the patch was "
                        "applied. Inspect and correct the code."
                    )
        # insert 'except ..'
        fln_out.writelines(_patch3)

    return tmp_fln


def load_profile_collection(path, patch_profiles=True):
    """
    Load profile collection located at the specified path. The collection consists of
    .py files started with 'DD-', where D is a digit (e.g. 05-file.py). The files
    are alphabetically sorted before execution.

    Parameters
    ----------
    path: str
        path to profile collection
    patch_profiles: boolean
        enable/disable patching profiles. At this point there is no reason not to
        patch profiles.

    Returns
    -------
    nspace: dict
        namespace in which the profile collection was executed
    """

    # Create the list of files to load
    path = os.path.expanduser(path)
    path = os.path.abspath(path)

    if not os.path.exists(path):
        raise IOError(f"Path '{path}' does not exist.")
    if not os.path.isdir(path):
        raise IOError(f"Failed to load the profile collection. Path '{path}' is not a directory.")

    file_pattern = os.path.join(path, "[0-9][0-9]*.py")
    file_list = glob.glob(file_pattern)
    file_list.sort()  # Sort in alphabetical order

    # Add original path to the profile collection to allow local imports
    #   from the patched temporary file.
    if path not in sys.path:
        # We don't want to add/remove the path if it is already in `sys.path` for some reason.
        sys.path.append(path)
        path_is_set = True
    else:
        path_is_set = False

    # Load the files into the namespace 'nspace'.
    try:
        nspace = None
        for file in file_list:
            logger.info(f"Loading startup file '{file}' ...")
            fln_tmp = _patch_profile(file) if patch_profiles else file
            nspace = runpy.run_path(fln_tmp, nspace)

        # Discard RE and db from the profile namespace (if they exist).
        nspace.pop("RE", None)
        nspace.pop("db", None)
    finally:
        try:
            if path_is_set:
                sys.path.remove(path)
        except Exception:
            pass

    return nspace


def plans_from_nspace(nspace):
    """
    Extract plans from the namespace. Currently the function returns the dict of callable objects.

    Parameters
    ----------
    nspace: dict
        Namespace that may contain plans.

    Returns
    -------
    dict(str: callable)
        Dictionary of Bluesky plans
    """
    plans = {}
    for name, obj in nspace.items():
        if callable(obj) and obj.__module__ != "typing":
            plans[name] = obj
    return plans


def devices_from_nspace(nspace):
    """
    Extract devices from the namespace. Currently the function returns the dict of ophyd.Device objects.

    Parameters
    ----------
    nspace: dict
        Namespace that may contain plans.

    Returns
    -------
    dict(str: callable)
        Dictionary of devices.
    """
    import ophyd

    devices = {}
    for item in nspace.items():
        if isinstance(item[1], ophyd.Device):
            devices[item[0]] = item[1]
    return devices


def parse_plan(plan, *, allowed_plans, allowed_devices):
    """
    Parse the plan: replace the device names (str) in the plan specification by
    references to ophyd objects; replace plan name by the reference to the plan.

    Parameters
    ----------
    plan: dict
        Plan specification. Keys: `name` (str) - plan name, `args` - plan args,
        `kwargs` - plan kwargs.
    allowed_plans: dict(str, callable)
        Dictionary of allowed plans.
    allowed_devices: dict(str, ophyd.Device)
        Dictionary of allowed devices

    Returns
    -------
    dict
        Parsed plan specification that contains references to plans and Ophyd devices.

    Raises
    ------
    RuntimeError
        Raised in case parsing was not successful.
    """

    success = True
    err_msg = ""

    plan_name = plan["name"]
    plan_args = plan["args"] if "args" in plan else []
    plan_kwargs = plan["kwargs"] if "kwargs" in plan else {}

    def ref_from_name(v, allowed_items):
        if isinstance(v, str):
            if v in allowed_items:
                v = allowed_items[v]
        return v

    def process_argument(v, allowed_items):
        # Recursively process lists (iterables) and dictionaries
        if isinstance(v, str):
            v = ref_from_name(v, allowed_items)
        elif isinstance(v, dict):
            for key, value in v.copy().items():
                v[key] = process_argument(value, allowed_items)
        elif isinstance(v, Iterable):
            v_original = v
            v = list()
            for item in v_original:
                v.append(process_argument(item, allowed_items))
        return v

    # TODO: should we allow plan names as arguments?
    allowed_items = allowed_devices

    plan_func = process_argument(plan_name, allowed_plans)
    if isinstance(plan_func, str):
        success = False
        err_msg = f"Plan '{plan_name}' is not allowed or does not exist."

    plan_args_parsed = process_argument(plan_args, allowed_items)
    plan_kwargs_parsed = process_argument(plan_kwargs, allowed_items)

    if not success:
        raise RuntimeError("Error while parsing the plan: %s", err_msg)

    plan_parsed = {
        "name": plan_func,
        "args": plan_args_parsed,
        "kwargs": plan_kwargs_parsed,
    }
    return plan_parsed


# TODO: it may be a good idea to implement 'gen_list_of_plans_and_devices' as a separate CLI tool.
#       For now it can be called from IPython. It shouldn't be called automatically
#       at any time, since it loads profile collection. The list of allowed plans
#       and devices can be also typed manually, since it shouldn't be very large.


def gen_list_of_plans_and_devices(path=None, file_name="allowed_plans_and_devices.yaml", overwrite=False):
    """
    Generate the list of plans and devices from profile collection.
    The list is saved to file `allowed_plans_and_devices.yaml`.

    Parameters
    ----------
    path: str or None
        path to the directory where the file is to be created. None - create file in current directory.
    file_name: str
        name of the output YAML file
    overwrite: boolean
        overwrite the file if it already exists

    Returns
    -------
    None

    Raises
    ------
    RuntimeError
        Error occurred while creating or saving the lists.
    """
    try:
        if path is None:
            path = os.getcwd()

        nspace = load_profile_collection(path)
        plans = plans_from_nspace(nspace)
        devices = devices_from_nspace(nspace)

        def process_plan(plan):
            def filter_values(v):
                if v is inspect.Parameter.empty:
                    return ""
                return str(v)

            sig = inspect.signature(plan)

            ret = {"module": plan.__module__, "name": plan.__name__, "parameters": {}}
            for p in sig.parameters.values():
                working_dict = ret["parameters"][p.name] = {}
                for target in ["kind", "default", "annotation"]:
                    v = getattr(p, target)
                    if v is not p.empty:
                        working_dict[target] = filter_values(v)

            return ret

        allowed_plans_and_devices = {
            "allowed_plans": {k: process_plan(v) for k, v in plans.items()},
            "allowed_devices": {
                k: {"classname": type(v).__name__, "module": type(v).__module__} for k, v in devices.items()
            },
        }

        file_path = os.path.join(path, file_name)
        if os.path.exists(file_path) and not overwrite:
            raise IOError("File '%s' already exists. File overwriting is disabled.")

        with open(file_path, "w") as stream:
            yaml.dump(allowed_plans_and_devices, stream)

    except Exception as ex:
        raise RuntimeError(f"Failed to create the list of devices and plans: {str(ex)}")


def load_list_of_plans_and_devices(path_to_file=None):
    """
    Load the lists of allowed plans and devices from YAML file. Returns empty lists
    if `path_to_file` is None or "".

    Parameters
    ----------
    path_to_file: str on None
        Full path to .yaml file that contains the lists.

    Returns
    -------
    (list, list)
        List of allowed plans and list of allowed devices.

    Raises
    ------
    IOError in case the file does not exist.
    """
    if not path_to_file:
        return {"allowed_plans": [], "allowed_devices": []}

    if not os.path.isfile(path_to_file):
        raise IOError(
            f"Failed to load the list of allowed plans and devices: file '{path_to_file}' does not exist."
        )

    with open(path_to_file, "r") as stream:
        allowed_plans_and_devices = yaml.safe_load(stream)

    allowed_plans = allowed_plans_and_devices["allowed_plans"]
    allowed_devices = allowed_plans_and_devices["allowed_devices"]

    return allowed_plans, allowed_devices
