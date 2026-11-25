"""Builds (and optionally runs) the evaluation notebooks.   python model_evaluation/build_notebooks.py [--run] [name ...]

Each notebooks/NN_*.ipynb is generated from defs/NN_*.py (a list CELLS of ("md"|"code", text)) so the notebooks stay reproducible; --run executes them with the project's Python
and stores the outputs inside the .ipynb files.
"""
import importlib.util
import sys
from pathlib import Path

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

HERE = Path(__file__).resolve().parent
PRELUDE = '''import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()))
from common import *
print("project root:", ROOT)'''


def build(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cells = [new_markdown_cell(module.CELLS[0][1]), new_code_cell(PRELUDE)]
    for kind, text in module.CELLS[1:]:
        cells.append(new_markdown_cell(text.strip()) if kind == "md" else new_code_cell(text.strip()))
    notebook = new_notebook(cells=cells, metadata={"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}, "language_info": {"name": "python"}})
    target = HERE / "notebooks" / f"{path.stem}.ipynb"
    nbformat.write(notebook, target)
    return target


def run(target):
    from nbclient import NotebookClient

    notebook = nbformat.read(target, as_version=4)
    NotebookClient(notebook, timeout=1800, kernel_name="python3", resources={"metadata": {"path": str(target.parent)}}).execute()
    nbformat.write(notebook, target)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    for definition in sorted((HERE / "defs").glob("*.py")):
        if args and not any(a in definition.stem for a in args):
            continue
        target = build(definition)
        print("built", target.name, flush=True)
        if "--run" in sys.argv:
            run(target)
            print("ran  ", target.name, flush=True)
