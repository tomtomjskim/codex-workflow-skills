import importlib.machinery
import importlib.util
import sys


def load_script(module_name, path):
    """Import an extensionless script as a module.

    SourceFileLoader.load_module() was removed in Python 3.15. This keeps its
    behavior: the module is registered in sys.modules before its code runs.
    """
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module
