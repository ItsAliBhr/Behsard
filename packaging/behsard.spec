# PyInstaller spec: one-folder build "dist/Behsard" with Behsard.exe.
# Run from the repository root:  pyinstaller packaging/behsard.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("behsard")
    + ["jdatetime", "openpyxl", "multipart", "python_multipart"]
)
datas = collect_data_files("behsard", include_py_files=False) + collect_data_files("jdatetime")

a = Analysis(
    ["app_entry.py"],
    pathex=["../src"],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "pytest", "playwright", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Behsard",
    console=True,  # the console window shows the log; closing it stops the app
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Behsard")
