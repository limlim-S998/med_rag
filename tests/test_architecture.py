# The boundaries, as tests.
#
# import-linter (.importlinter) covers medw_core, pipelines and ml, which are
# importable packages. It cannot cover the services: each one's code lives in
# a top-level `app` package inside its own image, so all four are called `app`
# and cannot be imported into one process to be analysed.
#
# So these walk the source with ast instead. Slower and cruder, and it is the
# only way to assert the rule that matters most: services do not import each
# other.

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVICES = ROOT / "services"


def _imports(path: pathlib.Path) -> set[str]:
    """Every module name imported by a file, as dotted strings."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        # level > 0 is a relative import, which by construction cannot reach
        # outside its own package.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_services_do_not_import_each_other():
    """Services talk over HTTP with a correlation header, not by import.

    This is what keeps them separately deployable. The moment one imports
    another they ship together, whether or not anyone decided that.
    """
    service_dirs = [d for d in SERVICES.iterdir() if d.is_dir()]
    offenders = []
    for svc in service_dirs:
        others = {d.name for d in service_dirs} - {svc.name}
        for path in _python_files(svc):
            for imported in _imports(path):
                head = imported.split(".")[0]
                if head == "services" or any(imported.startswith(o) for o in others):
                    offenders.append(f"{path.relative_to(ROOT)} imports {imported}")
    assert not offenders, "services must not import each other:\n" + "\n".join(offenders)


def test_services_do_not_import_pipelines():
    """The batch side is not a runtime dependency of the request path.

    pipelines pulls in Airflow, LlamaIndex and the parsers. A service that
    imported it would carry all of that into its image to use none of it.
    """
    offenders = [
        f"{p.relative_to(ROOT)} imports {i}"
        for p in _python_files(SERVICES)
        for i in _imports(p)
        if i == "pipelines" or i.startswith("pipelines.")
    ]
    assert not offenders, "\n".join(offenders)


def test_medw_core_does_not_import_upward():
    """medw_core is the floor: it is installed into every image, so anything
    it imports lands in all four."""
    lib = ROOT / "libs" / "medw_core"
    forbidden = ("app", "services", "pipelines", "ml")
    offenders = [
        f"{p.relative_to(ROOT)} imports {i}"
        for p in _python_files(lib)
        for i in _imports(p)
        if i.split(".")[0] in forbidden
    ]
    assert not offenders, "\n".join(offenders)


def test_every_service_exposes_both_probes():
    """Liveness and readiness are distinct, in every service.

    Conflating them means a slow dependency restarts pods in a crash loop
    instead of taking them out of rotation. The charts wire both paths, so a
    service missing one fails its probe forever rather than obviously.
    """
    missing = []
    for main in SERVICES.glob("*/app/main.py"):
        src = main.read_text()
        for probe in ("/healthz", "/readyz"):
            if probe not in src:
                missing.append(f"{main.relative_to(ROOT)} has no {probe}")
    assert not missing, "\n".join(missing)
